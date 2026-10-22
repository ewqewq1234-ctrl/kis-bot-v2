"""KODEX 200 전략 B (실거래 보호 개정판)

1~4단계 10/20/30/40%, -2.8% 분할매수 및 기존 청산비율 유지.
주문 접수와 체결을 분리하고 실제 잔고가 확인되기 전에는 신규 주문을 막는다.
"""
import html
import math
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from nh_client import NHClient, NHClientError

KST = timezone(timedelta(hours=9))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StrategyB_Cloud")

STATE_FILE = "state.json"
SYMBOL = "069500"  # KODEX 200
MAX_SHARES = 32  # 사용자 실계좌 보유 상한 (증액 전 재검토 필요)
WEIGHTS = [0.10, 0.20, 0.30, 0.40]
STEP_DROP_RATE = 0.028
TS_TRIGGER_RATE = 0.020
TS_FALLBACK_RATE = 0.010
ESCAPE_PROFIT_RATE = 0.0005


class SafetyHalt(RuntimeError):
    """불확실한 계좌·주문 상태로 인한 거래 차단."""


def send_telegram(message):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info("[Telegram 미설정] %s", message)
        return False
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=5)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram 전송 실패: %s", exc)
        return False


def default_state():
    return {"symbol": SYMBOL, "current_stage": 0, "current_step": 0,
            "shares": 0, "avg_price": 0.0, "last_buy_price": 0.0,
            "entry_prices": [], "trailing_mode": False, "highest_price": 0,
            "pending_order": None, "last_exit_date": "", "last_run": "",
            "last_midday_report_date": ""}


def load_state():
    result = default_state()
    if not os.path.exists(STATE_FILE):
        # 기존 기록이 없어졌을 때 임의로 0주라고 가정하지 않고 실제 계좌로 검증한다.
        return result
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            saved = json.load(file)
        if not isinstance(saved, dict) or saved.get("symbol", SYMBOL) != SYMBOL:
            raise ValueError("상태 형식/종목 불일치")
        result.update(saved)  # 기존 누적손익 필드는 보존
        return result
    except (OSError, ValueError, TypeError) as exc:
        raise SafetyHalt(f"상태파일 해석 불가 — 임의 초기화 금지: {exc}") from exc


def save_state(state):
    path = os.path.abspath(STATE_FILE)
    directory = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def reset_position(state):
    state.update({"current_stage": 0, "current_step": 0, "shares": 0,
                  "avg_price": 0.0, "last_buy_price": 0.0, "entry_prices": [],
                  "trailing_mode": False, "highest_price": 0, "pending_order": None})


def classify_order_side(order):
    """NH 당일주문체결조회 'sby_dit_cd_nm' 값을 보수적으로 분류한다.

    식별 불가능한 주문은 중복 매도를 막기 위해 자동 주문을 중단한다.
    """
    name = str(order.get("sby_dit_cd_nm") or "").strip()
    if "매도" in name and "매수" not in name:
        return "sell"
    if "매수" in name and "매도" not in name:
        return "buy"
    raise SafetyHalt(f"주문 매매구분 확인 실패: {name!r} — 중복 매도 방지")


def block_duplicate_sell(orders):
    """당일 매수 체결은 청산을 막지 않되, 매도 이력은 중복 매도를 막는다."""
    if any(classify_order_side(order) == "sell" for order in orders):
        raise SafetyHalt("당일 동일 종목 매도 주문 기록 발견 — 중복 매도 방지, 수동 확인 필요")


def _order_qty(record, field):
    raw = record.get(field)
    try:
        value = float(str(raw).replace(",", ""))
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            raise ValueError("유효한 비음수 정수가 아님")
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SafetyHalt(f"체결조회 {field} 확인 실패: {raw!r}") from exc


