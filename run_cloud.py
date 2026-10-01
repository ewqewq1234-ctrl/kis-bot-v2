import os
import json
import logging
from datetime import datetime
import requests

# KST 표준시 정의 (UTC + 9시간)
KST = timezone(timedelta(hours=9))

# 기 구축된 모듈 임포트
from nh_client import NHClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StrategyB_Cloud")

STATE_FILE = "state.json"
SYMBOL = "069500"  # KODEX 200

# [B 전략] 핵심 매트릭스 파라미터
WEIGHTS = [0.10, 0.20, 0.30, 0.40]   # 4단계 비대칭 자본금 비중
STEP_DROP_RATE = 0.028                # 단계별 -2.8% 하락 시 추가 매수
TS_TRIGGER_RATE = 0.020               # 1~2단계: 평단 대비 +2.0% 도달 시 TS 모드 가동
TS_FALLBACK_RATE = 0.010              # 1~2단계: 최고가 대비 -1.0% 반락 시 익절
ESCAPE_PROFIT_RATE = 0.0005           # 3~4단계: 평단 대비 +0.05% 도달 시 제비용 건지고 본전 탈출


def send_telegram(message: str):
    """텔레그램 알림 전송"""
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
    """state.json 상태 복원 (Actions 컨테이너 휘발 방지)"""
    default_state = {
        "symbol": SYMBOL,
        "current_step": 0,
        "entry_prices": [],
        "trailing_mode": False,
        "highest_price": 0,
        "last_run": ""
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"state.json 로드 실패, 기본값으로 초기화: {e}")
    return default_state


