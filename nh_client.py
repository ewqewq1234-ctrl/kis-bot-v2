"""NH투자증권(나무) API 어댑터 — 공식 nhplug SDK, 업무응답 검증.

참조: PLUG-OpenAPI/nhplug-mcp + nhplug-sdk/AGENTS.md, 2026-09 명세.
HTTP 200은 업무 성공을 보장하지 않으므로, 필수 출력 검증과 오류 문구 점검을 별도 수행한다.
"""
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

from nhplug import NhplugError, call, paginate, status_of

logger = logging.getLogger("NHClient")
KST = timezone(timedelta(hours=9))


class NHClientError(RuntimeError):
    """결과 불확실 포함: 이 예외가 발생하면 새로운 주문을 보내지 않는다."""


# SDK 오류에는 인증 URL(query string에 appkey/appsecretkey 포함)이 실릴 수 있다.
# 오류 전문/원인 예외를 로깅하지 않고 안전하게 유형만 노출한다.
_SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:appkey|appsecretkey|app_secret|app_key|access_token|refresh_token|token)=)[^\s&#]+")
_SENSITIVE_KV = re.compile(r"(?i)\b(appkey|appsecretkey|app_secret|app_key|access_token|refresh_token|authorization)\b\s*[:=]\s*[^\s,&}]+")
_SENSITIVE_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def redact_sensitive(value):
    """알림·진단 출력용. 토큰/URL 및 알려진 자격증명을 노출하지 않는다."""
    result = str(value)
    result = _SENSITIVE_QUERY.sub(r"\1[REDACTED]", result)
    result = _SENSITIVE_KV.sub(r"\1=[REDACTED]", result)
    # 예외의 URL에 비정형 인증 파라미터가 있을 가능성까지 차단한다.
    result = _SENSITIVE_URL.sub("[URL_REDACTED]", result)
    for env_name in ("NH_APP_KEY", "NH_APP_SECRET", "NHPLUG_APP_KEY", "NHPLUG_APP_SECRET",
                     "TELEGRAM_BOT_TOKEN", "NH_ACCOUNT_NO"):
        secret = os.getenv(env_name)
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


def classify_nh_failure(exc):
    """HTTP 응답과 연결 타임아웃을 구분. 원본 예외는 반환하지 않는다."""
    raw = str(exc).lower()
    if any(term in raw for term in ("connecttimeout", "timed out", "timeout", "connectionerror",
                                     "connection refused", "name resolution", "network error",
                                     "네트워크 오류", "max retries exceeded", "연결 실패")):
        return "network_timeout"
    if any(term in raw for term in ("401", "403", "invalid_client", "unauthorized",
                                     "forbidden", "invalid credential", "invalid_grant",
                                     "인증 거부", "인증 실패", "유효하지 않은")):
        return "auth_rejected"
    if any(term in raw for term in ("429", "rate limit", "too many requests", "호출 제한")):
        return "rate_limited"
    if any(term in raw for term in ("502", "503", "504", "bad gateway", "service unavailable")):
        return "temporary_server_error"
    return "unclassified"


def safe_nh_error(label, exc):
    kind = classify_nh_failure(exc)
    hints = {
        "network_timeout": "NH API 연결 시간초과/네트워크 오류",
        "auth_rejected": "인증 거부 (키/권한/상태 확인 필요)",
        "rate_limited": "API 요청 제한",
        "temporary_server_error": "NH API 서버 일시 오류",
        "unclassified": "API 통신 오류 (원인 미확정)",
    }
    return NHClientError(f"{label}: {hints[kind]} [{kind}] — 주문 보류")



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
        # NH SDK 자체의 토큰 재시도 뒤에도 네트워크 타임아웃이 나면
        # 주문이 아닌 조회에 한해 최대 1회만 추가 시도한다.
        readonly = path.startswith(("/krstock/inquiry/", "/krstock/quote/"))
        for attempt in range(2 if readonly else 1):
            try:
                data = call(path, payload, timeout=15)
                return _check_business_result(data, label)
            except NhplugError as exc:
                kind = classify_nh_failure(exc)
                if readonly and attempt == 0 and kind in ("network_timeout", "temporary_server_error"):
                    logger.warning("%s: 일시적 NH API 장애 [%s] — 조회만 1회 재시도", label, kind)
                    time.sleep(1)
                    continue
                # SDK 원본 예외에는 토큰 발급 GET URL과 키가 포함될 수 있다.
                raise safe_nh_error(label, exc) from None
        raise NHClientError(f"{label}: 조회 재시도 소진 — 주문 보류")

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

    def get_best_bid(self, ticker="069500"):
        """3~4단계 청산 전 최우선 매수호가를 조회한다. 불확실하면 매도 중단."""
        data = self._call("/krstock/quote/v1/currentExecution", {
            "market_cd": "KRX", "iem_cd": _symbol(ticker), "view_main_yn": "Y"
        }, "매수호가")
        output = data.get("Output_1")
        if not isinstance(output, dict) or output.get("bidp") is None:
            raise NHClientError("매수호가: Output_1.bidp 누락 — 본전 매도 중단")
        bid = _number(output["bidp"], "최우선 매수호가")
        if bid <= 0 or not bid.is_integer():
            raise NHClientError("매수호가가 유효한 원화 정수가 아님")
        return int(bid)

    def get_asset_status(self):
        data = self._call("/krstock/inquiry/v1/assetStatus", {
            "act_no": self.account_no, "eal_aly_cd": "2", "aet_bse": "2",
            "qut_dit_cd": "UNT", "aly_qut_cd": "1"
        }, "자산조회")
        summary = data.get("Output_0")
        if not isinstance(summary, dict):
            raise NHClientError("자산조회: Output_0 누락")
        if summary.get("tot_aet_amt") is None:
            raise NHClientError("자산조회: tot_aet_amt 누락")
        total_asset = _number(summary["tot_aet_amt"], "총자산")
        if total_asset < 0:
            raise NHClientError("자산조회: 총자산 음수")
        available = summary.get("stk_orr_pbl_amt")
        available_cash = _number(available, "주문가능금액") if available is not None else None
        # assetStatus also contains non-stock assets (e.g. 발행어음 with phs_pr=0).
        # Stock membership/quantity must come from the official stock balance view,
        # never from a guessed asset-class name or a hard-coded ticker exception.
        rows = self._get_stock_balance_rows()
        holdings = {}
        for item in rows:
            if not isinstance(item, dict) or "iem_cd" not in item or "itg_bnc_qty" not in item:
                raise NHClientError("자산조회: 보유종목 데이터 누락")
            sym = _symbol(item["iem_cd"])
            qty = _number(item["itg_bnc_qty"], "보유수량")
            if qty < 0 or not qty.is_integer():
                raise NHClientError("자산조회: 비정상 보유수량")
            avg = 0.0
            if qty > 0:
                # Official balance Output_1.phs_pr = 매입가격 (18).
                # No documented alternate price in this block: never use now_pr/eal_amt.
                try:
                    avg = float(str(item.get("phs_pr")).replace(",", "").strip())
                    valid = math.isfinite(avg) and avg > 0
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    symbol_label = sym if len(sym) == 6 and sym.isascii() and sym.isdigit() else "식별불가"
                    presence = "누락" if item.get("phs_pr") is None else "유효하지 않음"
                    raise NHClientError(
                        f"[NH 자산조회 오류]\n종목: {symbol_label}\n보유 수량: {int(qty)}주\n"
                        f"평균매입가 필드(phs_pr): {presence}\n조치: 신규 주문 및 매도 주문 보류"
                    ) from None
            if sym in holdings:
                raise NHClientError(f"자산조회: 종목 {sym} 다중잔고 확인 필요")
            holdings[sym] = {"qty": int(qty), "avg_price": avg}
        return {"total_asset": total_asset, "available_cash": available_cash, "holdings": holdings}

    def _get_stock_balance_rows(self):
        """Complete official stock-only balance; missing/partial responses never mean zero."""
        payload = {"act_no": self.account_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
                   "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1"}
        rows, final_summary = [], None
        page_seen = False
        try:
            pages = paginate("/krstock/inquiry/v1/balance", payload,
                             max_pages=6, want_meta=True, timeout=15)
            for index, (data, meta) in enumerate(pages):
                page_seen = True
                _check_business_result(data, "주식잔고조회")
                page_rows = data.get("Output_1")
                summary = data.get("Output_0")
                if page_rows is None:
                    if not isinstance(summary, dict):
                        raise NHClientError("주식잔고조회: 잔고·요약 출력 누락 (0주로 추정 금지)")
                    if (summary.get("tot_byn_amt") is None or summary.get("tot_eal_amt") is None
                            or _number(summary["tot_byn_amt"], "총매수금액") != 0
                            or _number(summary["tot_eal_amt"], "총평가금액") != 0):
                        raise NHClientError("주식잔고조회: 잔고 출력 누락 (0주로 추정 금지)")
                    page_rows = []
                if not isinstance(page_rows, list):
                    raise NHClientError("주식잔고조회: Output_1 형식 오류")
                rows.extend(page_rows)
                final_summary = summary
                if index == 5 and meta.has_next:
                    raise NHClientError("주식잔고조회: 6페이지 초과, 전체 잔고 확인 실패")
        except NhplugError as exc:
            raise safe_nh_error("주식잔고조회", exc) from None
        if not page_seen:
            raise NHClientError("주식잔고조회: 응답 없음 (0주로 추정 금지)")
        if meta.has_next:
            # SDK also stops on repeated continuation keys: this is not a complete snapshot.
            raise NHClientError("주식잔고조회: 연속조회 미완료 — 주문 보류")
        # The official API supplies aggregate totals only after all holdings are read.
        if not isinstance(final_summary, dict):
            raise NHClientError("주식잔고조회: 최종 요약 출력 누락 (0주로 추정 금지)")
        for field in ("tot_byn_amt", "tot_eal_amt"):
            if final_summary.get(field) is None or _number(final_summary[field], field) < 0:
                raise NHClientError("주식잔고조회: 최종 합계 검증 실패")
        if not rows:
            if (final_summary.get("tot_byn_amt") is None or final_summary.get("tot_eal_amt") is None
                    or _number(final_summary["tot_byn_amt"], "총매수금액") != 0
                    or _number(final_summary["tot_eal_amt"], "총평가금액") != 0):
                raise NHClientError("주식잔고조회: 빈 잔고 검증 실패 (0주로 추정 금지)")
        return rows

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
            raise safe_nh_error("당일주문조회", exc) from None
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