def verify_sell_filled(pending, order_history):
    """보유잔고 0주 외에 당일/주문일 매도 전량체결 기록을 필수 확인한다.

    NH 주문 접수 응답의 mkt_orr_no 와 체결조회 itg_orr_no 는 명세상
    서로 다른 번호 체계일 수 있어 일치한다고 가정하지 않는다. 해당 일자에
    정확히 하나의 매도 주문만 있고 수량·체결이 완벽히 일치할 때만 허용한다.
    애매하면 자동 복구하지 않는다.
    """
    if order_history is None:
        raise SafetyHalt("매도 주문 체결내역 없음 — 잔고 0주만으로 청산 확정 금지")
    sales = [order for order in order_history if classify_order_side(order) == "sell"]
    if len(sales) != 1:
        raise SafetyHalt(f"매도 주문 {len(sales)}건: 단일 전량체결 검증 불가능")
    sale = sales[0]
    wanted = int(pending["qty"])
    ordered = _order_qty(sale, "orr_qty")
    executed = _order_qty(sale, "tot_cns_qty")
    unfilled = _order_qty(sale, "ny_cns_qty")
    if ordered != wanted or executed != wanted or unfilled != 0:
        raise SafetyHalt(f"매도 체결 불완전: 주문 {ordered}, 체결 {executed}, 미체결 {unfilled}, 기대 {wanted}")
    if "can_qty" in sale and _order_qty(sale, "can_qty") != 0:
        raise SafetyHalt("취소수량이 존재하여 매도 전량체결 판정 보류")
    logger.info("[매도 체결검증] %d주 단일 주문 완전체결 + 실잔고 0주", wanted)


def reconcile_pending(state, actual_qty, actual_avg, sell_order_history=None):
    """접수한 주문은 실제 잔고와 체결조회가 충분할 때만 단계 확정."""
    pending = state.get("pending_order")
    if not pending:
        return
    before, wanted = int(pending["before_qty"]), int(pending["qty"])
    side, target_step = pending["side"], int(pending["target_step"])
    expected = before + wanted if side == "buy" else before - wanted
    if actual_qty != expected:
        raise SafetyHalt(
            f"주문 체결 불확실: {side} {wanted}주, 접수 전 {before}주, "
            f"체결 후 예상 {expected}주 / 실제 {actual_qty}주. 미체결·부분체결 수동 확인 필요"
        )
    if side == "sell":
        if actual_qty != 0:
            raise SafetyHalt("매도 완료 수량 검증 실패")
        verify_sell_filled(pending, sell_order_history)
        last_exit_date = str(pending.get("created_at") or "")[:10]
        if len(last_exit_date) != 10:
            raise SafetyHalt("매도 체결일 확인 실패 — 자동 재진입 중단")
        reset_position(state)
        state["last_exit_date"] = last_exit_date
    else:
        old_avg = float(pending["before_avg"])
        estimated_price = (actual_qty * actual_avg - before * old_avg) / wanted
        if estimated_price <= 0:
            raise SafetyHalt("매수 체결단가 역산 실패")
        if target_step == 1:
            state["entry_prices"] = [estimated_price]
        else:
            if len(state.get("entry_prices", [])) != target_step - 1:
                raise SafetyHalt("단계별 기존 매수가 기록 불일치")
            state["entry_prices"].append(estimated_price)
        state.update({"current_step": target_step, "current_stage": target_step,
                      "shares": actual_qty, "avg_price": actual_avg,
                      "last_buy_price": estimated_price, "trailing_mode": False,
                      "highest_price": 0, "pending_order": None})
    save_state(state)
    send_telegram(f"✅ <b>실잔고 체결 확인</b> / 현재 {actual_qty}주, 단계 {state['current_step']}/4")


