import os
import json
import requests
from datetime import datetime, timedelta

class NHInvestClient:
    BASE_URL = "https://api.nhplug.com:8443"

    def __init__(self, app_key=None, app_secret=None):
        self.app_key = app_key or os.getenv("NH_APP_KEY")
        self.app_secret = app_secret or os.getenv("NH_APP_SECRET")
        self.token_file = "tokens.json"
        self.access_token = None
        self._load_or_refresh_token()

    def _load_or_refresh_token(self):
        """저장된 토큰 불러오기 또는 신규 발급"""
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    expires_at = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
                    if datetime.now() < expires_at - timedelta(hours=1):
                        self.access_token = data.get("access_token")
                        return
            except Exception:
                pass
        self._issue_token()

    def _issue_token(self):
        """나무증권 OAuth2 접근 토큰 신규 발급 (Content-Type 엄격 준수)"""
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
            expires_in = token_info.get("expires_in", 86400)
            expires_at = datetime.now() + timedelta(seconds=expires_in)

            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump({
                    "access_token": self.access_token,
                    "expires_at": expires_at.isoformat()
                }, f, indent=2)
            print("[토큰 발급 완료] 새로운 접근 토큰이 저장되었습니다.")
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
                print("[토큰 만료 감지] 토큰을 즉시 재발급받고 재시도합니다...")
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
                raise KeyError(f"현재가(stck_prpr) 필드를 찾을 수 없습니다: {res.text}")

            return float(str(price).replace(",", "").strip())

        raise RuntimeError("토큰 재발급 후에도 시세 조회에 실패했습니다.")

    def get_stock_balance(self, account_no, ticker="069500"):
        """실제 계좌 보유 잔고 및 평균 매입단가 조회"""
        url = f"{self.BASE_URL}/krstock/account/v1/balance"
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
                "inqr_cnd_dit_cd": "01"
            }
        }
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code != 200:
                return None, None
            
            data = res.json()
            output_list = data.get("Output_1") or data.get("output1") or []
            for item in output_list:
                iem_cd = item.get("iem_cd") or item.get("pdno") or ""
                if str(ticker) in str(iem_cd):
                    qty = int(item.get("hld_qty") or item.get("cblc_qty") or 0)
                    avg_prc = float(item.get("pchs_avg_prc") or item.get("pchs_amt") or 0.0)
                    return qty, avg_prc
            return 0, 0.0
        except Exception as e:
            print(f"[잔고 동기화 조회 오류]: {e}")
            return None, None

    def send_order(self, account_no, ticker="069500", qty=1, price=0, order_type="BUY"):
        """나무증권 매수/매도 주문 전송 및 성공 응답 검증"""
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
                print("[주문 중 토큰 만료 감지] 토큰 재발급 후 재주문 시도...")
                self._issue_token()
                continue

            result = res.json()
            rsp_cd = result.get("rsp_cd", "")
            rsp_msg = result.get("rsp_msg", "")
            
            # 매수 접수(00048), 매도 접수(00047), 정상 접수(00000) 검증
            if rsp_cd not in ["00048", "00047", "00000"]:
                raise RuntimeError(f"증권사 주문 거절 [{rsp_cd}]: {rsp_msg}")

            print(f"[주문 접수 성공] {order_type} {qty}주 | 응답: {rsp_msg}")
            return result

        raise RuntimeError("주문 재시도 후에도 주문 처리에 실패했습니다.")
