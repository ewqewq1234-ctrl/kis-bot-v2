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
        """기존에 발급받아 저장된 토큰이 있는지 확인하고 24시간 만료 전 재사용"""
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
        """가이드 규격: /oauth2/token (client_credentials, scope=oob)"""
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

        # 토큰 중복 발급 방지를 위해 로컬 캐시 파일 저장
        with open(self.token_file, "w") as f:
            json.dump({
                "access_token": self.access_token,
                "expires_at": expires_at.isoformat()
            }, f)

    def _get_headers(self, tr_id=None):
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "tr_id": tr_id
        }

    def get_current_price(self, ticker="069500"):
        """KODEX 200 실시간 현재가 조회"""
        url = f"{self.BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._get_headers(tr_id="FHKST01010100")
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}
        res = requests.get(url, headers=headers, params=params, timeout=5)
        res.raise_for_status()
        output = res.json().get("output", {})
        return float(output.get("stck_prpr", 0))

    def send_order(self, account_no, ticker="069500", qty=1, price=0, order_type="BUY"):
        """실전 매수/매도 주문 (시장가 기본)"""
        url = f"{self.BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = "TTTC0802U" if order_type == "BUY" else "TTTC0801U"
        headers = self._get_headers(tr_id=tr_id)
        payload = {
            "CANO": account_no[:8],
            "ACNT_PRDT_CD": account_no[8:],
            "PDNO": ticker,
            "ORD_DVSN": "01" if price == 0 else "00",  # 01: 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price)
        }
        res = requests.post(url, headers=headers, json=payload, timeout=5)
        return res.json()