def reconcile_existing(state, actual_qty, actual_avg):
    """기존 버전 3단계(17주) -> 실잔고 4단계(32주) 복구만 허용한다."""
    if actual_qty > MAX_SHARES:
        raise SafetyHalt(f"보유 {actual_qty}주가 상한 {MAX_SHARES}주를 초과. 자동 주문 중단")
    stage, recorded_qty = int(state.get("current_step", 0)), int(state.get("shares", 0))
    if actual_qty == MAX_SHARES:
        if stage != 4 or recorded_qty != actual_qty:
            logger.warning("[상태 복구] 저장 %d주/%d단계 -> 실계좌 %d주/4단계", recorded_qty, stage, actual_qty)
            # 4단계에서는 직전 매수가에 기반한 추가매수가 없으므로 추정가격을 생성하지 않는다.
            state.update({"current_step": 4, "current_stage": 4, "shares": actual_qty,
                          "avg_price": actual_avg, "trailing_mode": False,
                          "highest_price": 0})
            save_state(state)
            send_telegram(f"🛡️ <b>4단계 상태 복구</b>: 실계좌 {actual_qty}주, 평단 {actual_avg:,.1f}원")
        else:
            state["avg_price"] = actual_avg
        return
    if actual_qty == 0:
        if stage != 0 or recorded_qty != 0:
            reset_position(state)
            state["last_exit_date"] = datetime.now(KST).date().isoformat()
            save_state(state)
        return
    if stage not in (1, 2, 3) or recorded_qty != actual_qty:
        raise SafetyHalt(f"저장 상태 {recorded_qty}주/{stage}단계 ↔ 실계좌 {actual_qty}주 불일치")
    if len(state.get("entry_prices", [])) != stage:
        raise SafetyHalt("분할매수 기록 개수와 매매 단계 불일치")
    state["avg_price"] = actual_avg


