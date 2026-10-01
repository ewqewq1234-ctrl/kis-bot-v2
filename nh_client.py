# nh_client.py
import os
import json
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("NHClient")
KST = timezone(timedelta(hours=9))
TOKEN_FILE = "tokens.json"

class NHClient:
    def __init__(self):
        self.app_key = os.getenv("NHPLUG_APP_KEY")
        self.app_secret = os.getenv("NHPLUG_APP_SECRET")
        self.account_no = os.getenv("NHPLUG_ACCOUNT_NO")
        self.base_url = "https://openapi.nhqv.com"  # 실서버 도메인
        self.access_token = self._get_valid_token()

    def _get_valid_token(self) -> str:
        """tokens.json 당일 캐싱 확인 및 OAuth2 토큰 발급"""
        today_str = datetime.now(KST).strftime("%Y-%m-%d")
        
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                    if tdata.get("issued_date") == today_str and "access_token" in tdata:
                        return tdata["access_token"]
            except Exception:
                pass

        # 토큰 신규 발급 (/oauth2/token) - application/x-www-form-urlencoded
        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret
        }
        res = requests.post(url, headers=headers, data=data, timeout=10)
        res_json = res.json()
        token = res_json.get("access_token")
        
        if token:
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump({"issued_date": today_str, "access_token": token}, f, indent=2)
            return token
        else:
            raise ValueError(f"토큰 발급 실패: {res_json}")

    def _headers(self, tr_id: str) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "tr_id": tr_id
        }

    def get_current_price(self, symbol: str) -> int:
        """시세 조회 (/krstock/quote/v1/currentExecution)"""
        url = f"{self.base_url}/krstock/quote/v1/currentExecution"
        params = {"view_main_yn": "N", "iem_cd": symbol}
        res = requests.get(url, headers=self._headers("CTPF1002R"), params=params, timeout=5)
        data = res.json()
        output1 = data.get("Output_1", {})
        price = output1.get("stck_prpr") or output1.get("prpr")
        return int(price) if price else 0

    def get_asset_status(self) -> dict:
        """자산/잔고 현황 조회 (/krstock/inquiry/v1/assetStatus)"""
        url = f"{self.base_url}/krstock/inquiry/v1/assetStatus"
        params = {"acno": self.account_no}
        res = requests.get(url, headers=self._headers("CTPF1001R"), params=params, timeout=5)
        data = res.json()
        
        output0 = data.get("Output_0", {})
        total_asset = float(output0.get("nas_amt", 0) or output0.get("dca", 0) or 0)
        
        holdings = {}
        for item in data.get("Output_1", []):
            code = item.get("iem_cd")
            qty = int(item.get("itg_bnc_qty", 0))
            avg_p = float(item.get("phs_pr", 0))
            if code:
                holdings[code] = {"qty": qty, "avg_price": avg_p}
                
        return {"total_asset": total_asset, "holdings": holdings}

    def order_cash_buy(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매수 주문"""
        url = f"{self.base_url}/krstock/order/v1/cashBuy"
        body = {
            "acno": self.account_no,
            "iem_cd": symbol,
            "ord_qty": str(qty),
            "nmn_pr_tp_cd": "05",  # 05: 시장가
            "sor_mkt_sli_yn": "N"
        }
        res = requests.post(url, headers=self._headers("TTTC0802U"), json=body, timeout=5)
        data = res.json()
        return data.get("rsp_cd") in ["00048", "00047", "00000"]

    def order_cash_sell(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매도 주문"""
        url = f"{self.base_url}/krstock/order/v1/cashSell"
        body = {
            "acno": self.account_no,
            "iem_cd": symbol,
            "ord_qty": str(qty),
            "nmn_pr_tp_cd": "05",  # 05: 시장가
            "sor_mkt_sli_yn": "N"
        }
        res = requests.post(url, headers=self._headers("TTTC0801U"), json=body, timeout=5)
        data = res.json()
        return data.get("rsp_cd") in ["00048", "00047", "00000"]
