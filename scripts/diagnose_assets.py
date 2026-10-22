"""Read-only asset diagnostics. Never import the strategy or write trading state."""
import json
import logging
import math
import os
import re
from importlib.metadata import version

from nhplug import call


def valid_number(value, positive=False):
    try:
        number = float(str(value).replace(",", "").strip())
        return math.isfinite(number) and (number > 0 if positive else number >= 0)
    except (ValueError, TypeError):
        return False


def summarize(data):
    """Whitelist output; never copy arbitrary response fields or values to logs."""
    if not isinstance(data, dict):
        return {"response_valid": False}
    rows = data.get("Output_1")
    result = {"summary_present": isinstance(data.get("Output_0"), dict),
              "rows_present": isinstance(rows, list), "positions": []}
    if isinstance(rows, list):
        for item in rows:
            if not isinstance(item, dict):
                result["positions"].append({"row_valid": False})
                continue
            symbol = str(item.get("iem_cd") or "").removeprefix("A")
            raw_qty = item.get("itg_bnc_qty")
            qty = float(str(raw_qty).replace(",", "")) if valid_number(raw_qty) else None
            result["positions"].append({
                "symbol": symbol if re.fullmatch(r"[A-Z0-9]{6,12}", symbol) else "unrecognized_symbol",
                "asset_class": str(item.get("iem_mlf_nm") or "")[:50],
                "qty": qty, "phs_pr_present": "phs_pr" in item,
                "phs_pr_valid": valid_number(item.get("phs_pr"), positive=True),
                "phs_pr_zero": valid_number(item.get("phs_pr")) and float(str(item["phs_pr"]).replace(",", "")) == 0,
            })
    return result


def main():
    logging.disable(logging.CRITICAL)
    for source, target in (("NH_APP_KEY", "NHPLUG_APP_KEY"), ("NH_APP_SECRET", "NHPLUG_APP_SECRET")):
        os.environ[target] = os.environ[source]
    account = os.environ["NH_ACCOUNT_NO"].replace("-", "").strip()
    common = {"act_no": account, "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1"}
    print("nhplug_version=" + version("nhplug"))
    # Both endpoints are documented read-only inquiries. No order/cancel/strategy code.
    inquiries = (
        ("assetStatus", {**common, "eal_aly_cd": "2"}),
        ("balance", {**common, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9"}),
    )
    failed = False
    for name, payload in inquiries:
        try:
            data = call("/krstock/inquiry/v1/" + name, payload, timeout=15)
            print(name + "=" + json.dumps(summarize(data), ensure_ascii=True, allow_nan=False))
        except Exception:
            # SDK exceptions may contain the full response or credentials; never print them.
            print(name + "=query_failed_details_redacted")
            failed = True
    # Exercise the production parser without the strategy, state files, quotes, or orders.
    # Restrict both SDK entrypoints to the two documented read-only inquiries.
    import nh_client
    original_call, original_paginate = nh_client.call, nh_client.paginate
    allowed = {"/krstock/inquiry/v1/assetStatus", "/krstock/inquiry/v1/balance"}

    def read_call(path, *args, **kwargs):
        if path not in allowed:
            raise RuntimeError("non-inquiry path forbidden")
        return original_call(path, *args, **kwargs)

    def read_pages(path, *args, **kwargs):
        if path not in allowed:
            raise RuntimeError("non-inquiry path forbidden")
        return original_paginate(path, *args, **kwargs)

    def no_order(*args, **kwargs):
        raise RuntimeError("orders forbidden in diagnostics")

    nh_client.call, nh_client.paginate = read_call, read_pages
    nh_client.NHClient.submit_order = no_order
    try:
        parsed = nh_client.NHClient().get_asset_status()
        holdings = parsed["holdings"]
        print("production_parser=" + json.dumps({
            "success": True, "stock_positions": [
                {"symbol": symbol if re.fullmatch(r"[A-Z0-9]{6,12}", symbol) else "unrecognized_symbol",
                 "qty": item["qty"],
                 "avg_valid": valid_number(item["avg_price"], positive=True) if item["qty"] else True}
                for symbol, item in holdings.items()]}, allow_nan=False))
    except Exception:
        print("production_parser=failed_details_redacted")
        failed = True
    return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("diagnostic_failed_details_redacted")
        raise SystemExit(1)
