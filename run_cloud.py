import os
import json
import logging
from datetime import datetime, timezone, timedelta
import requests
from nh_client import NHClient

KST = timezone(timedelta(hours=9))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StrategyB_Cloud")

STATE_FILE = "state.json"
SYMBOL = "069500"  # KODEX 200

# [B 전략] 핵심 매트릭스 파라미터
WEIGHTS = [0.10, 0.20, 0.30, 0.40]   # 4단계 비대칭 자본금 비중
STEP_DROP_RATE = 0.028                # 단계별 -2.8% 하락 시 추가 매수
TS_TRIGGER_RATE = 0.020               # 1~2단계: 평단 대비 +2.0% 도달 시 TS 모드 가동
TS_FALLBACK_RATE = 0.010              # 1~2단계: 최고가 대비 -1.0% 반락 시 익절
ESCAPE_PROFIT_RATE = 0.0005           # 3~4단계: 평단 대비 +0.05% 도달 시 본전 탈출 (제비용 회수)


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.info(f"[Telegram 미설정] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"텔레그램 전송 실패: {e}")


def load_state() -> dict:
    default_state = {
        "symbol": SYMBOL,
        "current_stage": 0,
        "current_step": 0,
        "shares": 0,
        "avg_price": 0.0,
        "last_buy_price": 0.0,
        "entry_prices": [],
        "trailing_mode": False,
        "highest_price": 0,
        "last_run": ""
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k, v in default_state.items():
                        if k not in loaded:
                            loaded[k] = v
                    return loaded
        except Exception as e:
            logger.warning(f"state.json 로드 실패, 기본값으로 초기화: {e}")
    return default_state


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def run_strategy():
    now_kst = datetime.now(KST)
    logger.info(f"=== B 전략 엔진 가동 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')} KST) ===")

    client = NHClient()
    state = load_state()

    # 1. 실제 자산 및 잔고 동기화 (SDK 규칙 준수: aly_qut_cd="2")
    asset_info = client.get_asset_status()
    total_asset = float(asset_info.get("total_asset", 0))
    holding_data = asset_info.get("holdings", {}).get(SYMBOL, {})
    holding_qty = int(holding_data.get("qty", 0))
    account_avg_price = float(holding_data.get("avg_price", 0))

    # 2. 실시간 시세 조회 (SDK 규칙 준수: view_main_yn="Y")
    current_price = client.get_current_price(SYMBOL)
    if current_price is None or current_price <= 0:
        logger.error("시세 조회 실패로 이번 루틴을 안전하게 중단합니다.")
        return

    logger.info(
        f"[*] 실계좌 데이터 동기화: 종목={SYMBOL}, 현재가={current_price:,}원, "
        f"총자산={total_asset:,.0f}원, 보유수량={holding_qty:,}주, 평단가={account_avg_price:,.1f}원"
    )

    # 3. 자체 치유(Self-Healing) 로직 보정
    current_step = state.get("current_step", 0)

    if holding_qty == 0 and current_step > 0:
        logger.info("[자체 치유] 증권사 잔고 0주 확인 -> state.json 포지션 리셋")
        state["current_step"] = 0
        state["current_stage"] = 0
        state["shares"] = 0
        state["entry_prices"] = []
        state["trailing_mode"] = False
        state["highest_price"] = 0
        save_state(state)

    elif holding_qty > 0 and current_step == 0:
        rec_price = int(account_avg_price) if account_avg_price > 0 else current_price
        if holding_qty >= 7:
            logger.info(f"[자체 치유] 실제 잔고 {holding_qty}주 감지 -> 2단계 상태로 자동 복구")
            state["current_step"] = 2
            state["current_stage"] = 2
            state["entry_prices"] = [rec_price, rec_price]
        else:
            logger.info(f"[자체 치유] 실제 잔고 {holding_qty}주 감지 -> 1단계 상태로 자동 복구")
            state["current_step"] = 1
            state["current_stage"] = 1
            state["entry_prices"] = [rec_price]

        state["shares"] = holding_qty
        state["avg_price"] = rec_price
        state["trailing_mode"] = False
        state["highest_price"] = max(rec_price, current_price)
        save_state(state)

    state["shares"] = holding_qty
    if account_avg_price > 0:
        state["avg_price"] = account_avg_price

    effective_avg = account_avg_price if account_avg_price > 0 else (
        sum(state["entry_prices"]) / len(state["entry_prices"]) if state.get("entry_prices") else float(current_price)
    )
    current_step = state.get("current_step", 0)

    # -------------------------------------------------------------------------
    # [정규장 시간 가드] 09:05 ~ 15:20 사이에만 신규 주문 전송 허용
    # -------------------------------------------------------------------------
    is_market_open = (
        (now_kst.hour == 9 and now_kst.minute >= 5) or 
        (10 <= now_kst.hour < 15) or 
        (now_kst.hour == 15 and now_kst.minute <= 20)
    )

    if not is_market_open:
        logger.info(f"[정규장 마감 시간] 현재 시각({now_kst.strftime('%H:%M')})은 매매 시간 외이므로 주문 집행을 생략합니다.")
        # 장 마감 브리핑 (15:00 ~ 15:30) 수행
        if now_kst.hour == 15 and now_kst.minute < 30:
            unrealized_pct = ((current_price / effective_avg) - 1) * 100 if holding_qty > 0 else 0.0
            send_telegram(
                f"📊 <b>[KODEX 200] 정규장 마감 일일 결산</b>\n"
                f"• 종가: {current_price:,}원\n"
                f"• 포지션 단계: {current_step}/4단계\n"
                f"• 보유 수량: {holding_qty:,}주\n"
                f"• 평가 손익률: {unrealized_pct:+.2f}%\n"
                f"• 트레일링 가동 여부: {state.get('trailing_mode', False)}"
            )
        state["last_run"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        return

    # -------------------------------------------------------------------------
    # [조건 1] 포지션 미보유 시: 1단계 신규 진입 (10% 비중)
    # -------------------------------------------------------------------------
    if current_step == 0:
        allocated_budget = total_asset * WEIGHTS[0]
        order_qty = int(allocated_budget // current_price)
        if order_qty > 0:
            logger.info(f"▶ [1단계 신규 진입] 예산: {allocated_budget:,.0f}원 -> 시장가 {order_qty:,}주 매수")
            success, reason = client.order_cash_buy_with_reason(SYMBOL, order_qty)
            if success:
                state["current_step"] = 1
                state["current_stage"] = 1
                state["shares"] = order_qty
                state["avg_price"] = current_price
                state["last_buy_price"] = current_price
                state["entry_prices"] = [current_price]
                state["trailing_mode"] = False
                state["highest_price"] = current_price
                save_state(state)
                send_telegram(
                    f"🟢 <b>[KODEX 200] 1단계 진입 체결</b>\n"
                    f"• 체결단가: {current_price:,}원\n"
                    f"• 매수수량: {order_qty:,}주\n"
                    f"• 투입비중: 10% ({allocated_budget:,.0f}원)"
                )
            else:
                send_telegram(
                    f"🚨 <b>[KODEX 200] 1단계 주문 거절 경고</b>\n"
                    f"• 시도 수량: {order_qty:,}주\n"
                    f"• 거절 사유: <code>{reason}</code>\n"
                    f"• <b>계좌 예수금을 확인하세요!</b>"
                )
        return

    # -------------------------------------------------------------------------
    # [조건 2] 청산 매트릭스 판정
    # -------------------------------------------------------------------------
    if current_step in [1, 2]:
        ts_trigger_price = effective_avg * (1 + TS_TRIGGER_RATE)

        if not state.get("trailing_mode", False) and current_price >= ts_trigger_price:
            logger.info(f"★ [트레일링 모드 가동] 평단(+2.0%) 돌파: 현재가 {current_price:,}원")
            state["trailing_mode"] = True
            state["highest_price"] = current_price
            save_state(state)
            send_telegram(
                f"🎯 <b>[KODEX 200] 트레일링 스탑 가동</b>\n"
                f"• 현재가: {current_price:,}원 (평단 대비 +2.0% 돌파)\n"
                f"• 고점 대비 -1.0% 반락 시 익절 집행"
            )

        if state.get("trailing_mode", False):
            if current_price > state.get("highest_price", 0):
                state["highest_price"] = current_price
                save_state(state)

            ts_stop_price = state["highest_price"] * (1 - TS_FALLBACK_RATE)
            if current_price <= ts_stop_price:
                logger.info(f"★ [트레일링 익절 조건 충족] 전량 매도 시도")
                success, reason = client.order_cash_sell_with_reason(SYMBOL, holding_qty)
                if success:
                    profit_pct = ((current_price / effective_avg) - 1) * 100
                    send_telegram(
                        f"🎉 <b>[KODEX 200] 트레일링 익절 완료</b>\n"
                        f"• 청산수량: {holding_qty:,}주 | 매도가격: {current_price:,}원\n"
                        f"• 수익률: +{profit_pct:.2f}%"
                    )
                    state["current_step"] = 0
                    state["current_stage"] = 0
                    state["shares"] = 0
                    state["entry_prices"] = []
                    state["trailing_mode"] = False
                    state["highest_price"] = 0
                    save_state(state)
                else:
                    send_telegram(f"🚨 <b>[KODEX 200] 매도 주문 거절 경고</b>\n• 사유: <code>{reason}</code>")
                return

    elif current_step in [3, 4]:
        escape_price = effective_avg * (1 + ESCAPE_PROFIT_RATE)
        if current_price >= escape_price:
            logger.info(f"★ [비상 본전 탈출 충족] 전량 청산 시도")
            success, reason = client.order_cash_sell_with_reason(SYMBOL, holding_qty)
            if success:
                send_telegram(
                    f"🛡️ <b>[KODEX 200] 비상 본전 탈출 체결 (무손실)</b>\n"
                    f"• 탈출단계: {current_step}단계 | 평단가: {effective_avg:,.1f}원\n"
                    f"• 매도단가: {current_price:,}원 | 수량: {holding_qty:,}주"
                )
                state["current_step"] = 0
                state["current_stage"] = 0
                state["shares"] = 0
                state["entry_prices"] = []
                state["trailing_mode"] = False
                state["highest_price"] = 0
                save_state(state)
            else:
                send_telegram(f"🚨 <b>[KODEX 200] 탈출 매도 주문 거절 경고</b>\n• 사유: <code>{reason}</code>")
            return

    # -------------------------------------------------------------------------
    # [조건 3] 단계별 추가 분할 매수 판정 (-2.8% 하락 시)
    # -------------------------------------------------------------------------
    if current_step < 4 and state.get("entry_prices"):
        last_price = state["entry_prices"][-1]
        next_trigger_price = last_price * (1 - STEP_DROP_RATE)

        if current_price <= next_trigger_price:
            next_step = current_step + 1
            allocated_budget = total_asset * WEIGHTS[next_step - 1]
            order_qty = int(allocated_budget // current_price)

            if order_qty > 0:
                logger.info(f"▼ [{next_step}단계 물타기 진입] 현재가 {current_price:,}원 <= 기준가 {next_trigger_price:,.0f}원")
                success, reason = client.order_cash_buy_with_reason(SYMBOL, order_qty)
                
                if success:
                    state["current_step"] = next_step
                    state["current_stage"] = next_step
                    state["entry_prices"].append(current_price)
                    state["trailing_mode"] = False
                    state["highest_price"] = current_price
                    save_state(state)
                    send_telegram(
                        f"⚠️ <b>[KODEX 200] {next_step}단계 추가 분할 매수 체결</b>\n"
                        f"• 체결단가: {current_price:,}원 | 수량: {order_qty:,}주\n"
                        f"• 투입비중: {WEIGHTS[next_step - 1] * 100:.0f}% ({allocated_budget:,.0f}원)"
                    )
                else:
                    # 예수금 부족 등 거절 시 텔레그램 경고 발송
                    send_telegram(
                        f"🚨 <b>[KODEX 200] {next_step}단계 주문 거절 경고</b>\n"
                        f"• 시도 수량: {order_qty:,}주 (현재가 {current_price:,}원)\n"
                        f"• 거절 사유: <code>{reason}</code>\n"
                        f"• <b>조치 필요: 계좌 예수금을 확인하세요!</b>"
                    )
            return

    state["last_run"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


if __name__ == "__main__":
    run_strategy()
