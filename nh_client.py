import os
import json
import logging
import time
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger("NHClient")
TOKEN_FILE = "tokens.json"


class NHClient:
    def __init__(self):
        self.app_key = os.getenv("NHPLUG_APP_KEY")
        self.app_secret = os.getenv("NHPLUG_APP_SECRET")
        self.account_no = os.getenv("NHPLUG_ACCOUNT_NO")
        
        # 공식 운영 환경 기본 도메인
        self.base_url = os.getenv("NHPLUG_BASE_URL", "https://api.nhplug.com:8443")
        self.access_token = self._get_valid_token()

    def _get_valid_token(self) -> str:
        """24시간 유효 토큰 캐싱 및 재사용 (운영 도메인 전용 발급)"""
        now = time.time()
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                    tdata = json.load(f)
                    if tdata.get("expires_at", 0) > now + 600:
                        return tdata["access_token"]
            except Exception:
                pass

        # 토큰 발급은 운영(api.nhplug.com:8443) 전용
        url = "https://api.nhplug.com:8443/oauth2/token"
        params = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "scope": "oob"
        }
        res = requests.post(url, params=params, timeout=10)
        res.raise_for_status()
        res_json = res.json()
        
        token = res_json.get("access_token")
        expires_in = int(res_json.get("expires_in", 86400))
        
        if token:
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump({"access_token": token, "expires_at": now + expires_in}, f, indent=2)
            return token
        else:
            raise ValueError(f"토큰 발급 실패: {res_json}")

    def _headers(self) -> dict:
        """나무증권 REST 공식 규약: Bearer 토큰 단일 인증"""
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}"
        }

    def _post(self, path: str, payload: dict) -> dict:
        """나무증권 REST 공통 POST + Input_0 봉투 래핑"""
        url = f"{self.base_url}{path}"
        body = {"Input_0": payload}
        
        res = requests.post(url, headers=self._headers(), json=body, timeout=10)
        if res.status_code == 401:  # 토큰 무효 시 1회 재발급
            self.access_token = self._get_valid_token()
            res = requests.post(url, headers=self._headers(), json=body, timeout=10)
            
        res.raise_for_status()
        time.sleep(0.2)  # 초당 5회 유량 제한 준수
        return res.json()

    def get_current_price(self, symbol: str) -> int:
        """시세 조회 (/krstock/quote/v1/currentPrice)"""
        payload = {
            "market_cd": "KRX",
            "iem_cd": symbol
        }
        data = self._post("/krstock/quote/v1/currentPrice", payload)
        out0 = data.get("Output_0") or {}
        price = out0.get("stck_prpr")
        return int(price) if price else 0

    def get_asset_status(self) -> dict:
        """자산/잔고 현황 조회 (/krstock/inquiry/v1/assetStatus)"""
        payload = {
            "act_no": self.account_no,
            "eal_aly_cd": "2",  # 2: 시가평가
            "aet_bse": "1",      # 1: 순자산
            "qut_dit_cd": "KRX", # KRX 시세
            "aly_qut_cd": "1"   # 1: 정규장 (필수 파라미터)
        }
        data = self._post("/krstock/inquiry/v1/assetStatus", payload)
        out0 = data.get("Output_0") or {}
        out1 = data.get("Output_1") or []
        
        total_asset = float(out0.get("nas_amt", 0) or 0)
        holdings = {}
        for item in out1:
            code = item.get("iem_cd")
            if code:
                holdings[code] = {
                    "name": item.get("iem_nm"),
                    "qty": int(item.get("itg_bnc_qty", 0)),
                    "avg_price": float(item.get("phs_pr", 0)),
                    "now_price": float(item.get("now_pr", 0))
                }
                
        return {"total_asset": total_asset, "holdings": holdings}

    def order_cash_buy(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매수 주문 (/krstock/order/v1/cashBuy)"""
        if qty <= 0:
            return False
            
        payload = {
            "act_no": self.account_no,
            "iem_cd": symbol,
            "orr_qty": str(qty),
            "nmn_pr_tp_cd": "05",       # 05: 시장가 (orr_pr 생략 필수)
            "orr_cnd_dit_cd": "00",     # 00: 조건없음
            "ssl_nmn_pr_dit_cd": "00", # 00: 정상 (기본값)
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N"
        }
        data = self._post("/krstock/order/v1/cashBuy", payload)
        out0 = data.get("Output_0") or {}
        order_no = out0.get("mkt_orr_no")
        if not order_no:
            logger.error(f"[매수 실패] {data.get('rsp_msg')}")
            return False
        return True

    def order_cash_sell(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매도 주문 (/krstock/order/v1/cashSell)"""
        if qty <= 0:
            return False
            
        payload = {
            "act_no": self.account_no,
            "iem_cd": symbol,
            "orr_qty": str(qty),
            "nmn_pr_tp_cd": "05",       # 05: 시장가 (orr_pr 생략 필수)
            "orr_cnd_dit_cd": "00",     # 00: 조건없음
            "ssl_nmn_pr_dit_cd": "00", # 00: 정상 (기본값)
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N"
        }
        data = self._post("/krstock/order/v1/cashSell", payload)
        out0 = data.get("Output_0") or {}
        order_no = out0.get("mkt_orr_no")
        if not order_no:
            logger.error(f"[매도 실패] {data.get('rsp_msg')}")
            return False
        return True