def entry_cooldown_active(state, now):
    """청산일과 그 다음 평일 세션까지 자동 재매수를 막는다 (공휴일은 별도 달력 미반영)."""
    last = state.get("last_exit_date")
    if not last:
        return False
    try:
        exit_day = datetime.strptime(last, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise SafetyHalt("last_exit_date 상태 불명확 — 신규 진입 금지") from exc
    next_session = exit_day + timedelta(days=1)
    while next_session.weekday() >= 5:
        next_session += timedelta(days=1)
    return now.date() <= next_session


def market_open(now):
    return (now.weekday() < 5 and
            ((now.hour == 9 and now.minute >= 5) or (10 <= now.hour < 15) or
             (now.hour == 15 and now.minute <= 20)))


def place_order(client, state, side, qty, quote, target_step, before_qty, before_avg, now,
                sell_limit_price=None):
    if qty <= 0 or (side == "buy" and before_qty + qty > MAX_SHARES):
        raise SafetyHalt("최대 보유수량을 초과하거나 주문수량이 0 이하")
    # 실주문 실행은 운영자의 명시적 환경변수 설정이 필요하다.
    if os.getenv("ENABLE_LIVE_ORDERS", "NO").upper() != "YES":
        logger.warning("[DRY RUN] 실제 주문 비활성: %s %d주", side, qty)
        send_telegram(f"🧪 <b>주문 미실행(DRY RUN)</b> {html.escape(side)} {qty}주")
        return
    if sell_limit_price is not None and (side != "sell" or sell_limit_price <= 0):
        raise SafetyHalt("매도 지정가 설정이 유효하지 않음")
    order_price = (quote if side == "buy" else
                   int(sell_limit_price) if sell_limit_price is not None else 0)
    state["pending_order"] = {
        "side": side, "qty": qty, "target_step": target_step,
        "before_qty": before_qty, "before_avg": before_avg,
        "requested_price": quote, "limit_price": order_price if sell_limit_price is not None else None,
        "created_at": now.isoformat(),
        "order_no": None, "status": "submitting"
    }
    save_state(state)  # 예외/응답 유실 시에는 다음 실행에서 새 주문을 막는다.
    receipt = client.submit_order(SYMBOL, qty, side, price=order_price)
    state["pending_order"].update({"order_no": receipt["order_no"], "status": "accepted"})
    save_state(state)
    send_telegram(f"📨 <b>주문 접수(체결 미확인)</b> {html.escape(side)} {qty}주 / "
                  f"접수번호 {html.escape(receipt['order_no'])}")


def send_midday_report():
    now = datetime.now(KST)
    report_date = now.date().isoformat()
    state = load_state()
    if state.get("last_midday_report_date") == report_date:
        logger.info("오늘 중간보고는 이미 전송했습니다.")
        return False

    if not market_open(now):
        # Retain the scheduled Telegram report without claiming account/fill verification.
        pending = "있음 (체결 미확인)" if state.get("pending_order") else "저장 기록 없음"
        message = (
            f"🕑 <b>KODEX 200 장외 대기</b> / {report_date} KST\n"
            f"저장 보유 {state['shares']}주 / 저장 평단 {state['avg_price']:,.1f}원\n"
            f"미확인 주문: {pending}\n"
            "※ 실계좌·시세 미조회. 저장 기록이며 현재 잔고 또는 체결 확인이 아닙니다."
        )
        if not send_telegram(message):
            return False
        state["last_midday_report_date"] = report_date
        save_state(state)
        return True

    client = NHClient()
    assets = client.get_asset_status()
    current = assets["holdings"].get(SYMBOL, {})
    qty = int(current.get("qty", 0))
    avg = float(current.get("avg_price", 0))
    quote = client.get_current_price(SYMBOL)
    pending = state.get("pending_order")
    pending_text = ("없음" if not pending else
                    f"{html.escape(str(pending.get('side', '미상')))} "
                    f"{html.escape(str(pending.get('qty', '미상')))}주 "
                    f"({html.escape(str(pending.get('status', '상태 미상')))})")
    message = (
        f"🕑 <b>KODEX 200 장중 잠정 스냅샷</b> / {report_date} KST\n"
        f"보유 {qty}주 / 평단 {avg:,.1f}원 / 현재가 {quote:,}원\n"
        f"총자산 {assets['total_asset']:,.0f}원 / 미확인 주문: {pending_text}\n"
        "※ 장중 잠정 보고이며 최종 잔고·자산과 다를 수 있습니다."
    )
    if not send_telegram(message):
        return False

    state["last_midday_report_date"] = report_date
    save_state(state)
    return True


def _run_strategy_core():
    now = datetime.now(KST)
    logger.info("=== 전략 B %s KST ===", now.strftime("%Y-%m-%d %H:%M:%S"))
    state = load_state()
    # Time gate precedes broker construction and all account/quote/order inquiries.
    # Only execution metadata is recorded; saved positions and pending are not evidence of fills.
    if not market_open(now):
        logger.info("장외 대기: 계좌·시세·주문 조회 생략, 매수·매도 금지 (체결 미확인)")
        if state.get("pending_order"):
            logger.warning("미확인 주문이 남아 있습니다. 다음 장중 반드시 확인")
        state["last_run"] = now.isoformat()
        save_state(state)
        return
    client = NHClient()
    assets = client.get_asset_status()
    current = assets["holdings"].get(SYMBOL, {})
    qty, avg = int(current.get("qty", 0)), float(current.get("avg_price", 0))
    quote = client.get_current_price(SYMBOL)
    logger.info("실계좌 %d주 / 평단 %.2f / 현재가 %d / 총자산 %.0f", qty, avg, quote, assets["total_asset"])
    if qty and avg <= 0:
        raise SafetyHalt("실계좌 평균매수가 누락")
    if qty > MAX_SHARES:
        raise SafetyHalt("계좌 보유량이 설정 상한 초과")

    orders = client.get_daily_orders(SYMBOL, now.strftime("%Y%m%d"))
    outstanding = [o for o in orders if float(o["ny_cns_qty"]) > 0]
    if outstanding:
        raise SafetyHalt(f"오늘 KODEX 200 미체결 주문 {len(outstanding)}건 — 신규 주문 중단")

    # 매도 pending 은 잔고 0주만으로 확정하지 않고, 주문일 전량체결까지 확인한다.
    sell_history = None
    pending = state.get("pending_order")
    if pending and pending.get("side") == "sell":
        try:
            ordered_day = datetime.fromisoformat(pending["created_at"]).strftime("%Y%m%d")
        except (KeyError, ValueError, TypeError) as exc:
            raise SafetyHalt("매도 주문일자 확인 실패 — 수동 확인 필요") from exc
        sell_history = (orders if ordered_day == now.strftime("%Y%m%d") else
                        client.get_daily_orders(SYMBOL, ordered_day))

    # 첫 주문 다음 번 실행까지 pending 유지. 체결 확인 후에만 다음 매매를 허용.
    reconcile_pending(state, qty, avg, sell_order_history=sell_history)
    if not state.get("pending_order"):
        reconcile_existing(state, qty, avg)
    stage = int(state["current_step"])
    state["last_run"] = now.isoformat()
    save_state(state)

    # 매수: 기존 보수적 정책 유지. 당일 매수/매도 이력이 있으면 새 매수 금지.
    # 매도: 과거 당일 매수 이력은 청산을 막지 않지만, 매도 이력은 중복매도 방지.
    if stage == 0 and orders:
        logger.info("오늘 주문 기록 %d건 존재: 신규 매수 생략", len(orders))
        return

    if stage == 0:
        if entry_cooldown_active(state, now):
            logger.info("청산 후 다음 거래 세션까지 신규 매수 대기: 청산일 %s", state["last_exit_date"])
            return
        target_step = 1
        budget = assets["total_asset"] * WEIGHTS[0]
        order_qty = int(budget // quote)
        if order_qty > 0:
            if assets["available_cash"] is None or assets["available_cash"] < order_qty * quote:
                raise SafetyHalt("주문가능금액 부족/불명확 — 신규 매수 금지")
            place_order(client, state, "buy", order_qty, quote, target_step, qty, avg, now)
        return

    if stage in (1, 2):
        trigger = avg * (1 + TS_TRIGGER_RATE)
        if not state.get("trailing_mode") and quote >= trigger:
            state["trailing_mode"], state["highest_price"] = True, quote
            save_state(state)
        if state.get("trailing_mode"):
            state["highest_price"] = max(quote, state["highest_price"])
            save_state(state)
            if quote <= state["highest_price"] * (1 - TS_FALLBACK_RATE):
                block_duplicate_sell(orders)
                # 1~2단계의 트레일링은 빠른 청산을 우선해 기존 시장가 유지.
                place_order(client, state, "sell", qty, quote, 0, qty, avg, now)
                return
    elif stage in (3, 4) and quote >= avg * (1 + ESCAPE_PROFIT_RATE):
        block_duplicate_sell(orders)
        # 3~4단계 본전 회복 청산: 시장가 슬리피지 대신 현재 최우선 매수호가에
        # 지정가 매도. 호가가 발동 가격보다 낮으면 손해 보는 자동 청산을 피한다.
        # +0.05%는 거래비용 차감 전 가격 조건이며 순손익을 보장하지 않는다.
        minimum = avg * (1 + ESCAPE_PROFIT_RATE)
        bid = client.get_best_bid(SYMBOL)
        if bid < minimum:
            logger.info("본전 청산 보류: 매수호가 %d원 < 목표가 %.2f원", bid, minimum)
            return
        place_order(client, state, "sell", qty, quote, 0, qty, avg, now,
                    sell_limit_price=bid)
        return

    if stage >= 4 or qty >= MAX_SHARES:
        logger.info("4단계/보유상한: 추가매수 금지")
        return
    last_buy = float(state["entry_prices"][-1])
    if quote <= last_buy * (1 - STEP_DROP_RATE):
        target_step = stage + 1
        budget = assets["total_asset"] * WEIGHTS[target_step - 1]
        order_qty = int(budget // quote)
        if order_qty > 0:
            if qty + order_qty > MAX_SHARES:
                raise SafetyHalt(f"매수 예정 {order_qty}주 포함 {qty + order_qty}주 > 상한 {MAX_SHARES}주")
            if assets["available_cash"] is None or assets["available_cash"] < order_qty * quote:
                raise SafetyHalt("주문가능금액 부족/불명확 — 추가매수 금지")
            place_order(client, state, "buy", order_qty, quote, target_step, qty, avg, now)


def run_strategy():
    _run_strategy_core()
    if os.getenv("RUN_MIDDAY_REPORT") == "Y":
        send_midday_report()


if __name__ == "__main__":
    try:
        run_strategy()
    except (SafetyHalt, NHClientError, OSError, ValueError, KeyError, TypeError) as error:
        logger.error("[거래 중단] %s", error, exc_info=True)
        send_telegram(f"🚨 <b>자동매매 안전중단</b>\n{html.escape(str(error))}")
        raise SystemExit(1)
