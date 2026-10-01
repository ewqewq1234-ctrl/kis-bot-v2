import os
import sys
import time
import math
import logging
from typing import List, Tuple
from dotenv import load_dotenv

# 1. nhplug SDK 공식 호출 인터페이스 임포트
try:
    from nhplug import call
except ImportError:
    print("[에러] nhplug 패키지가 설치되어 있지 않거나 경로에 없습니다.")
    print("실행 방법: pip install nhplug 또는 PYTHONPATH=. python <파일명>.py")
    sys.exit(1)

# 환경 변수 로드 (.env의 NHPLUG_APP_KEY, NHPLUG_APP_SECRET 등)
load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("strategy_kospi200.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("Kospi200Bot")


class Kospi200AsymmetricBot:
    """
    코스피 200 ETF 비대칭 가중 분할 매수(10:20:30:40) & 본전(+0.05%) 탈출 전략 봇
    """
    def __init__(self, account_no: str, symbol: str = "069500", total_capital: float = 10_000_000):
        self.account_no = account_no
        self.symbol = symbol                      # 기본값: KODEX 200 (069500)
        self.total_capital = total_capital        # 총 할당 운용 자본금
        
        # 전략 핵심 파라미터
        self.weights = [0.10, 0.20, 0.30, 0.40]  # 4단계 비대칭 가중 비중
        self.step_drop_rate = 0.028              # 단계별 직전 진입가 대비 낙폭 (-2.8%)
        self.target_profit_rate = 0.0005         # 본전 탈출 목표 반등률 (+0.05%)
        
        # 포지션 관리 상태 변수 [(step, entry_price, qty), ...]
        self.history: List[Tuple[int, int, int]] = []
        self.current_step = 0

    # -------------------------------------------------------------------------
    # [1] NHPLUG Open API 호출 래퍼 (공식 URI 엔드포인트 방식)
    # -------------------------------------------------------------------------
    def get_current_price(self) -> int:
        """현재가 조회: /krstock/quote/v1/currentPrice"""
        try:
            res = call("/krstock/quote/v1/currentPrice", {
                "iem_cd": self.symbol,
                "market_cd": "KRX"
            })
            # 응답 필드 파싱 (stck_prpr 또는 prpr)
            price_str = res.get("stck_prpr") or res.get("prpr") or res.get("price")
            if price_str:
                return int(price_str)
            raise ValueError(f"현재가 응답 이상: {res}")
        except Exception as e:
            logger.error(f"시세 조회 실패: {e}")
            return 0

    def order_buy(self, price: int, qty: int) -> bool:
        """현금 매수 주문: /krstock/order/v1/buy"""
        if qty <= 0:
            return False
        try:
            payload = {
                "acno": self.account_no,
                "iem_cd": self.symbol,
                "ord_dvsn": "00",          # 00: 지정가 (01: 시장가)
                "ord_qty": str(qty),
                "ord_unpr": str(price)
            }
            res = call("/krstock/order/v1/buy", payload)
            logger.info(f"==> [매수 주문 접수 완료] 응답: {res}")
            return True
        except Exception as e:
            logger.error(f"매수 주문 실패: {e}")
            return False

    def order_sell(self, price: int, qty: int) -> bool:
        """현금 매도 주문: /krstock/order/v1/sell"""
        if qty <= 0:
            return False
        try:
            payload = {
                "acno": self.account_no,
                "iem_cd": self.symbol,
                "ord_dvsn": "00",          # 00: 지정가
                "ord_qty": str(qty),
                "ord_unpr": str(price)
            }
            res = call("/krstock/order/v1/sell", payload)
            logger.info(f"==> [매도 주문 접수 완료] 응답: {res}")
            return True
        except Exception as e:
            logger.error(f"매도 주문 실패: {e}")
            return False

    # -------------------------------------------------------------------------
    # [2] 수학적 보정 및 가중 평단가 계산
    # -------------------------------------------------------------------------
    @staticmethod
    def adjust_tick(price: float) -> int:
        """
        국내 ETF 호가 틱 단위 보정 (대부분의 코스피 ETF는 5원 단위)
        증권사 호가 불일치 주문거부 방지
        """
        return int(math.floor(price / 5.0) * 5)

    def calculate_portfolio_avg(self) -> float:
        """보유 수량 가중 실제 체결 평단가 (조화평균 효과 반영)"""
        total_spent = sum(price * qty for _, price, qty in self.history)
        total_qty = sum(qty for _, _, qty in self.history)
        return (total_spent / total_qty) if total_qty > 0 else 0.0

    # -------------------------------------------------------------------------
    # [3] 단계별 매수 집행
    # -------------------------------------------------------------------------
    def execute_step_entry(self, step: int, price: int):
        fund_allocated = self.total_capital * self.weights[step - 1]
        order_price = self.adjust_tick(price)
        order_qty = int(fund_allocated // order_price)

        if order_qty <= 0:
            logger.warning(f"[{step}단계] 배정 자본금({fund_allocated:,.0f}원) 대비 수량이 0주입니다. 주문 스킵.")
            return

        logger.info(f"★ [{step}단계 진입 주문] 단가: {order_price:,}원 | 수량: {order_qty:,}주 | 투입금: {order_price * order_qty:,}원")
        
        # 실주문 전송
        if self.order_buy(order_price, order_qty):
            self.history.append((step, order_price, order_qty))
            self.current_step = step
            
            avg_p = self.calculate_portfolio_avg()
            total_q = sum(q for _, _, q in self.history)
            logger.info(f"[*] 체결 후 상태: 총 {total_q:,}주 보유 | 현재 계좌 가중 평단가: {avg_p:,.2f}원")

    # -------------------------------------------------------------------------
    # [4] 메인 트레이딩 감시 엔진
    # -------------------------------------------------------------------------
    def run(self):
        logger.info("=" * 60)
        logger.info(f"나무증권 봇 가동 - 대상 종목: {self.symbol} | 총 운용액: {self.total_capital:,}원")
        logger.info("비대칭 비중: 10% -> 20% -> 30% -> 40% (단계별 -2.8% 하락 시 추가 매수)")
        logger.info("본전 탈출 목표: 계좌 평단가 대비 +0.05% 반등 시 전량 청산")
        logger.info("=" * 60)

        # 1. 1단계 최초 진입
        p1 = self.get_current_price()
        while p1 <= 0:
            logger.warning("초기 현재가 수신 대기 중...")
            time.sleep(2)
            p1 = self.get_current_price()

        logger.info(f"[시작] 기준 가격 확인: {p1:,}원. 1단계 즉시 진입을 시작합니다.")
        self.execute_step_entry(step=1, price=p1)

        # 2. 실시간 모니터링 루프
        while True:
            try:
                time.sleep(1)  # 1초 주기 감시 (증권사 Rate Limit 준수)
                cur_price = self.get_current_price()
                if cur_price <= 0:
                    continue

                total_qty = sum(q for _, _, q in self.history)
                avg_price = self.calculate_portfolio_avg()

                # --- A. 본전 탈출 조건 (+0.05% 기술적 반등) ---
                target_sell_price = self.adjust_tick(avg_price * (1 + self.target_profit_rate))
                if cur_price >= target_sell_price and total_qty > 0:
                    logger.info("!" * 60)
                    logger.info(f"[본전 탈출 트리거] 현재가({cur_price:,}원) >= 탈출목표가({target_sell_price:,}원)")
                    logger.info(f"계좌 평단가({avg_price:.2f}원) 대비 무손실 탈출을 위해 전량({total_qty:,}주) 매도를 집행합니다.")
                    logger.info("!" * 60)

                    self.order_sell(cur_price, total_qty)
                    logger.info("[완료] 본전 탈출 성공. 매매 사이클을 안전하게 종료합니다.")
                    break

                # --- B. 추가 단계 진입 조건 (직전 체결가 대비 -2.8% 하락) ---
                if self.current_step < 4:
                    last_step, last_entry_price, _ = self.history[-1]
                    trigger_price = self.adjust_tick(last_entry_price * (1 - self.step_drop_rate))

                    if cur_price <= trigger_price:
                        next_step = self.current_step + 1
                        logger.info(f"▼ 추가 낙폭(-2.8%) 발생: 현재가({cur_price:,}원) <= {next_step}단계 기준가({trigger_price:,}원)")
                        self.execute_step_entry(step=next_step, price=cur_price)

            except KeyboardInterrupt:
                logger.warning("사용자에 의해 매매 루프가 중단되었습니다.")
                break
            except Exception as e:
                logger.error(f"루프 실행 중 예외 발생: {e}")
                time.sleep(3)


if __name__ == "__main__":
    # 실행 시 본인의 계좌번호와 운용 자본금을 입력하세요.
    ACCOUNT_NUMBER = os.getenv("NHPLUG_ACCOUNT_NO", "00000000-01")
    TARGET_SYMBOL = "069500"          # KODEX 200
    TOTAL_BUDGET = 10_000_000         # 1천만 원 운용 예시

    bot = Kospi200AsymmetricBot(
        account_no=ACCOUNT_NUMBER,
        symbol=TARGET_SYMBOL,
        total_capital=TOTAL_BUDGET
    )
    bot.run()
