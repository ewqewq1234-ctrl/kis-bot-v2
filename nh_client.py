"""NH투자증권(나무) API 어댑터 — 공식 nhplug SDK, 업무응답 검증.

참조: PLUG-OpenAPI/nhplug-mcp + nhplug-sdk/AGENTS.md, 2026-09 명세.
HTTP 200은 업무 성공을 보장하지 않으므로, 필수 출력 검증과 오류 문구 점검을 별도 수행한다.
"""
import logging
import math
import os
from datetime import datetime, timedelta, timezone

from nhplug import NhplugError, call, paginate, status_of

logger = logging.getLogger("NHClient")
KST = timezone(timedelta(hours=9))


class NHClientError(RuntimeError):
    """결과 불확실 포함: 이 예외가 발생하면 새로운 주문을 보내지 않는다."""


def _number(value, field):
    try:
        parsed = float(str(value).replace(",", "").strip())
        if not math.isfinite(parsed):
            raise ValueError("finite number required")
        return parsed
    except (TypeError, ValueError) as exc:
        raise NHClientError(f"{field} 숫자 파싱 실패: {value!r}") from exc


def _symbol(value):
    value = str(value or "").strip()
    return value[1:] if value.startswith("A") else value


def _message(data):
    _, msg = status_of(data)
    if msg:
        return str(msg)
    extra = data.get("message") if isinstance(data, dict) else None
    if isinstance(extra, dict):
        return str(extra.get("rsp_msg") or extra.get("message") or "")
    return str(extra or "")


def _check_business_result(data, label):
    if not isinstance(data, dict):
        raise NHClientError(f"{label}: 비정상 응답 형식")
    msg = _message(data)
    # 응답코드는 API마다 의미가 달라 성공코드를 하드코딩하지 않는다.
    errors = ("오류", "실패", "거절", "거부", "불가", "증거금부족", "잔고부족", "유효하지", "초과", "잘못된")
    if any(word in msg for word in errors):
        raise NHClientError(f"{label}: {msg}")
    return data


