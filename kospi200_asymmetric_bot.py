import os
import json
import logging
from typing import Dict, Any

# 기존 프로젝트 내 맞춤형 nh_client 임포트
from nh_client import NHClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StrategyB")

STATE_FILE = "state.json"
SYMBOL = "069500"  # KODEX 200

# 전략 파라미터 정의
WEIGHTS = [0.10, 0.20, 0.30, 0.40]  # 4단계 비대칭 비중
STEP_DROP_RATE = 0.028               # -2.8% 하락 시 단계 진입
TS_TRIGGER_RATE = 0.020              # 1~2단계: 평단 대비 +2.0% 도달 시 TS 가동
TS_FALLBACK_RATE = 0.010             # TS 가동 후 최고가 대비 -1.0% 반락 시 익절
ESCAPE_PROFIT_RATE = 0.0005          # 3~4단계: 평단 대비 +0.05% 도달 시 본전 탈출


def load_state() -> Dict[str, Any]:
    default_state = {
        "symbol": SYMBOL,
        "current_step": 0,
        "entry_prices": [],
        "trailing_mode": False,
        "highest_price": 0
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"state.json 로드 실패, 기본값 사용: {e}")
    return default_state


def save_state(state: Dict[str, Any]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def run_strategy_b():
    client = NHClient()
    state = load_state()

    # 1. 증권사 실제 잔고 및 계좌 자산 조회 (/krstock/inquiry/v1/assetStatus 연동)
    asset_info = client.get_asset_status()
    total_asset = float(asset_info.get("total_asset", 0))  # 순자산 (nas_amt)
    holding_qty = int(asset_info.get("holdings", {}).get(SYMBOL, {}).get("qty", 0))
    account_avg_price = float(asset_info.get("holdings", {}).get(SYMBOL, {}).get("avg_price", 0))

    # 2. 실시간 시세 조회 (/krstock/quote/v1/currentExecution 연동)
    current_price = client.get_current_price(SYMBOL)
    if current_price <= 0:
        logger.error("시세 수신 실패로 이번 주기 실행을 중단합니다.")
        return

    logger.info(f"[*] 상태 점검 - 종목: {SYMBOL} | 현재가: {current_price:,}원 | 보유수량: {holding_qty}주 | 평단: {account_avg_price:,.1f}원 | 진행단계: {state['current_step']}")

    # 3. 자체 치유 (Self-Healing) 로직
    if holding_qty == 0 and state["current_step"] > 0:
        logger.info("[자체 치유] 실제 잔고가 0주이므로 상태 정보를 초기화합니다.")
        state["current_step"] = 0
        state["entry_prices"] = []
        state["trailing_mode"] = False
        state["highest_price"] = 0
        save_state(state)

    elif holding_qty > 0 and state["current_step"] == 0:
        logger.info("[자체 치유] 잔고가 존재하나 단계가 0이므로 1단계로 강제 동기화합니다.")
        state["current_step"] = 1
        state["entry_prices"] = [int(account_avg_price)]
        save_state(state)

    # -------------------------------------------------------------
    # [A] 포지션 미보유 시: 1단계 신규 진입 (10% 비중)
    # -------------------------------------------------------------
    if state["current_step"] == 0:
        target_budget = total_asset * WEIGHTS[0]
        order_qty = int(target_budget // current_price)
        if order_qty > 0:
            logger.info(f"▶ [1단계 신규 진입] 예산: {target_budget:,.0f}원 | 수량: {order_qty}주 시장가 매수")
            if client.order_cash_buy(SYMBOL, order_qty):
                state["current_step"] = 1
                state["entry_prices"] = [current_price]
                state["trailing_mode"] = False
                state["highest_price"] = 0
                save_state(state)
        return

    # -------------------------------------------------------------
    # [B] 청산 매트릭스 판정 (이원화 핵심 로직)
    # -------------------------------------------------------------
    effective_avg = account_avg_price if account_avg_price > 0 else (sum(state["entry_prices"]) / len(state["entry_prices"]))

    # B-1. [1~2단계 보유 중]: 트레일링 스탑 가동 (+2.0% 진입 -> -1.0% 반락 시 익절)
    if state["current_step"] in [1, 2]:
        ts_trigger_price = effective_avg * (1 + TS_TRIGGER_RATE)
        
        # 트레일링 스탑 활성화 검사
        if not state["trailing_mode"] and current_price >= ts_trigger_price:
            logger.info(f"★ [트레일링 스탑 가동] 현재가({current_price:,}원)가 평단 대비 +2.0% 기준({ts_trigger_price:,.0f}원)을 돌파했습니다.")
            state["trailing_mode"] = True
            state["highest_price"] = current_price
            save_state(state)

        # 트레일링 스탑 추적 중
        if state["trailing_mode"]:
            if current_price > state["highest_price"]:
                state["highest_price"] = current_price
                save_state(state)
                logger.info(f"[*] 신고가 갱신: {current_price:,}원 (익절 감시가: {current_price * (1 - TS_FALLBACK_RATE):,.0f}원)")

            stop_price = state["highest_price"] * (1 - TS_FALLBACK_RATE)
            if current_price <= stop_price:
                logger.info(f"★ [트레일링 익절 체결] 최고가({state['highest_price']:,}원) 대비 -1.0% 하락({stop_price:,.0f}원 도달). 전량 매도!")
                if client.order_cash_sell(SYMBOL, holding_qty):
                    state["current_step"] = 0
                    state["entry_prices"] = []
                    state["trailing_mode"] = False
                    state["highest_price"] = 0
                    save_state(state)
                return

    # B-2. [3~4단계 보유 중]: 비상 본전 탈출 (평단가 대비 +0.05% 반등 시 전량 매도)
    elif state["current_step"] in [3, 4]:
        escape_price = effective_avg * (1 + ESCAPE_PROFIT_RATE)
        if current_price >= escape_price:
            logger.info(f"★ [비상 본전 탈출 체결] 현재가({current_price:,}원) >= 탈출목표({escape_price:,.0f}원, 평단+0.05%). 전량 무손실 청산!")
            if client.order_cash_sell(SYMBOL, holding_qty):
                state["current_step"] = 0
                state["entry_prices"] = []
                state["trailing_mode"] = False
                state["highest_price"] = 0
                save_state(state)
            return

    # -------------------------------------------------------------
    # [C] 단계별 추가 분할 매수 판정 (직전 체결가 대비 -2.8% 하락)
    # -------------------------------------------------------------
    if state["current_step"] < 4:
        last_price = state["entry_prices"][-1]
        next_trigger_price = last_price * (1 - STEP_DROP_RATE)

        if current_price <= next_trigger_price:
            next_step = state["current_step"] + 1
            target_budget = total_asset * WEIGHTS[next_step - 1]
            order_qty = int(target_budget // current_price)

            if order_qty > 0:
                logger.info(f"▼ [{next_step}단계 물타기 진입] 직전가({last_price:,}원) 대비 -2.8% 도달. 수량: {order_qty}주 시장가 매수")
                if client.order_cash_buy(SYMBOL, order_qty):
                    state["current_step"] = next_step
                    state["entry_prices"].append(current_price)
                    # 추가 매수 시 트레일링 모드는 재설정
                    state["trailing_mode"] = False
                    state["highest_price"] = 0
                    save_state(state)


if __name__ == "__main__":
    run_strategy_b()
