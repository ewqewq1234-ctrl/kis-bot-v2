import os
import json
import requests
from datetime import datetime
from nh_client import NHInvestClient

STATE_FILE = "state.json"

def send_telegram_message(text):
    """텔레그램 메시지 전송 유틸리티"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[텔레그램 전송 실패]: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    # 기본 초기 상태 (초기 자본 300만원 기준)
    return {
        "initial_seed": 3000000,
        "total_capital": 3000000,
        "current_stage": 0,
        "shares": 0,
        "avg_price": 0.0,
        "last_buy_price": 0.0,
        "holding_bars": 0,
        "pyramid_mode": False,
        "pyramid_shares": 0,
        "highest_price": 0.0,
        "total_profit_accumulated": 0,
        "cycle_win_count": 0,
        "last_summary_date": ""
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def execute_strategy():
    state = load_state()
    client = NHInvestClient()
    account_no = os.getenv("NH_ACCOUNT_NO")

    # 1. 자동 복리: 현재 총 운용자본(수익금 누적분 포함) 기준 10/20/30/40% 동적 산출
    total_capital = state.get("total_capital", 3000000)
    stage_budgets = [
        int(total_capital * 0.10),
        int(total_capital * 0.20),
        int(total_capital * 0.30),
        int(total_capital * 0.40)
    ]

    price = client.get_current_price("069500")  # KODEX 200
    if price <= 0:
        print("현재가 조회 실패")
        return

    now = datetime.now()
    print(f"[{now.strftime('%H:%M:%S')}] KODEX 200: {price:,.0f}원 | {state['current_stage']}단계 | 총자본: {total_capital:,.0f}원")

    # ----------------------------------------------------
    # 전략 매매 로직
    # ----------------------------------------------------
    # 1단계: 미보유 상태 최초 진입 (동적 10% 예산)
    if state["current_stage"] == 0 and not state["pyramid_mode"]:
        qty = int(stage_budgets[0] // price)
        if qty > 0:
            client.send_order(account_no, qty=qty, order_type="BUY")
            state["shares"] = qty
            state["avg_price"] = price
            state["last_buy_price"] = price
            state["current_stage"] = 1
            state["holding_bars"] = 0
            
            msg = (
                f"🟢 <b>[1단계 신규 매수 (자동복리)]</b>\n"
                f"• 배정 예산: {stage_budgets[0]:,.0f}원 (자본의 10%)\n"
                f"• 매수가: {price:,.0f}원 ({qty}주)\n"
                f"• 총 운용자본: {total_capital:,.0f}원"
            )
            send_telegram_message(msg)
            save_state(state)
        return

    # 불타기(10% 잔여 물량) 트레일링 스탑
    if state["pyramid_mode"]:
        if price > state["highest_price"]:
            state["highest_price"] = price
        drop = (price - state["highest_price"]) / state["highest_price"]
        if drop <= -0.01:
            profit = int((price - state["avg_price"]) * state["pyramid_shares"])
            client.send_order(account_no, qty=state["pyramid_shares"], order_type="SELL")
            
            state["total_capital"] += profit
            state["total_profit_accumulated"] += profit
            
            msg = (
                f"🔥 <b>[불타기 트레일링 전량 청산]</b>\n"
                f"• 청산가: {price:,.0f}원 ({state['pyramid_shares']}주)\n"
                f"• 불타기 실현손익: {profit:+,.0f}원\n"
                f"• 복리 반영 총자본: {state['total_capital']:,.0f}원"
            )
            send_telegram_message(msg)

            state["pyramid_mode"] = False
            state["pyramid_shares"] = 0
            state["current_stage"] = 0
            save_state(state)
        return

    # 4단계 풀매수 시간 누적 (20영업일/140봉 동결 탈출용)
    if state["current_stage"] == 4:
        state["holding_bars"] += 1

    return_rate = (price - state["avg_price"]) / state["avg_price"]
    # 4단계 풀매수 후 140시간(20영업일) 경과 시 평단가 본전(+0.05%) 탈출
    target = 0.0005 if (state["current_stage"] == 4 and state["holding_bars"] >= 140) else 0.02

    # 익절 조건 도달
    if return_rate >= target:
        sell_qty = int(state["shares"] * 0.90)
        rem_qty = state["shares"] - sell_qty
        
        # 실현 이익금 계산 및 복리 자본 합산
        realized_profit = int((price - state["avg_price"]) * sell_qty)
        state["total_capital"] += realized_profit
        state["total_profit_accumulated"] += realized_profit
        state["cycle_win_count"] = state.get("cycle_win_count", 0) + 1

        client.send_order(account_no, qty=sell_qty, order_type="SELL")

        is_break_even = (target == 0.0005)
        msg_title = "🛡️ <b>[20영업일 경과 본전 탈출]</b>" if is_break_even else "🎯 <b>[목표수익 도달 익절 (90%)]</b>"
        msg = (
            f"{msg_title}\n"
            f"• 매도가: {price:,.0f}원 (수익률: {return_rate*100:+.2f}%)\n"
            f"• 매도 수량: {sell_qty}주 (잔여 불타기: {rem_qty}주)\n"
            f"• 실현 손익: <b>{realized_profit:+,.0f}원</b>\n"
            f"• <b>다음 사이클 복리 총자본: {state['total_capital']:,.0f}원</b>"
        )
        send_telegram_message(msg)

        if rem_qty > 0 and not is_break_even:
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

    # 물타기: 직전 매수가 대비 -1.2% 하락 시
    dip = (price - state["last_buy_price"]) / state["last_buy_price"]
    if state["current_stage"] < 4 and dip <= -0.012:
        next_stage = state["current_stage"] + 1
        qty = int(stage_budgets[next_stage - 1] // price)
        if qty > 0:
            client.send_order(account_no, qty=qty, order_type="BUY")
            total_val = (state["shares"] * state["avg_price"]) + (qty * price)
            state["shares"] += qty
            state["avg_price"] = total_val / state["shares"]
            state["last_buy_price"] = price
            state["current_stage"] = next_stage

            msg = (
                f"🔵 <b>[{next_stage}단계 물타기 체결]</b>\n"
                f"• 투입 예산: {stage_budgets[next_stage - 1]:,.0f}원 (누적 {state['shares']}주)\n"
                f"• 신규 평단가: {state['avg_price']:,.0f}원 (직전 대비 {dip*100:.2f}%)\n"
                f"• 목표 익절가(+2%): {state['avg_price']*1.02:,.0f}원"
            )
            send_telegram_message(msg)
            save_state(state)

    # ----------------------------------------------------
    # 텔레그램 하루 1회 종합 브리핑 (오후 15시 봉 실행 시)
    # ----------------------------------------------------
    today_str = now.strftime("%Y-%m-%d")
    # 오후 15시 이후이면서 오늘 아직 일일 요약을 보내지 않은 경우 발송
    if now.hour >= 15 and state.get("last_summary_date") != today_str:
        initial_seed = state.get("initial_seed", 3000000)
        tot_profit = state.get("total_profit_accumulated", 0)
        roi = (state["total_capital"] - initial_seed) / initial_seed * 100

        # 평가 손익 추정
        eval_profit = 0
        if state["shares"] > 0:
            eval_profit = int((price - state["avg_price"]) * state["shares"])

        report = (
            f"📊 <b>[ETF 그리드봇 일일 결산 브리핑]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• 일자: {today_str}\n"
            f"• KODEX 200 종가: {price:,.0f}원\n"
            f"• 현재 진행 상태: <b>{state['current_stage']}단계</b>\n"
            f"• 보유 주식 수: {state['shares']}주\n"
            f"• 보유 평단가: {state['avg_price']:,.0f}원\n"
            f"• 현재 평가손익: {eval_profit:+,.0f}원\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"• 초기 투자원금: {initial_seed:,.0f}원\n"
            f"• 누적 확정손익: <b>{tot_profit:+,.0f}원</b>\n"
            f"• <b>총 운용 자본(복리): {state['total_capital']:,.0f}원</b>\n"
            f"• 누적 누적수익률: <b>{roi:+.2f}%</b> (익절 {state.get('cycle_win_count', 0)}회)\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        send_telegram_message(report)
        state["last_summary_date"] = today_str

    save_state(state)

if __name__ == "__main__":
    execute_strategy()
