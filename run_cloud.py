import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from nh_client import NHInvestClient

STATE_FILE = "state.json"
TICKER = "069500"  # KODEX 200

# 전략 B 확정 파라미터
DIP_INTERVAL = -0.028          # 추가 매수(물타기) 간격: 직전 매수가 대비 -2.8%
STAGE_1_2_TARGET = 0.020       # 1~2단계 목표 익절선: 평단가 대비 +2.0%
TRAILING_STOP_DROP = 0.010     # 트레일링 스탑 낙폭: 최고가 대비 -1.0% 하락 시 청산
BREAK_EVEN_TARGET = 0.0005     # 3~4단계 비상 본전 탈출선: 평단가 대비 +0.05% (제비용 보전)

def load_state():
    default_state = {
        "initial_seed": 3000000,
        "total_capital": 3000000,
        "current_stage": 0,
        "shares": 0,
        "avg_price": 0.0,
        "last_buy_price": 0.0,
        "first_buy_date": "",
        "trailing_mode": False,
        "highest_price": 0.0,
        "total_profit_accumulated": 0,
        "cycle_win_count": 0,
        "cycle_draw_count": 0,
        "last_summary_date": ""
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
    kst = timezone(timedelta(hours=9))
    text = (
        f"🚨 <b>[전략 B 비상 경보]</b>\n"
        f"• 시각: {datetime.now(kst).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"• 에러: <code>{error_msg}</code>\n"
        f"• 확인 후 조치 바랍니다."
    )
    send_telegram_message(text)

def check_and_send_summary(state, client, price, now_kst):
    """15시 이후 장마감 일일 브리핑 (1일 1회)"""
    today_str = now_kst.strftime("%Y-%m-%d")
    if now_kst.hour >= 15 and state.get("last_summary_date") != today_str:
        shares = state.get("shares", 0)
        avg_price = state.get("avg_price", 0.0)
        total_capital = state.get("total_capital", 3000000)

        eval_profit = (price - avg_price) * shares if shares > 0 else 0
        profit_rate = ((price / avg_price) - 1) * 100 if (shares > 0 and avg_price > 0) else 0.0
        
        stage = state.get("current_stage", 0)
        if stage in [1, 2]:
            target_desc = f"+2.0% 감시가 ({avg_price * (1 + STAGE_1_2_TARGET):,.0f}원)"
        elif stage in [3, 4]:
            target_desc = f"+0.05% 본전탈출가 ({avg_price * (1 + BREAK_EVEN_TARGET):,.0f}원)"
        else:
            target_desc = "진입 대기"

        msg = (
            f"📊 <b>[전략 B 일일 결산 브리핑]</b>\n"
            f"• 일자: {today_str}\n"
            f"• KODEX 200: {price:,.0f}원\n"
            f"• 포지션: {stage}단계 ({shares}주)\n"
            f"• 매입 평단: {avg_price:,.0f}원\n"
            f"• 평가 손익: {eval_profit:+,.0f}원 ({profit_rate:+.2f}%)\n"
            f"• 운용 자본: {total_capital:,.0f}원\n"
            f"• 누적 실현손익: {state.get('total_profit_accumulated', 0):+,.0f}원\n"
            f"• 전적: {state.get('cycle_win_count', 0)}승 {state.get('cycle_draw_count', 0)}무\n"
            f"• 목표 청산선: {target_desc}"
        )
        state["last_summary_date"] = today_str
        save_state(state)
        send_telegram_message(msg)

def reset_position(state):
    """사이클 종료 시 상태 초기화 공통 함수"""
    state.update({
        "shares": 0,
        "avg_price": 0.0,
        "last_buy_price": 0.0,
        "current_stage": 0,
        "first_buy_date": "",
        "trailing_mode": False,
        "highest_price": 0.0
    })

def execute_strategy():
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    today_str = now_kst.strftime("%Y-%m-%d")
    state = load_state()

    try:
        client = NHInvestClient()
        account_no = os.getenv("NH_ACCOUNT_NO")
        price = client.get_current_price(TICKER)

        # 1. 증권사 실제 계좌 자산현황 실시간 동기화
        cash, real_qty, real_avg, net_assets = client.get_account_assets(account_no, TICKER)
        if cash is not None:
            if net_assets > 0:
                state["total_capital"] = net_assets
            else:
                total_equity = cash + (real_qty * price)
                if total_equity > 0:
                    state["total_capital"] = total_equity

            # 실제 잔고 수량과 평단가 동기화
            if real_qty != state["shares"] or (real_qty > 0 and abs(state["avg_price"] - real_avg) > 1.0):
                print(f"[자산 동기화] state({state['shares']}주/{state['avg_price']}원) -> 계좌실제({real_qty}주/{real_avg}원)")
                state["shares"] = real_qty
                if real_avg > 0:
                    state["avg_price"] = real_avg

            # 실제 보유 중인데 봇 내부 단계가 0이면 1단계로 안전 보정
            if real_qty > 0 and state["current_stage"] == 0:
                state["current_stage"] = 1
                if not state.get("first_buy_date"):
                    state["first_buy_date"] = today_str
                if state.get("last_buy_price", 0.0) == 0.0:
                    state["last_buy_price"] = real_avg or price
                save_state(state)

            # 실제 잔고가 0인데 단계가 남아있는 경우 리셋
            if real_qty == 0 and state["current_stage"] > 0:
                print(f"[상태 정상화] 실제 잔고 0주 확인 -> 포지션 리셋")
                reset_position(state)
                save_state(state)

        total_capital = state["total_capital"]

        # 4단계 가중 분할 예산 배분 (10% : 20% : 30% : 40%)
        stage_budgets = [
            total_capital * 0.10,
            total_capital * 0.20,
            total_capital * 0.30,
            total_capital * 0.40
        ]

        print(f"[{now_kst.strftime('%H:%M:%S')}] KODEX 200: {price:,.0f}원 | {state['current_stage']}단계 | 보유: {state['shares']}주 | 총자산: {total_capital:,.0f}원")

        # 2. 1~2단계 트레일링 스탑 감시 및 청산
        if state["trailing_mode"] and state["shares"] > 0:
            if price > state["highest_price"]:
                state["highest_price"] = price

            # 당일 최고가 대비 -1.0% 하락 시 전량 이익 실현
            if price <= state["highest_price"] * (1 - TRAILING_STOP_DROP):
                sell_qty = state["shares"]
                client.send_order(account_no, ticker=TICKER, qty=sell_qty, order_type="SELL")

                realized_profit = int((price - state["avg_price"]) * sell_qty)
                state["total_profit_accumulated"] += realized_profit
                state["cycle_win_count"] += 1
                reset_position(state)
                save_state(state)

                msg = (
                    f"🎉 <b>[1~2단계 트레일링 스탑 익절 완료]</b>\n"
                    f"• 청산가: {price:,.0f}원 ({sell_qty}주 전량 매도)\n"
                    f"• 최고 도달가: {state['highest_price']:,.0f}원\n"
                    f"• 실현 이익: {realized_profit:+,.0f}원\n"
                    f"• 누적 전적: {state['cycle_win_count']}승 {state['cycle_draw_count']}무"
                )
                send_telegram_message(msg)
                check_and_send_summary(state, client, price, now_kst)
                return

        # 3. 0단계 신규 진입 (1단계 10% 매수)
        if state["current_stage"] == 0 and state["shares"] == 0:
            qty = int(stage_budgets[0] // price)
            if qty == 0 and total_capital >= price:
                qty = 1

            if qty > 0:
                client.send_order(account_no, ticker=TICKER, qty=qty, order_type="BUY")
                time.sleep(2)
                _, act_qty, act_avg, _ = client.get_account_assets(account_no, TICKER)

                state["shares"] = act_qty if (act_qty and act_qty > 0) else qty
                state["avg_price"] = act_avg if (act_avg and act_avg > 0) else price
                state["last_buy_price"] = price
                state["current_stage"] = 1
                state["first_buy_date"] = today_str
                save_state(state)

                msg = (
                    f"🟢 <b>[전략 B 1단계 신규 매수 진입]</b>\n"
                    f"• 배정 예산: {stage_budgets[0]:,.0f}원 (10%)\n"
                    f"• 체결가: {price:,.0f}원 ({state['shares']}주)\n"
                    f"• 운용 자본: {total_capital:,.0f}원"
                )
                send_telegram_message(msg)
            check_and_send_summary(state, client, price, now_kst)
            return

        # 4. 보유 중 포지션 관리 및 청산 매트릭스
        if state["shares"] > 0:
            gain_rate = (price / state["avg_price"]) - 1.0

            # [청산 매트릭스 ①] 1~2단계 보유 중일 때: +2.0% 터치 시 트레일링 스탑 가동
            if state["current_stage"] in [1, 2]:
                if not state["trailing_mode"] and gain_rate >= STAGE_1_2_TARGET:
                    state["trailing_mode"] = True
                    state["highest_price"] = price
                    save_state(state)

                    msg = (
                        f"🎯 <b>[1~2단계 익절 목표(+2.0%) 도달 ➔ 트레일링 스탑 가동]</b>\n"
                        f"• 현재가: {price:,.0f}원 (수익률: {gain_rate * 100:+.2f}%)\n"
                        f"• 최고가 대비 -1.0% 하락 시 전량 이익 실현 대기"
                    )
                    send_telegram_message(msg)

            # [청산 매트릭스 ②] 3~4단계 진입 시: +0.05% 도달 시 즉시 전량 본전 탈출
            elif state["current_stage"] in [3, 4]:
                if gain_rate >= BREAK_EVEN_TARGET:
                    sell_qty = state["shares"]
                    client.send_order(account_no, ticker=TICKER, qty=sell_qty, order_type="SELL")

                    realized_profit = int((price - state["avg_price"]) * sell_qty)
                    state["total_profit_accumulated"] += realized_profit
                    state["cycle_draw_count"] += 1
                    reset_position(state)
                    save_state(state)

                    msg = (
                        f"🛡️ <b>[3~4단계 위기 회피: 수수료 제외 본전 탈출 완료]</b>\n"
                        f"• 청산가: {price:,.0f}원 ({sell_qty}주 전량)\n"
                        f"• 수익률: {gain_rate * 100:+.2f}%\n"
                        f"• 손익: {realized_profit:+,.0f}원 (원금 100% 보존)\n"
                        f"• 누적 전적: {state['cycle_win_count']}승 {state['cycle_draw_count']}무 0패"
                    )
                    send_telegram_message(msg)
                    check_and_send_summary(state, client, price, now_kst)
                    return

            # [추가 매수(물타기)] 직전 매수가 대비 -2.8% 하락 시 다음 단계 가중 매수
            dip_rate = (price / state["last_buy_price"]) - 1.0
            next_stage = state["current_stage"] + 1

            if dip_rate <= DIP_INTERVAL and next_stage <= 4:
                budget = stage_budgets[next_stage - 1]
                add_qty = int(budget // price)
                if add_qty == 0 and total_capital >= price:
                    add_qty = 1

                if add_qty > 0:
                    client.send_order(account_no, ticker=TICKER, qty=add_qty, order_type="BUY")
                    time.sleep(2)
                    _, act_qty, act_avg, _ = client.get_account_assets(account_no, TICKER)

                    total_shares = act_qty if (act_qty and act_qty > state["shares"]) else (state["shares"] + add_qty)
                    new_avg = act_avg if (act_avg and act_avg > 0) else (((state["shares"] * state["avg_price"]) + (add_qty * price)) / total_shares)

                    state["shares"] = total_shares
                    state["avg_price"] = new_avg
                    state["last_buy_price"] = price
                    state["current_stage"] = next_stage
                    state["trailing_mode"] = False  # 물타기 진행 시 트레일링 모드 리셋
                    save_state(state)

                    stage_desc = "최종 풀베팅" if next_stage == 4 else f"{next_stage}차 추가매수"
                    msg = (
                        f"🔵 <b>[전략 B {next_stage}단계 ({stage_desc}) 체결]</b>\n"
                        f"• 추가 매수: {add_qty}주 (예산 {budget:,.0f}원)\n"
                        f"• 누적 보유: {total_shares}주\n"
                        f"• 밀착 평단: {new_avg:,.0f}원 (직전 대비 {dip_rate * 100:+.2f}%)\n"
                        f"• 다음 청산 목표: {'+0.05% 본전 탈출' if next_stage >= 3 else '+2.0% 트레일링'}"
                    )
                    send_telegram_message(msg)

        # 5. 장마감 결산 체크 및 최종 상태 저장
        check_and_send_summary(state, client, price, now_kst)
        save_state(state)

    except Exception as e:
        err_msg = str(e)
        print(f"[전략 실행 오류]: {err_msg}")
        send_telegram_alert(err_msg)
        raise

if __name__ == "__main__":
    execute_strategy()
