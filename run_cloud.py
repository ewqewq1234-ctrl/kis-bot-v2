import os
import json
from datetime import datetime
from nh_client import NHInvestClient

STATE_FILE = "state.json"

def load_state():
    """이전 매매 상태 불러오기"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    # 기본 초기 상태 (자본 300만원 분할 기준)
    return {
        "current_stage": 0,
        "shares": 0,
        "avg_price": 0.0,
        "last_buy_price": 0.0,
        "holding_bars": 0,
        "pyramid_mode": False,
        "pyramid_shares": 0,
        "highest_price": 0.0
    }

def save_state(state):
    """현재 매매 상태 파일에 저장"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def execute_strategy():
    state = load_state()
    client = NHInvestClient()
    account_no = os.getenv("NH_ACCOUNT_NO")
    stage_budgets = [300000, 600000, 900000, 1200000]

    price = client.get_current_price("069500") # KODEX 200
    if price <= 0:
        print("현재가 조회 실패")
        return

    print(f"현재 KODEX 200 가격: {price:,.0f}원 | 현재 단계: {state['current_stage']}단계")

    # 1. 최초 진입 (1단계: 30만 원)
    if state["current_stage"] == 0 and not state["pyramid_mode"]:
        qty = int(stage_budgets[0] // price)
        if qty > 0:
            print(f"[클라우드 매수] 1단계 진입: {qty}주 매수")
            client.send_order(account_no, qty=qty, order_type="BUY")
            state["shares"] = qty
            state["avg_price"] = price
            state["last_buy_price"] = price
            state["current_stage"] = 1
            state["holding_bars"] = 0
            save_state(state)
        return

    # 2. 불타기(10% 물량) 운용 중
    if state["pyramid_mode"]:
        if price > state["highest_price"]:
            state["highest_price"] = price
        drop = (price - state["highest_price"]) / state["highest_price"]
        if drop <= -0.01:
            print(f"[클라우드 청산] 불타기 최고점 대비 -1% 도달: {state['pyramid_shares']}주 매도")
            client.send_order(account_no, qty=state["pyramid_shares"], order_type="SELL")
            state["pyramid_mode"] = False
            state["pyramid_shares"] = 0
            state["current_stage"] = 0
            save_state(state)
        return

    # 3. 4단계 풀매수 시간 누적 (동결 해제 장치)
    if state["current_stage"] == 4:
        state["holding_bars"] += 1

    return_rate = (price - state["avg_price"]) / state["avg_price"]
    # 4단계 풀매수 후 140시간(20영업일) 경과 시 평단가 본전 탈출(+0.05%), 평소엔 +2.0%
    target = 0.0005 if (state["current_stage"] == 4 and state["holding_bars"] >= 140) else 0.02

    # 4. 익절 실행 (90% 매도, 10% 불타기 보존)
    if return_rate >= target:
        sell_qty = int(state["shares"] * 0.90)
        rem_qty = state["shares"] - sell_qty
        print(f"[클라우드 익절] 목표 도달({return_rate*100:.2f}%): 90%({sell_qty}주) 매도")
        client.send_order(account_no, qty=sell_qty, order_type="SELL")

        if rem_qty > 0 and target == 0.02:
            state["pyramid_mode"] = True
            state["pyramid_shares"] = rem_qty
            state["highest_price"] = price
        else:
            state["current_stage"] = 0
            state["pyramid_mode"] = False

        state["shares"] = 0
        state["holding_bars"] = 0
        save_state(state)
        return

    # 5. 물타기 (직전가 대비 -1.2% 하락)
    dip = (price - state["last_buy_price"]) / state["last_buy_price"]
    if state["current_stage"] < 4 and dip <= -0.012:
        next_stage = state["current_stage"] + 1
        qty = int(stage_budgets[next_stage - 1] // price)
        if qty > 0:
            print(f"[클라우드 물타기] {next_stage}단계 추가 매수: {qty}주")
            client.send_order(account_no, qty=qty, order_type="BUY")
            total_cost = (state["shares"] * state["avg_price"]) + (qty * price)
            state["shares"] += qty
            state["avg_price"] = total_cost / state["shares"]
            state["last_buy_price"] = price
            state["current_stage"] = next_stage
            save_state(state)

    # 변동 없어도 경과 시간(holding_bars 등) 갱신 저장
    save_state(state)

if __name__ == "__main__":
    execute_strategy()
