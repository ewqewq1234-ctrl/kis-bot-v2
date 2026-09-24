import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from nh_client import NHInvestClient

STATE_FILE = "state.json"
TICKER = "069500"  # KODEX 200

def load_state():
    default_state = {
        "initial_seed": 3000000,
        "total_capital": 3000000,
        "current_stage": 0,
        "shares": 0,
        "avg_price": 0.0,
        "last_buy_price": 0.0,
        "first_buy_date": "",
        "pyramid_mode": False,
        "pyramid_shares": 0,
        "highest_price": 0.0,
        "total_profit_accumulated": 0,
        "cycle_win_count": 0,
        "last_summary_date": "",
        "last_order_time": ""
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
                return default_state
        except Exception:
            pass
    return default_state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram_message(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[텔레그램 미설정]")
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
        print(f"[텔레그램 발송 오류]: {e}")

def send_telegram_alert(error_msg):
    """비상 상황 시 즉각 발송되는 경보 메시지"""
    text = (
        f"🚨 <b>[ETF 그리드봇 치명적 오류 경보]</b>\n"
        f"• 시각: {datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"• 오류 상세: <code>{error_msg}</code>\n"
        f"• 조치 요망: 상태 및 계좌 잔고를 수동 확인하세요."
    )
    send_telegram_message(text)

def check_and_send_summary(state, client, price, now_kst):
    """한국 시간 기준 15시 이후 장 마감 결산 브리핑"""
    today_str = now_kst.strftime("%Y-%m-%d")
    if now_kst.hour >= 15 and state.get("last_summary_date") != today_str:
        shares = state.get("shares", 0)
        avg_price = state.get("avg_price", 0.0)
        total_capital = state.get("total_capital", 3000000)
        
        eval_profit = (price - avg_price) * shares if shares > 0 else 0
        profit_rate = ((price / avg_price) - 1) * 100 if (shares > 0 and avg_price > 0) else 0.0
        target_price = avg_price * 1.02 if avg_price > 0 else 0.0

        msg = (
            f"📊 <b>[ETF 그리드봇 일일 결산 브리핑]</b>\n"
            f"• 기준 일자: {today_str}\n"
            f"• 종목: KODEX 200 ({price:,.0f}원)\n"
            f"• 현재 포지션: {state['current_stage']}단계 ({shares}주 보유)\n"
            f"• 보유 평단가: {avg_price:,.0f}원\n"
            f"• 평가 손익: {eval_profit:+,.0f}원 ({profit_rate:+.2f}%)\n"
            f"• 총 운용자본: {total_capital:,.0f}원\n"
            f"• 누적 실현손익: {state.get('total_profit_accumulated', 0):+,.0f}원\n"
            f"• 목표 익절가(+2%): {target_price:,.0f}원"
        )
        send_telegram_message(msg)
        state["last_summary_date"] = today_str
        save_state(state)
        print(f"[{today_str} 일일 브리핑 전송 완료]")

def execute_strategy():
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    today_str = now_kst.strftime("%Y-%m-%d")
    state = load_state()

    try:
        client = NHInvestClient()
        account_no = os.getenv("NH_ACCOUNT_NO")
        price = client.get_current_price(TICKER)

        # 1. 실제 증권사 잔고와 동기화 (잔고 정보가 정상 조회될 경우 보정)
        real_qty, real_avg = client.get_stock_balance(account_no, TICKER)
        if real_qty is not None:
            if real_qty > 0 and (state["shares"] != real_qty or abs(state["avg_price"] - real_avg) > 1.0):
                print(f"[잔고 동기화] state({state['shares']}주 / {state['avg_price']}원) -> 증권사 실제({real_qty}주 / {real_avg}원)")
                state["shares"] = real_qty
                if real_avg > 0:
                    state["avg_price"] = real_avg

        total_capital = state["total_capital"]
        stage_budgets = [
            total_capital * 0.10,
            total_capital * 0.20,
            total_capital * 0.30,
            total_capital * 0.40
        ]

        print(f"[{now_kst.strftime('%H:%M:%S')}] KODEX 200: {price:,.0f}원 | {state['current_stage']}단계 | 보유: {state['shares']}주")

        # 2. 불타기 트레일링 스탑 모드
        if state["pyramid_mode"]:
            if price > state["highest_price"]:
                state["highest_price"] = price

            if price <= state["highest_price"] * 0.99:
                sell_qty = state["pyramid_shares"]
                client.send_order(account_no, ticker=TICKER, qty=sell_qty, order_type="SELL")
                
                realized_profit = int((price - state["avg_price"]) * sell_qty)
                state["total_capital"] += realized_profit
                state["total_profit_accumulated"] += realized_profit

                msg = (
                    f"🔥 <b>[불타기 트레일링 청산 완료]</b>\n"
                    f"• 매도가: {price:,.0f}원 ({sell_qty}주)\n"
                    f"• 확정 이익: {realized_profit:+,.0f}원\n"
                    f"• 총 운용자본: {state['total_capital']:,.0f}원"
                )
                send_telegram_message(msg)

                state.update({
                    "shares": 0, "avg_price": 0.0, "last_buy_price": 0.0,
                    "current_stage": 0, "first_buy_date": "",
                    "pyramid_mode": False, "pyramid_shares": 0, "highest_price": 0.0
                })
                state["cycle_win_count"] += 1
                save_state(state)
                check_and_send_summary(state, client, price, now_kst)
                return

        # 3. 0단계 신규 1단계 매수 진입
        if state["current_stage"] == 0 and not state["pyramid_mode"]:
            qty = int(stage_budgets[0] // price)
            if qty == 0 and total_capital >= price:
                qty = 1

            if qty > 0:
                client.send_order(account_no, ticker=TICKER, qty=qty, order_type="BUY")
                
                # 주문 후 실제 체결 동기화
                time.sleep(2)
                act_qty, act_avg = client.get_stock_balance(account_no, TICKER)
                
                state["shares"] = act_qty if (act_qty and act_qty > 0) else qty
                state["avg_price"] = act_avg if (act_avg and act_avg > 0) else price
                state["last_buy_price"] = price
                state["current_stage"] = 1
                state["first_buy_date"] = today_str

                msg = (
                    f"🟢 <b>[1단계 신규 매수 체결]</b>\n"
                    f"• 배정 예산: {stage_budgets[0]:,.0f}원 (자본의 10%)\n"
                    f"• 매수가: {price:,.0f}원 ({state['shares']}주)\n"
                    f"• 총 운용자본: {total_capital:,.0f}원"
                )
                send_telegram_message(msg)
                save_state(state)
            check_and_send_summary(state, client, price, now_kst)
            return

        # 4. 보유 중 포지션 관리
        if state["shares"] > 0:
            gain_rate = (price / state["avg_price"]) - 1.0

            # 목표 수익률 +2.0% 도달 시 90% 분할 익절
            if gain_rate >= 0.02:
                sell_qty = int(state["shares"] * 0.9)
                if sell_qty == 0:
                    sell_qty = state["shares"]
                rem_qty = state["shares"] - sell_qty

                client.send_order(account_no, ticker=TICKER, qty=sell_qty, order_type="SELL")
                realized_profit = int((price - state["avg_price"]) * sell_qty)
                state["total_capital"] += realized_profit
                state["total_profit_accumulated"] += realized_profit

                msg = (
                    f"🎯 <b>[목표수익 도달 익절 (90%)]</b>\n"
                    f"• 익절가: {price:,.0f}원 (수익률: {gain_rate * 100:+.2f}%)\n"
                    f"• 매도 수량: {sell_qty}주 (실현손익: {realized_profit:+,.0f}원)\n"
                    f"• 총 운용자본: {state['total_capital']:,.0f}원"
                )
                send_telegram_message(msg)

                if rem_qty > 0:
                    state["pyramid_mode"] = True
                    state["pyramid_shares"] = rem_qty
                    state["highest_price"] = price
                    state["shares"] = rem_qty
                    state["current_stage"] = 0
                else:
                    state.update({
                        "shares": 0, "avg_price": 0.0, "last_buy_price": 0.0,
                        "current_stage": 0, "first_buy_date": ""
                    })
                    state["cycle_win_count"] += 1

                save_state(state)
                check_and_send_summary(state, client, price, now_kst)
                return

            # 4단계 풀매수 후 20일(약 1달) 경과 시 본전(+0.2%) 탈출
            if state["current_stage"] == 4 and state.get("first_buy_date"):
                first_date = datetime.strptime(state["first_buy_date"], "%Y-%m-%d").date()
                curr_date = now_kst.date()
                if (curr_date - first_date).days >= 28:  # 달력 기준 약 4주(20영업일)
                    if price >= state["avg_price"] * 1.002:
                        client.send_order(account_no, ticker=TICKER, qty=state["shares"], order_type="SELL")
                        realized_profit = int((price - state["avg_price"]) * state["shares"])
                        state["total_capital"] += realized_profit
                        state["total_profit_accumulated"] += realized_profit

                        msg = (
                            f"🛡️ <b>[20영업일 경과 본전 탈출 청산]</b>\n"
                            f"• 탈출가: {price:,.0f}원 ({state['shares']}주 전량)\n"
                            f"• 실현손익: {realized_profit:+,.0f}원\n"
                            f"• 총 운용자본: {state['total_capital']:,.0f}원"
                        )
                        send_telegram_message(msg)
                        state.update({
                            "shares": 0, "avg_price": 0.0, "last_buy_price": 0.0,
                            "current_stage": 0, "first_buy_date": ""
                        })
                        save_state(state)
                        check_and_send_summary(state, client, price, now_kst)
                        return

            # 직전 매수가 대비 -1.2% 하락 시 단계별 물타기 매수
            dip_rate = (price / state["last_buy_price"]) - 1.0
            next_stage = state["current_stage"] + 1

            if dip_rate <= -0.012 and next_stage <= 4:
                budget = stage_budgets[next_stage - 1]
                add_qty = int(budget // price)
                if add_qty == 0 and total_capital >= price:
                    add_qty = 1

                if add_qty > 0:
                    client.send_order(account_no, ticker=TICKER, qty=add_qty, order_type="BUY")
                    
                    time.sleep(2)
                    act_qty, act_avg = client.get_stock_balance(account_no, TICKER)

                    total_shares = act_qty if (act_qty and act_qty > 0) else (state["shares"] + add_qty)
                    new_avg = act_avg if (act_avg and act_avg > 0) else (((state["shares"] * state["avg_price"]) + (add_qty * price)) / total_shares)

                    state["shares"] = total_shares
                    state["avg_price"] = new_avg
                    state["last_buy_price"] = price
                    state["current_stage"] = next_stage

                    msg = (
                        f"🔵 <b>[{next_stage}단계 물타기 체결]</b>\n"
                        f"• 투입 예산: {budget:,.0f}원 (누적 {total_shares}주)\n"
                        f"• 신규 평단가: {new_avg:,.0f}원 (직전 대비 {dip_rate * 100:+.2f}%)\n"
                        f"• 목표 익절가(+2%): {new_avg * 1.02:,.0f}원"
                    )
                    send_telegram_message(msg)
                    save_state(state)

        # 5. 장마감 결산 체크
        check_and_send_summary(state, client, price, now_kst)
        save_state(state)

    except Exception as e:
        err_msg = str(e)
        print(f"[실행 오류]: {err_msg}")
        send_telegram_alert(err_msg)
        raise

if __name__ == "__main__":
    execute_strategy()
