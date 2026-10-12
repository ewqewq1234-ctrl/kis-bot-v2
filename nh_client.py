import os
import json
import logging
from datetime import datetime, timezone, timedelta
from nhplug import call, NhplugError, status_of

logger = logging.getLogger("NHClient")

class NHClient:
    def __init__(self, account_no=None):
        self.account_no = account_no or os.getenv("NH_ACCOUNT_NO")

    def _get_act_no(self):
        """계좌번호 정제 (하이픈 제거)"""
        return str(self.account_no).replace("-", "").strip()

    def get_current_price(self, ticker="069500"):
        """현재가 조회 (SDK 규격: view_main_yn='Y' 필수)"""
        try:
            data = call("/krstock/quote/v1/currentExecution", {
                "market_cd": "KRX",
                "iem_cd": str(ticker).replace("A", "").strip(),
                "view_main_yn": "Y"
            })
            out_1 = data.get("Output_1") or {}
            price = out_1.get("stck_prpr")
            return float(str(price).replace(",", "").strip()) if price else 0.0
        except Exception as e:
            logger.error(f"시세 조회 예외: {e}")
            return 0.0

    def get_asset_status(self):
        """자산 및 잔고 조회 (SDK 규격: aly_qut_cd='2' 필수)"""
        try:
            data = call("/krstock/inquiry/v1/assetStatus", {
                "act_no": self._get_act_no(),
                "eal_aly_cd": "2",
                "aet_bse": "1",
                "qut_dit_cd": "UNT",
                "aly_qut_cd": "2"
            })
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
        """현금 매수 (시장가 '05', orr_pr: 0, rsp_msg 기반 성공/실패 판정)"""
        is_market = (price == 0)
        payload = {
            "Input_0": {
                "act_no": self._get_act_no(),
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
            data = call("/krstock/order/v1/cashBuy", payload)
            rsp_cd, rsp_msg = status_of(data)
            
            # 증권사 거절 키워드 탐지
            if any(k in rsp_msg for k in ["종료", "불가", "오류", "거절", "한도초과", "잔고부족", "증거금부족"]):
                logger.warning(f"[주문 거절] {ticker} {qty}주 | 사유: {rsp_msg}")
                return False, rsp_msg

            logger.info(f"[주문 접수 완료] {ticker} {qty}주 | {rsp_msg}")
            return True, rsp_msg
        except NhplugError as ne:
            err_str = f"API 오류 (HTTP {ne.status_code}): {ne.raw}"
            logger.error(f"[주문 예외]: {err_str}")
            return False, err_str
        except Exception as e:
            err_str = str(e)
            logger.error(f"[주문 전송 예외]: {err_str}")
            return False, err_str

    def order_cash_sell_with_reason(self, ticker, qty, price=0):
        """현금 매도 (시장가 '05', orr_pr: 0)"""
        is_market = (price == 0)
        payload = {
            "Input_0": {
                "act_no": self._get_act_no(),
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
            data = call("/krstock/order/v1/cashSell", payload)
            rsp_cd, rsp_msg = status_of(data)
            
            if any(k in rsp_msg for k in ["종료", "불가", "오류", "거절", "한도초과", "잔고부족", "증거금부족"]):
                logger.warning(f"[매도 거절] {ticker} {qty}주 | 사유: {rsp_msg}")
                return False, rsp_msg

            logger.info(f"[매도 접수 완료] {ticker} {qty}주 | {rsp_msg}")
            return True, rsp_msg
        except Exception as e:
            err_str = str(e)
            logger.error(f"[매도 전송 예외]: {err_str}")
            return False, err_str
