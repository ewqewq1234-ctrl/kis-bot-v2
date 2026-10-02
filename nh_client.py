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
        # 환경변수 우선순위 체크 (NH_ 및 NHPLUG_ 모두 지원)
        self.app_key = os.getenv("NH_APP_KEY") or os.getenv("NHPLUG_APP_KEY")
        self.app_secret = os.getenv("NH_APP_SECRET") or os.getenv("NHPLUG_APP_SECRET")
        self.act_no = os.getenv("NH_ACCOUNT_NO") or os.getenv("NHPLUG_ACCOUNT_NO")  # 11자리 계좌번호

        if not self.app_key or not self.app_secret:
            raise ValueError(
                "[인증 오류] APP_KEY 또는 APP_SECRET 환경변수가 비어있습니다. "
                "GitHub Secrets 설정(NH_APP_KEY, NH_APP_SECRET)을 확인하세요."
            )

        self.base_url = os.getenv("NHPLUG_BASE_URL", "https://api.nhplug.com:8443")
        self.access_token = self._get_valid_token()

    def _get_valid_token(self) -> str:
        """24시간 유효 토큰 파일 캐싱 및 재사용 (운영 도메인 전용 발급)"""
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
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}"
        }

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        body = {"Input_0": payload}

        res = requests.post(url, headers=self._headers(), json=body, timeout=10)
        if res.status_code == 401:
            self.access_token = self._get_valid_token()
            res = requests.post(url, headers=self._headers(), json=body, timeout=10)

        res.raise_for_status()
        time.sleep(0.2)  # 초당 5회 제한 보호
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
        """자산 현황 조회 (/krstock/inquiry/v1/assetStatus) - 거래시간 연장 규약 준수"""
        payload = {
            "act_no": self.act_no,
            "eal_aly_cd": "2",  # 시가평가
            "aet_bse": "1",      # 순자산
            "qut_dit_cd": "KRX", # KRX 시세
            "aly_qut_cd": "1"   # 정규장 필수
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
        """시장가 현금 매수 주문 (/krstock/order/v1/cashBuy) - orr_pr 생략 규약"""
        if qty <= 0:
            return False

        payload = {
            "act_no": self.act_no,
            "iem_cd": symbol,
            "orr_qty": str(qty),
            "nmn_pr_tp_cd": "05",       # 시장가
            "orr_cnd_dit_cd": "00",     # 조건없음
            "ssl_nmn_pr_dit_cd": "00", # 정상
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N"
        }
        data = self._post("/krstock/order/v1/cashBuy", payload)
        out0 = data.get("Output_0") or {}
        return bool(out0.get("mkt_orr_no"))

    def order_cash_sell(self, symbol: str, qty: int) -> bool:
        """시장가 현금 매도 주문 (/krstock/order/v1/cashSell) - orr_pr 생략 규약"""
        if qty <= 0:
            return False

        payload = {
            "act_no": self.act_no,
            "iem_cd": symbol,
            "orr_qty": str(qty),
            "nmn_pr_tp_cd": "05",       # 시장가
            "orr_cnd_dit_cd": "00",     # 조건없음
            "ssl_nmn_pr_dit_cd": "00", # 정상
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N"
        }
        data = self._post("/krstock/order/v1/cashSell", payload)
        out0 = data.get("Output_0") or {}
        return bool(out0.get("mkt_orr_no"))
