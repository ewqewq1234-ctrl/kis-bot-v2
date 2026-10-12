import os
import json
import logging
from datetime import datetime, timezone, timedelta
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from nhplug import call, NhplugError, status_of

logger = logging.getLogger("NHClient")

class NHClient:
    BASE_URL = "https://api.nhplug.com:8443"

    def __init__(self, app_key=None, app_secret=None, account_no=None):
        self.app_key = app_key or os.getenv("NH_APP_KEY")
        self.app_secret = app_secret or os.getenv("NH_APP_SECRET")
        self.account_no = account_no or os.getenv("NH_ACCOUNT_NO")
        self.token_file = "tokens.json"
        self.access_token = None

        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._load_or_refresh_token()

    def _get_auth_headers(self):
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "cts": "",
            "cts_flag": ""
        }

    def _load_or_refresh_token(self):
        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime("%Y-%m-%d")

        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("issued_date") == today_str:
                        self.access_token = data.get("access_token")
                        return
            except Exception:
                pass
        self._issue_token()

    def _issue_token(self):
        url = f"{self.BASE_URL}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "appkey": self.app_key,
            "appsecretkey": self.app_secret,
            "grant_type": "client_credentials",
            "scope": "oob"
        }
        res = self.session.post(url, headers=headers, data=data, timeout=20)
        res.raise_for_status()
        token_info = res.json()
        self.access_token = token_info.get("access_token")

        kst = timezone(timedelta(hours=9))
        today_str = datetime.now(kst).strftime("%Y-%m-%d")
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump({"access_token": self.access_token, "issued_date": today_str}, f)

    def _post(self, path, payload):
        url = f"{self.BASE_URL}{path}"
        headers = self._get_auth_headers()
        res = self.session.post(url, headers=headers, json=payload, timeout=(10, 20))
        if res.status_code != 200:
            logger.error(f"[API 호출 오류] {path} | HTTP {res.status_code} | 본문: {res.text}")
            res.raise_for_status()
        return res.json()

    def get_current_price(self, ticker="069500"):
        payload = {
            "Input_0": {
                "market_cd": "KRX",
                "iem_cd": str(ticker).replace("A", "").strip(),
                "view_main_yn": "Y"
            }
        }
        try:
            data = self._post("/krstock/quote/v1/currentExecution", payload)
            out_1 = data.get("Output_1") or {}
            price = out_1.get("stck_prpr")
            return float(str(price).replace(",", "").strip()) if price else 0.0
        except Exception as e:
            logger.error(f"시세 조회 예외: {e}")
            return 0.0

    def get_asset_status(self):
        act_no = str(self.account_no).replace("-", "").strip()
        payload = {
            "Input_0": {
                "act_no": act_no,
                "eal_aly_cd": "2",
                "aet_bse": "1",
                "qut_dit_cd": "UNT",
                "aly_qut_cd": "2"
            }
        }
        try:
            data = self._post("/krstock/inquiry/v1/assetStatus", payload)
            out_0 = data.get("Output_0") or {}
            total_asset = float(out_0.get("nas_amt") or out_0.get("tot_aet_amt") or 0.0)

            holdings = {}
            for item in (data.get("Output_1") or []):
                sym = str(item.get("iem_cd") or "").replace("A", "").strip()
                holdings[sym] = {
                    "qty": int(float(item.get("itg_bnc_qty") or 0)),
                    "avg_price": float(item.get("phs_pr") or 0.0)
                }

            return {"total_asset": total_asset, "holdings": holdings}
        except Exception as e:
            logger.error(f"자산 조회 예외: {e}")
            return {"total_asset": 0.0, "holdings": {}}

    def order_cash_buy_with_reason(self, ticker, qty, price=0):
        """현금 지정가/시장가 매수 (증거금 할증 회피 지정가 전환 반영)"""
        act_no = str(self.account_no).replace("-", "").strip()
        
        # price가 0이면 현재가 조회 후 지정가(01) 가격으로 활용하여 증거금 초과 할증 회피
        if price == 0:
            current_price = self.get_current_price(ticker)
            order_price = int(current_price) if current_price > 0 else 0
        else:
            order_price = int(price)

        payload = {
            "Input_0": {
                "act_no": act_no,
                "iem_cd": str(ticker).replace("A", "").strip(),
                "orr_qty": int(qty),
                "orr_pr": order_price,
                "nmn_pr_tp_cd": "01",  # 지정가 01
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N"
            }
        }
        try:
            data = call("/krstock/order/v1/cashBuy", payload)
            rsp_cd, rsp_msg = status_of(data)
            if any(k in rsp_msg for k in ["종료", "불가", "오류", "거절", "한도초과", "잔고부족", "증거금부족"]):
                logger.warning(f"[주문 거절] {ticker} {qty}주 | 사유: {rsp_msg}")
                return False, rsp_msg
            logger.info(f"[주문 접수 완료] {ticker} {qty}주 | {rsp_msg}")
            return True, rsp_msg
        except Exception as e:
            err_str = str(e)
            logger.error(f"[주문 전송 예외]: {err_str}")
            return False, err_str

    def order_cash_sell_with_reason(self, ticker, qty, price=0):
        act_no = str(self.account_no).replace("-", "").strip()
        is_market = (price == 0)
        payload = {
            "Input_0": {
                "act_no": act_no,
                "iem_cd": str(ticker).replace("A", "").strip(),
                "orr_qty": int(qty),
                "orr_pr": 0 if is_market else int(price),
                "nmn_pr_tp_cd": "05" if is_market else "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N"
            }
        }
        try:
            data = self._post("/krstock/order/v1/cashSell", payload)
            rsp_msg = data.get("rsp_msg", "")
            if any(k in rsp_msg for k in ["종료", "불가", "오류", "거절", "한도초과", "잔고부족", "증거금부족"]):
                logger.warning(f"[매도 거절] {ticker} {qty}주 | 사유: {rsp_msg}")
                return False, rsp_msg
            logger.info(f"[매도 접수 완료] {ticker} {qty}주 | {rsp_msg}")
            return True, rsp_msg
        except Exception as e:
            err_str = str(e)
            logger.error(f"[매도 전송 예외]: {err_str}")
            return False, err_str
