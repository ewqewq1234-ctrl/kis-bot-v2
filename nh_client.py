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
        """24시간 유효 토큰 관리 및 재사용"""
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    data = json.load(f)
                    expires_at = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
                    if datetime.now() < expires_at - timedelta(hours=1):
                        self.access_token = data.get("access_token")
                        return
            except Exception:
                pass
        self._issue_token()

    def _issue_token(self):
        """나무증권 OAuth2 접근 토큰 발급"""
        url = f"{self.BASE_URL}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob"
        }
        res = requests.post(url, headers=headers, data=data, timeout=10)
        res.raise_for_status()
        token_info = res.json()

        self.access_token = token_info["access_token"]
        expires_in = token_info.get("expires_in", 86400)
        expires_at = datetime.now() + timedelta(seconds=expires_in)

        with open(self.token_file, "w") as f:
            json.dump({
                "access_token": self.access_token,
                "expires_at": expires_at.isoformat()
            }, f)

    def get_current_price(self, ticker="069500"):
        """
        나무증권 현재가 조회: /krstock/quote/v1/currentPrice (POST)
        """
        url = f"{self.BASE_URL}/krstock/quote/v1/currentPrice"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret
        }
        payload = {
            "Input_0": {
                "market_cd": "KRX",
                "iem_cd": ticker
            }
        }
        res = requests.post(url, headers=headers, json=payload, timeout=5)
        if res.status_code != 200:
            print(f"[시세 조회 실패] HTTP {res.status_code}: {res.text}")
            res.raise_for_status()

        data = res.json()
        output = data.get("Output_0") or data.get("output") or data.get("output1") or data
        if isinstance(output, list) and len(output) > 0:
            output = output[0]

        price = (
            output.get("cur_prc") or 
            output.get("prc") or 
            output.get("now_prc") or 
            output.get("stck_prpr") or 
            output.get("clpr")
        )

        if price is None:
            print(f"[응답 확인용] 전체 데이터: {data}")
            raise KeyError("현재가 필드를 찾을 수 없습니다.")

        return float(str(price).replace(",", "").strip())

    def send_order(self, account_no, ticker="069500", qty=1, price=0, order_type="BUY"):
        """
        나무증권 공식 명세:
        - 매수: /krstock/order/v1/cashBuy
        - 매도: /krstock/order/v1/cashSell
        """
        endpoint = "/krstock/order/v1/cashBuy" if order_type == "BUY" else "/krstock/order/v1/cashSell"
        url = f"{self.BASE_URL}{endpoint}"

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "cts": "",
            "cts_flag": "",
            "mac_address": ""
        }

        # 시장가 주문: nmn_pr_tp_cd = "03", 지정가 = "01"
        is_market = (price == 0)
        nmn_pr_tp_cd = "03" if is_market else "01"
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
                "rmt_mkt_cd": "SOR",
                "sor_mkt_sli_yn": "N"
            }
        }

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            print(f"[주문 API 결과] {order_type} {qty}주 | HTTP {res.status_code} | 본문: {res.text}")
            return res.json()
        except Exception as e:
            print(f"[주문 전송 오류]: {e}")
            return None