def save_state(state: dict):
    """state.json 최신화"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def run_strategy():
    now_kst = datetime.now(KST)  # pytz 없이 표준 KST 시간 추출
    logger.info(f"=== B 전략 엔진 가동 ({now_kst.strftime('%Y-%m-%d %H:%M:%S')} KST) ===")

    client = NHClient()
    state = load_state()

    # 1. NH투자증권 실제 자산 및 잔고 조회 (/assetStatus 기반)
    asset_info = client.get_asset_status()
    total_asset = float(asset_info.get("total_asset", 0))  # Output_0.nas_amt (순자산)
    holding_data = asset_info.get("holdings", {}).get(SYMBOL, {})
    holding_qty = int(holding_data.get("qty", 0))          # Output_1.itg_bnc_qty (실제 보유수량)
    account_avg_price = float(holding_data.get("avg_price", 0)) # Output_1.phs_pr (증권사 공식 평단가)

    # 2. 실시간 시세 조회 (/currentExecution 기반)
    current_price = client.get_current_price(SYMBOL)
    if current_price <= 0:
        logger.error("시세 조회 실패로 안전을 위해 이번 루틴을 중단합니다.")
        return

    logger.info(
        f"[*] 실계좌 데이터 동기화 완료: 종목={SYMBOL}, 현재가={current_price:,}원, "
        f"총자산={total_asset:,.0f}원, 보유수량={holding_qty:,}주, 평단가={account_avg_price:,.1f}원"
    )

    # 3. 자체 치유(Self-Healing) 로직: GitHub Actions 지연에 따른 상태 왜곡 방지
    if holding_qty == 0 and state["current_step"] > 0:
        logger.info("[자체 치유] 증권사 잔고가 0주이므로 state.json 포지션을 초기화합니다.")
        state["current_step"] = 0
        state["entry_prices"] = []
        state["trailing_mode"] = False
        state["highest_price"] = 0
        save_state(state)

    elif holding_qty > 0 and state["current_step"] == 0:
        logger.info("[자체 치유] 실제 보유 수량이 존재하므로 1단계 상태로 강제 복구합니다.")
        state["current_step"] = 1
        state["entry_prices"] = [int(account_avg_price)]
        state["trailing_mode"] = False
        state["highest_price"] = current_price
        save_state(state)

    effective_avg = account_avg_price if account_avg_price > 0 else (
        sum(state["entry_prices"]) / len(state["entry_prices"]) if state["entry_prices"] else current_price
    )

    # -------------------------------------------------------------------------
    # [조건 1] 포지션 미보유 시: 1단계 신규 진입 (10% 비중)
    # -------------------------------------------------------------------------
    if state["current_step"] == 0:
        allocated_budget = total_asset * WEIGHTS[0]
        order_qty = int(allocated_budget // current_price)
        if order_qty > 0:
            logger.info(f"▶ [1단계 신규 진입] 예산: {allocated_budget:,.0f}원 -> 시장가 {order_qty:,}주 매수")
            if client.order_cash_buy(SYMBOL, order_qty):
                state["current_step"] = 1
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
        return

    # -------------------------------------------------------------------------
    # [조건 2] 청산 매트릭스 판정 (이원화 핵심 로직)
    # -------------------------------------------------------------------------
    # 2-A. 1~2단계: 트레일링 스탑 가동 (+2.0% 도달 후 -1.0% 반락 시 익절)
    if state["current_step"] in [1, 2]:
        ts_trigger_price = effective_avg * (1 + TS_TRIGGER_RATE)

        # 트레일링 모드 미진입 상태에서 +2.0% 도달 시 가동
        if not state.get("trailing_mode", False) and current_price >= ts_trigger_price:
            logger.info(f"★ [트레일링 모드 가동] 평단(+2.0%) 돌파: 현재가 {current_price:,}원 >= 트리거 {ts_trigger_price:,.0f}원")
            state["trailing_mode"] = True
            state["highest_price"] = current_price
            save_state(state)
            send_telegram(
                f"🎯 <b>[KODEX 200] 트레일링 스탑 가동</b>\n"
                f"• 현재가: {current_price:,}원 (평단 대비 +2.0% 돌파)\n"
                f"• 최고가 추적 시작, 고점 대비 -1.0% 반락 시 익절 집행"
            )

        # 트레일링 모드 추적 중 최고가 갱신 및 반락 익절 검사
        if state.get("trailing_mode", False):
            if current_price > state.get("highest_price", 0):
                state["highest_price"] = current_price
                save_state(state)
                logger.info(f"[*] 최고가 갱신: {current_price:,}원")

            ts_stop_price = state["highest_price"] * (1 - TS_FALLBACK_RATE)
            if current_price <= ts_stop_price:
                logger.info(f"★ [트레일링 익절 조건 충족] 현재가 {current_price:,}원 <= 스탑가 {ts_stop_price:,.0f}원. 전량 매도")
                if client.order_cash_sell(SYMBOL, holding_qty):
                    profit_pct = ((current_price / effective_avg) - 1) * 100
                    send_telegram(
                        f"🎉 <b>[KODEX 200] 트레일링 익절 완료</b>\n"
                        f"• 청산수량: {holding_qty:,}주\n"
                        f"• 매도가격: {current_price:,}원\n"
                        f"• 수익률: +{profit_pct:.2f}%\n"
                        f"• 사이클 종료 및 상태 초기화"
                    )
                    state["current_step"] = 0
                    state["entry_prices"] = []
                    state["trailing_mode"] = False
                    state["highest_price"] = 0
                    save_state(state)
                return

    # 2-B. 3~4단계: 비상 본전 탈출 (평단가 대비 +0.05% 도달 시 전량 청산)
    elif state["current_step"] in [3, 4]:
        escape_price = effective_avg * (1 + ESCAPE_PROFIT_RATE)
        if current_price >= escape_price:
            logger.info(f"★ [비상 본전 탈출 충족] 현재가 {current_price:,}원 >= 탈출목표가 {escape_price:,.0f}원")
            if client.order_cash_sell(SYMBOL, holding_qty):
                send_telegram(
                    f"🛡️ <b>[KODEX 200] 비상 본전 탈출 체결 (무손실)</b>\n"
                    f"• 탈출단계: {state['current_step']}단계 풀베팅 후 탈출\n"
                    f"• 매도단가: {current_price:,}원 (평단가 {effective_avg:,.1f}원)\n"
                    f"• 청산수량: {holding_qty:,}주\n"
                    f"• 원금 전액 회수 성공"
                )
                state["current_step"] = 0
                state["entry_prices"] = []
                state["trailing_mode"] = False
                state["highest_price"] = 0
                save_state(state)
            return

    # -------------------------------------------------------------------------
    # [조건 3] 단계별 추가 분할 매수 판정 (직전 체결가 대비 -2.8% 하락 시)
    # -------------------------------------------------------------------------
    if state["current_step"] < 4:
        last_price = state["entry_prices"][-1]
        next_trigger_price = last_price * (1 - STEP_DROP_RATE)

        if current_price <= next_trigger_price:
            next_step = state["current_step"] + 1
            allocated_budget = total_asset * WEIGHTS[next_step - 1]
            order_qty = int(allocated_budget // current_price)

            if order_qty > 0:
                logger.info(f"▼ [{next_step}단계 물타기 진입] 현재가 {current_price:,}원 <= 기준가 {next_trigger_price:,.0f}원")
                if client.order_cash_buy(SYMBOL, order_qty):
                    state["current_step"] = next_step
                    state["entry_prices"].append(current_price)
                    # 추가 매수 시 기존 트레일링 플래그 리셋
                    state["trailing_mode"] = False
                    state["highest_price"] = current_price
                    save_state(state)
                    send_telegram(
                        f"⚠️ <b>[KODEX 200] {next_step}단계 추가 분할 매수</b>\n"
                        f"• 직전단가 대비 -2.8% 하락 감지\n"
                        f"• 체결단가: {current_price:,}원 | 수량: {order_qty:,}주\n"
                        f"• 비중 투입: {WEIGHTS[next_step - 1] * 100:.0f}% ({allocated_budget:,.0f}원)"
                    )
            return

    # 4. 장 마감 일일 결산 브리핑 (15시 이후 실행 주기)
    if now_kst.hour == 15 and now_kst.minute < 30:
        unrealized_pct = ((current_price / effective_avg) - 1) * 100 if holding_qty > 0 else 0.0
        send_telegram(
            f"📊 <b>[KODEX 200] 정규장 마감 일일 결산</b>\n"
            f"• 종가: {current_price:,}원\n"
            f"• 포지션 단계: {state['current_step']}/4단계\n"
            f"• 보유 수량: {holding_qty:,}주\n"
            f"• 평가 손익률: {unrealized_pct:+.2f}%\n"
            f"• 트레일링 가동 여부: {state.get('trailing_mode', False)}"
        )

    state["last_run"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


if __name__ == "__main__":
    run_strategy()
