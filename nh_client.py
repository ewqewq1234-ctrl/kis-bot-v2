import os
import json
import requests
from datetime import datetime, timezone, timedelta

class NHInvestClient:
    BASE_URL = "https://api.nhplug.com:8443"

    def __init__(self, app_key=None, app_secret=None):
        self.app_key = app_key or os.getenv("NH_APP_KEY")
        self.app_secret = app_secret or os.getenv("NH_APP_SECRET")
        self.token_file = "tokens.json"
        self.access_token = None
        self._load_or_refresh_token()

    def _load_or_refresh_token(self):
        """1일 1회 발급 원칙: 당일 발급 내역이 유효하면 재발급 생략"""
        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime("%Y-%m-%d")

        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    issued_date = data.get("issued_date", "")
                    expires_at_str = data.get("expires_at", "2000-01-01T00:00:00")
                    expires_at = datetime.fromisoformat(expires_at_str)

                    # 당일 발급되었고 만료 30분 전까지는 저장된 토큰 재사용
                    if issued_date == today_str and datetime.now() < expires_at - timedelta(minutes=30):
                        self.access_token = data.get("access_token")
                        return
            except Exception:
                pass
        self._issue_token()

    def _issue_token(self):
        """나무증권 OAuth2 접근 토큰 신규 발급"""
        url = f"{self.BASE_URL}/oauth2/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob"
        }

        try:
            res = requests.post(url, headers=headers, data=data, timeout=10)
            if res.status_code != 200:
                print(f"[토큰 발급 실패] HTTP {res.status_code} | 본문: {res.text}")
                res.raise_for_status()

            token_info = res.json()
            self.access_token = token_info.get("access_token")
            expires_in = int(token_info.get("expires_in", 86400))
            expires_at = datetime.now() + timedelta(seconds=expires_in)

            kst = timezone(timedelta(hours=9))
            today_str = datetime.now(kst).strftime("%Y-%m-%d")

            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump({
                    "access_token": self.access_token,
                    "expires_at": expires_at.isoformat(),
                    "issued_date": today_str
                }, f, indent=2)
            print(f"[토큰 1일 1회 발급 완료] 기준일자: {today_str}")
        except Exception as e:
            print(f"[토큰 발급 중 치명적 오류]: {e}")
            raise

    def get_current_price(self, ticker="069500"):
        """나무증권 현재가 체결추이 조회 (/krstock/quote/v1/currentExecution)"""
        url = f"{self.BASE_URL}/krstock/quote/v1/currentExecution"

        for attempt in range(2):
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.access_token}",
                "appkey": self.app_key,
                "appsecretkey": self.app_secret
            }
            payload = {
                "Input_0": {
                    "market_cd": "KRX",
                    "iem_cd": str(ticker),
                    "view_main_yn": "N"
                }
            }
            res = requests.post(url, headers=headers, json=payload, timeout=5)

            if "IGW40043" in res.text or res.status_code == 401:
                print("[토큰 만료 감지] 토큰 강제 재발급 진행...")
                self._issue_token()
                continue

            if res.status_code != 200:
                print(f"[시세 조회 실패] HTTP {res.status_code}: {res.text}")
                res.raise_for_status()

            data = res.json()
            price = None
            if "Output_1" in data and isinstance(data["Output_1"], dict):
                price = data["Output_1"].get("stck_prpr")
            elif "Output_0" in data and isinstance(data["Output_0"], list) and len(data["Output_0"]) > 0:
                price = data["Output_0"][0].get("stck_prpr")

            if price is None:
                raise KeyError(f"현재가(stck_prpr) 파싱 불가: {res.text}")

            return float(str(price).replace(",", "").strip())

        raise RuntimeError("토큰 재발급 후에도 시세 조회 실패")

    def get_account_assets(self, account_no, ticker="069500"):
        """실시간 예수금 및 대상 종목 보유 잔고/평단가 조회 (나무증권 공식 엔드포인트)"""
        url = f"{self.BASE_URL}/krstock/inquiry/v1/balance"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "cts": "",
            "cts_flag": ""
        }
        payload = {
            "Input_0": {
                "act_no": str(account_no).replace("-", "").strip(),
                "bnc_bse_cd": "1",      # 체결기준 잔고
                "ltg_aot_dit_cd": "1",  # 상장종목
                "aet_bse": "1",         # 순자산 기준
                "qut_dit_cd": "UNT"     # 통합시세
            }
        }
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code != 200:
                print(f"[계좌 자산 조회 실패] HTTP {res.status_code} | 본문: {res.text}")
                return None, 0, 0.0

            data = res.json()
            
            # 예수금 / 총평가금액 추출 (Output_0 요약)
            summary = data.get("Output_0") or {}
            if isinstance(summary, list) and len(summary) > 0:
                summary = summary[0]

            cash = float(
                summary.get("ord_able_cash") or 
                summary.get("dnca_tot_amt") or 
                summary.get("eal_amt_sum") or 
                0.0
            )

            # 보유 종목 추출 (Output_1 목록)
            shares = 0
            avg_price = 0.0
            stocks = data.get("Output_1") or []
            for item in stocks:
                iem_cd = str(item.get("iem_cd") or item.get("pdno") or "")
                if str(ticker) in iem_cd:
                    shares = int(item.get("hld_qty") or item.get("cblc_qty") or 0)
                    avg_price = float(item.get("pchs_avg_prc") or item.get("pchs_amt") or 0.0)
                    break

            return cash, shares, avg_price
        except Exception as e:
            print(f"[계좌 자산 조회 오류]: {e}")
            return None, 0, 0.0

    def send_order(self, account_no, ticker="069500", qty=1, price=0, order_type="BUY"):
        """나무증권 공식 매수/매도 주문 전송 (KRX 시장가 '05') 및 검증"""
        endpoint = "/krstock/order/v1/cashBuy" if order_type == "BUY" else "/krstock/order/v1/cashSell"
        url = f"{self.BASE_URL}{endpoint}"

        for attempt in range(2):
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.access_token}",
                "appkey": self.app_key,
                "appsecretkey": self.app_secret,
                "cts": "",
                "cts_flag": "",
                "mac_address": ""
            }

            is_market = (price == 0)
            nmn_pr_tp_cd = "05" if is_market else "01"
            order_price = 0 if is_market else int(price)

            payload = {
                "Input_0": {
                    "act_no": str(account_no).replace("-", "").strip(),
                    "iem_cd": str(ticker),
                    "orr_qty": int(qty),
                    "orr_pr": order_price,
                    "nmn_pr_tp_cd": nmn_pr_tp_cd,
                    "orr_cnd_dit_cd": "00",
                    "ssl_nmn_pr_dit_cd": "00",
                    "rmt_mkt_cd": "KRX",
                    "sor_mkt_sli_yn": "N"
                }
            }

            res = requests.post(url, headers=headers, json=payload, timeout=10)

            if "IGW40043" in res.text or res.status_code == 401:
                print("[주문 중 토큰 만료 감지] 토큰 재발급 후 재시도...")
                self._issue_token()
                continue

            result = res.json()
            rsp_cd = result.get("rsp_cd", "")
            rsp_msg = result.get("rsp_msg", "")

            # 정상 접수 코드: 00048(매수), 00047(매도), 00000(성공)
            if rsp_cd not in ["00048", "00047", "00000"]:
                raise RuntimeError(f"증권사 주문 거절 [{rsp_cd}]: {rsp_msg}")

            print(f"[주문 접수 완료] {order_type} {qty}주 | 응답: {rsp_msg}")
            return result

        raise RuntimeError("주문 처리 실패")
