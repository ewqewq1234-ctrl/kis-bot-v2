import os
import json
import time
import requests
from typing import Dict, Any, Optional

BASE_URL = "https://api.nhplug.com:8443"  # 운영 환경 기준 (모의투자는 https://moapi.nhplug.com:8443)
TOKEN_CACHE_FILE = "token_cache.json"

class NHClient:
    def __init__(self):
        self.app_key = os.getenv("NH_APP_KEY")
        self.app_secret = os.getenv("NH_APP_SECRET")
        self.act_no = os.getenv("NH_ACCOUNT_NO")  # 11자리 계좌번호
        self.access_token = self._get_valid_token()

    def _get_valid_token(self) -> str:
        """24시간 유효 토큰 로컬 파일 캐싱 및 재사용 (나무증권 가이드 준수)"""
        now = time.time()
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                    if cache.get("expires_at", 0) > now + 600:  # 만료 10분 전까지 재사용
                        return cache["access_token"]
            except Exception:
                pass

        # 토큰 신규 발급 (application/x-www-form-urlencoded, Params 전달)
        url = f"{BASE_URL}/oauth2/token"
        params = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "scope": "oob"
        }
        res = requests.post(url, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
        
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        
        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expires_at": now + expires_in}, f)
            
        return token

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "Authorization": f"Bearer {self.access_token}"
        }

    def get_current_price(self, symbol: str) -> int:
        """주식현재가 시세 조회 (/krstock/quote/v1/currentPrice)"""
        url = f"{BASE_URL}/krstock/quote/v1/currentPrice"
        payload = {
            "Input_0": {
                "market_cd": "KRX",
                "iem_cd": symbol
            }
        }
        res = requests.post(url, json=payload, headers=self._headers(), timeout=5)
        if res.status_code == 200:
            data = res.json()
            out = data.get("Output_0", {})
            return int(out.get("stck_prpr", 0))
        return 0

    def get_asset_status(self) -> Dict[str, Any]:
        """계좌 자산 현황 조회 (/krstock/inquiry/v1/assetStatus)"""
        url = f"{BASE_URL}/krstock/inquiry/v1/assetStatus"
        payload = {
            "Input_0": {
                "act_no": self.act_no,
                "eal_aly_cd": "2",      # 2: 시가평가
                "aet_bse": "1",         # 1: 순자산
                "qut_dit_cd": "KRX",
                "aly_qut_cd": "1"       # 1: 정규장
            }
        }
        res = requests.post(url, json=payload, headers=self._headers(), timeout=5)
        result = {"total_asset": 0, "holdings": {}}
        if res.status_code == 200:
            data = res.json()
            out0 = data.get("Output_0", {})
            out1 = data.get("Output_1", [])
            result["total_asset"] = float(out0.get("nas_amt", 0))  # 순자산금액
            
            for item in out1:
                code = item.get("iem_cd")
                result["holdings"][code] = {
                    "qty": int(item.get("itg_bnc_qty", 0)),
                    "avg_price": float(item.get("phs_pr", 0))
                }
        return result

    def order_cash_buy(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매수 주문 (/krstock/order/v1/cashBuy)"""
        url = f"{BASE_URL}/krstock/order/v1/cashBuy"
        payload = {
            "Input_0": {
                "act_no": self.act_no,
                "iem_cd": symbol,
                "orr_qty": str(qty),
                "orr_pr": "0",
                "nmn_pr_tp_cd": "05",          # 05: 시장가
                "orr_cnd_dit_cd": "00",        # 00: 조건없음
                "ssl_nmn_pr_dit_cd": "00",    # 00: 정상
                "rmt_mkt_cd": "KRX",           # KRX 요청
                "sor_mkt_sli_yn": "N"          # SOR 미적용 시 N 필수
            }
        }
        res = requests.post(url, json=payload, headers=self._headers(), timeout=5)
        if res.status_code == 200:
            out = res.json().get("Output_0", {})
            return bool(out.get("mkt_orr_no"))
        return False

    def order_cash_sell(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매도 주문 (/krstock/order/v1/cashSell)"""
        url = f"{BASE_URL}/krstock/order/v1/cashSell"
        payload = {
            "Input_0": {
                "act_no": self.act_no,
                "iem_cd": symbol,
                "orr_qty": str(qty),
                "orr_pr": "0",
                "nmn_pr_tp_cd": "05",          # 05: 시장가
                "orr_cnd_dit_cd": "00",        # 00: 없음
                "ssl_nmn_pr_dit_cd": "00",    # 00: 정상
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N"
            }
        }
        res = requests.post(url, json=payload, headers=self._headers(), timeout=5)
        if res.status_code == 200:
            out = res.json().get("Output_0", {})
            return bool(out.get("mkt_orr_no"))
        return False