class NHClient:
    def __init__(self, app_key=None, app_secret=None, account_no=None):
        self.app_key = app_key or os.getenv("NH_APP_KEY")
        self.app_secret = app_secret or os.getenv("NH_APP_SECRET")
        self.account_no = str(account_no or os.getenv("NH_ACCOUNT_NO") or "").replace("-", "").strip()
        if not self.app_key or not self.app_secret or not self.account_no:
            raise NHClientError("NH_APP_KEY / NH_APP_SECRET / NH_ACCOUNT_NO 설정 누락")
        # SDK가 캐시·토큰 만료·429 스로틀을 직접 관리하도록 일원화한다.
        os.environ["NHPLUG_APP_KEY"] = self.app_key
        os.environ["NHPLUG_APP_SECRET"] = self.app_secret
        os.environ.setdefault("NHPLUG_BASE_URL", "https://api.nhplug.com:8443")

    def _call(self, path, payload, label):
        try:
            data = call(path, payload, timeout=15)
        except NhplugError as exc:
            raise NHClientError(f"{label}: NH API 통신 실패 (결과 불확실): {exc}") from exc
        return _check_business_result(data, label)

    def get_current_price(self, ticker="069500"):
        data = self._call("/krstock/quote/v1/currentExecution", {
            "market_cd": "KRX", "iem_cd": _symbol(ticker), "view_main_yn": "Y"
        }, "현재가")
        out = data.get("Output_1")
        if not isinstance(out, dict) or out.get("stck_prpr") is None:
            raise NHClientError("현재가: Output_1.stck_prpr 누락 (주문 중단)")
        price = _number(out["stck_prpr"], "현재가")
        if price <= 0:
            raise NHClientError("현재가가 0 이하임 (주문 중단)")
        return int(price)

    def get_asset_status(self):
        data = self._call("/krstock/inquiry/v1/assetStatus", {
            "act_no": self.account_no, "eal_aly_cd": "2", "aet_bse": "2",
            "qut_dit_cd": "UNT", "aly_qut_cd": "1"
        }, "자산조회")
        summary, rows = data.get("Output_0"), data.get("Output_1")
        if not isinstance(summary, dict):
            raise NHClientError("자산조회: Output_0 누락")
        if rows is None:
            # 공식 명세에서 데이터가 없는 출력블록은 생략될 수 있다.
            # '총매수/총평가금액 모두 0'으로 검증된 빈 계좌만 0주 허용한다.
            if (summary.get("tot_byn_amt") is not None
                    and summary.get("tot_eal_amt") is not None
                    and _number(summary["tot_byn_amt"], "총매수금액") == 0
                    and _number(summary["tot_eal_amt"], "총평가금액") == 0):
                rows = []
            else:
                raise NHClientError("자산조회: 잔고 출력 블록 누락 (0주로 추정 금지)")
        if not isinstance(rows, list):
            raise NHClientError("자산조회: Output_1 형식 오류")
        if summary.get("tot_aet_amt") is None:
            raise NHClientError("자산조회: tot_aet_amt 누락")
        total_asset = _number(summary["tot_aet_amt"], "총자산")
        if total_asset < 0:
            raise NHClientError("자산조회: 총자산 음수")
        available = summary.get("stk_orr_pbl_amt")
        available_cash = _number(available, "주문가능금액") if available is not None else None
        holdings = {}
        for item in rows:
            if not isinstance(item, dict) or "iem_cd" not in item or "itg_bnc_qty" not in item:
                raise NHClientError("자산조회: 보유종목 데이터 누락")
            sym = _symbol(item["iem_cd"])
            qty = _number(item["itg_bnc_qty"], "보유수량")
            if qty < 0 or not qty.is_integer():
                raise NHClientError("자산조회: 비정상 보유수량")
            avg = _number(item.get("phs_pr"), "평균매입가") if qty > 0 else 0.0
            if qty > 0 and avg <= 0:
                raise NHClientError("자산조회: 평균매입가 누락")
            if sym in holdings:
                raise NHClientError(f"자산조회: 종목 {sym} 다중잔고 확인 필요")
            holdings[sym] = {"qty": int(qty), "avg_price": avg}
        return {"total_asset": total_asset, "available_cash": available_cash, "holdings": holdings}

    def get_daily_orders(self, ticker, order_date=None):
        day = order_date or datetime.now(KST).strftime("%Y%m%d")
        payload = {"orr_dt": day, "act_no": self.account_no,
                   "orr_mkt_cd": "00", "ost_cns_dit": "0"}
        matching = []
        try:
            pages = paginate("/krstock/inquiry/v1/dailyOrderExecution", payload,
                             max_pages=6, want_meta=True, timeout=15)
            for index, (data, meta) in enumerate(pages):
                _check_business_result(data, "당일주문/체결조회")
                if "Output_0" not in data and not _message(data):
                    raise NHClientError("당일주문조회: 목록도 업무 메시지도 누락 — 결과 불확실")
                rows = data.get("Output_0", [])
                if rows is None:
                    rows = []
                if not isinstance(rows, list):
                    raise NHClientError("당일주문조회: Output_0 형식 오류")
                for row in rows:
                    if not isinstance(row, dict):
                        raise NHClientError("당일주문조회: 주문내역 형식 오류")
                    if _symbol(row.get("iem_cd")) == _symbol(ticker):
                        if "ny_cns_qty" not in row:
                            raise NHClientError("당일주문조회: 미체결수량 누락")
                        matching.append(row)
                if index == 5 and meta.has_next:
                    raise NHClientError("당일주문조회: 6페이지 초과, 전체 주문 확인 실패")
        except NhplugError as exc:
            raise NHClientError(f"당일주문조회 실패 — 신규 주문 중단: {exc}") from exc
        return matching

    def submit_order(self, ticker, qty, side, price=0):
        if side not in ("buy", "sell") or not isinstance(qty, int) or qty <= 0:
            raise NHClientError("주문 방향 또는 수량 오류")
        is_market = side == "sell" and price == 0
        if side == "buy" and price <= 0:
            raise NHClientError("매수 지정가가 0 이하임")
        payload = {
            "act_no": self.account_no, "iem_cd": _symbol(ticker), "orr_qty": qty,
            "nmn_pr_tp_cd": "05" if is_market else "01",
            "orr_cnd_dit_cd": "00", "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX", "sor_mkt_sli_yn": "N",
        }
        if not is_market:
            payload["orr_pr"] = int(price)
        endpoint = "/krstock/order/v1/cashBuy" if side == "buy" else "/krstock/order/v1/cashSell"
        result = self._call(endpoint, payload, f"{side} 주문")
        output = result.get("Output_0")
        order_no = output.get("mkt_orr_no") if isinstance(output, dict) else None
        if order_no is None or not str(order_no).strip().isdigit() or int(order_no) <= 0:
            raise NHClientError(f"{side} 주문: 주문 접수번호 누락. 체결 여부 불명확, 추가 주문 금지")
        logger.info("[%s 주문 접수] %s %d주 / 주문번호=%s (체결 확인 전)", side, ticker, qty, order_no)
        return {"order_no": str(order_no), "message": _message(result)}
