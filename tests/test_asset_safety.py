"""Account parsing and real entry-point regressions; all network calls are forbidden."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime as RealDatetime
from unittest.mock import Mock, patch

if "nhplug" not in sys.modules:
    sdk = types.ModuleType("nhplug")
    sdk.NhplugError = type("NhplugError", (Exception,), {})
    sdk.call = lambda *a, **k: None
    sdk.paginate = lambda *a, **k: iter(())
    sdk.status_of = lambda d: (d.get("rsp_cd"), d.get("rsp_msg"))
    sys.modules["nhplug"] = sdk

import nh_client
import run_cloud

ROOT = Path(__file__).resolve().parents[1]


def balance(rows, **summary):
    return {"Output_0": {"tot_byn_amt": 100000, "tot_eal_amt": 100000, **summary},
            "Output_1": rows}


def row(avg="105756", qty="32", symbol="069500"):
    return {"iem_cd": symbol, "itg_bnc_qty": qty, "phs_pr": avg}


class AssetParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(nh_client.NHClient)
        self.client.account_no = "00000000000"
        self.assets = {"Output_0": {"tot_aet_amt": 4000000, "stk_orr_pbl_amt": 100000},
                       "Output_1": [row(), row(0, "93977", "NHKRCMA030")]}
        self.call = patch.object(self.client, "_call", return_value=self.assets).start()
        self.addCleanup(patch.stopall)
        patch.object(nh_client, "call", side_effect=AssertionError("network forbidden")).start()

    def parse(self, rows, **summary):
        pages = [(balance(rows, **summary), types.SimpleNamespace(has_next=False))]
        with patch.object(nh_client, "paginate", return_value=iter(pages)) as query:
            result = self.client.get_asset_status()
        self.assertEqual(query.call_args.args[0], "/krstock/inquiry/v1/balance")
        self.assertEqual(query.call_args.args[1]["ltg_aot_dit_cd"], "9")
        return result

    def test_zero_quantity_missing_purchase_price(self):
        result = self.parse([{"iem_cd": "069500", "itg_bnc_qty": 0}])
        self.assertEqual(result["holdings"]["069500"], {"qty": 0, "avg_price": 0})

    def test_positive_quantity_valid_numeric_price(self):
        self.assertEqual(self.parse([row(105756.09)])["holdings"]["069500"]["avg_price"], 105756.09)

    def test_positive_quantity_valid_string_price(self):
        self.assertEqual(self.parse([row("105,756.09")])["holdings"]["069500"]["avg_price"], 105756.09)

    def test_positive_quantity_missing_price(self):
        with self.assertRaisesRegex(nh_client.NHClientError, "종목: 069500"):
            self.parse([{"iem_cd": "069500", "itg_bnc_qty": 32}])

    def test_zero_price_blocks(self):
        with self.assertRaises(nh_client.NHClientError):
            self.parse([row(0)])

    def test_negative_price_blocks(self):
        with self.assertRaises(nh_client.NHClientError):
            self.parse([row(-1)])

    def test_invalid_string_blocks_without_echoing_raw_value(self):
        with self.assertRaises(nh_client.NHClientError) as raised:
            self.parse([row("sensitive-invalid-value")])
        self.assertNotIn("sensitive-invalid-value", str(raised.exception))
        self.assertIn("신규 주문 및 매도 주문 보류", str(raised.exception))

    def test_nan_blocks(self):
        for value in (float("nan"), "NaN"):
            with self.subTest(value=value), self.assertRaises(nh_client.NHClientError):
                self.parse([row(value)])

    def test_infinity_blocks(self):
        for value in (float("inf"), "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(nh_client.NHClientError):
                self.parse([row(value)])

    def test_null_blank_and_boolean_block(self):
        for value in (None, "null", "", " ", True, False):
            with self.subTest(value=value), self.assertRaises(nh_client.NHClientError):
                self.parse([row(value)])

    def test_current_price_and_other_numbers_never_substitute(self):
        invalid = {**row(0), "now_pr": 110000, "eal_amt": 3500000,
                   "avg_phs_uit_pr": 105756, "byn_amt": 3384192}
        with self.assertRaises(nh_client.NHClientError):
            self.parse([invalid])

    def test_actual_response_shape_cma_zero_price_not_stock(self):
        # Observed sanitized shape: ETF + 발행어음 in assetStatus; ETF only in balance.
        result = self.parse([row()])
        self.assertEqual(set(result["holdings"]), {"069500"})
        self.assertEqual(result["holdings"]["069500"]["qty"], 32)
        self.assertEqual(result["total_asset"], 4000000)

    def test_official_stock_balance_price_used_when_asset_price_zero(self):
        self.assets["Output_1"][0]["phs_pr"] = 0
        self.assertEqual(self.parse([row()])["holdings"]["069500"]["avg_price"], 105756)

    def test_other_stock_invalid_price_also_blocks(self):
        with self.assertRaises(nh_client.NHClientError):
            self.parse([row(), row(0, 1, "122630")])

    def test_missing_stock_rows_with_nonzero_totals_blocks(self):
        with self.assertRaises(nh_client.NHClientError):
            self.parse(None)

    def test_empty_stock_rows_with_nonzero_totals_blocks(self):
        with self.assertRaises(nh_client.NHClientError):
            self.parse([])

    def test_zero_stock_account_requires_zero_summary_totals(self):
        self.assertEqual(self.parse(None, tot_byn_amt=0, tot_eal_amt=0)["holdings"], {})

    def test_no_pages_never_means_empty_account(self):
        with patch.object(nh_client, "paginate", return_value=iter(())):
            with self.assertRaises(nh_client.NHClientError):
                self.client.get_asset_status()

    def test_pagination_reads_all_pages_before_return(self):
        pages = [({"Output_1": [row()]}, types.SimpleNamespace(has_next=True)),
                 (balance([row(20000, 1, "122630")]), types.SimpleNamespace(has_next=False))]
        with patch.object(nh_client, "paginate", return_value=iter(pages)):
            self.assertEqual(len(self.client.get_asset_status()["holdings"]), 2)

    def test_pagination_limit_blocks(self):
        pages = [(balance([row()]), types.SimpleNamespace(has_next=True))] * 6
        with patch.object(nh_client, "paginate", return_value=iter(pages)):
            with self.assertRaises(nh_client.NHClientError):
                self.client.get_asset_status()

    def test_premature_pagination_end_blocks(self):
        pages = [(balance([row()]), types.SimpleNamespace(has_next=True))]
        with patch.object(nh_client, "paginate", return_value=iter(pages)):
            with self.assertRaisesRegex(nh_client.NHClientError, "연속조회 미완료"):
                self.client.get_asset_status()

    def test_missing_final_summary_blocks(self):
        pages = [({"Output_1": [row()]}, types.SimpleNamespace(has_next=False))]
        with patch.object(nh_client, "paginate", return_value=iter(pages)):
            with self.assertRaises(nh_client.NHClientError):
                self.client.get_asset_status()

    def test_duplicate_and_invalid_quantities_block(self):
        for rows in ([row(), row()], [row(qty=-1)], [row(qty="NaN")], [row(qty=1.5)]):
            with self.subTest(rows=rows), self.assertRaises(nh_client.NHClientError):
                self.parse(rows)


class ExecutionGateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.json"
        patch.object(run_cloud, "STATE_FILE", str(self.path)).start()
        patch.object(run_cloud, "send_telegram", return_value=True).start()
        self.broker = patch.object(run_cloud, "NHClient").start()
        self.addCleanup(patch.stopall)
        self.state = run_cloud.default_state()
        self.state.update(shares=32, avg_price=105756.09, current_step=4, current_stage=4,
                          entry_prices=[111467, 111540, 105650, 105756],
                          pending_order={"side": "sell", "qty": 32, "before_qty": 32,
                                         "status": "submitting", "order_no": None,
                                         "created_at": "2026-10-22T15:00:00+09:00"})
        run_cloud.save_state(self.state)

    def clock(self, hour, minute=0):
        class Clock(RealDatetime):
            @classmethod
            def now(cls, tz=None):
                return RealDatetime(2026, 10, 22, hour, minute, tzinfo=tz)
        return patch.object(run_cloud, "datetime", Clock)

    def test_after_hours_no_broker_queries_or_orders_and_pending_preserved(self):
        for hour, minute in ((21, 20), (15, 35), (9, 0), (8, 52)):
            with self.subTest(hour=hour, minute=minute), self.clock(hour, minute):
                run_cloud._run_strategy_core()
                saved = json.loads(self.path.read_text())
                expected = copy.deepcopy(self.state)
                expected["last_run"] = saved["last_run"]
                self.assertEqual(saved, expected)
        self.broker.assert_not_called()

    def test_after_hours_report_is_explicitly_unverified_and_no_inquiries(self):
        with self.clock(15, 35), patch.dict(os.environ, {"RUN_MIDDAY_REPORT": "Y", "ENABLE_LIVE_ORDERS": "YES"}):
            run_cloud.run_strategy()
        self.broker.assert_not_called()
        text = run_cloud.send_telegram.call_args.args[0]
        self.assertIn("실계좌·시세 미조회", text)
        self.assertIn("체결 확인이 아닙니다", text)
        saved = run_cloud.load_state()
        self.assertEqual(saved["pending_order"], self.state["pending_order"])
        self.assertEqual(saved["shares"], 32)

    def test_market_hours_account_error_blocks_all_orders_and_preserves_state(self):
        before = self.path.read_bytes()
        self.broker.return_value.get_asset_status.side_effect = nh_client.NHClientError("평균매입가 불명확")
        with self.clock(11), self.assertRaises(nh_client.NHClientError):
            run_cloud.run_strategy()
        self.broker.return_value.get_current_price.assert_not_called()
        self.broker.return_value.get_daily_orders.assert_not_called()
        self.broker.return_value.submit_order.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_original(self):
        before = self.path.read_bytes()
        with patch.object(run_cloud.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                run_cloud.save_state({"shares": 0})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".state-*.tmp")), [])

    def test_corrupt_state_is_not_initialized_after_hours(self):
        self.path.write_text("not json")
        with self.clock(21), self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.run_strategy()
        self.assertEqual(self.path.read_text(), "not json")
        self.broker.assert_not_called()

    def test_production_workflow_schedule_and_safety_settings(self):
        text = (ROOT / ".github/workflows/trade.yml").read_text()
        for expected in ("'17 9,11,13 * * 1-5'", "'7 15 * * 1-5'", "'35 15 * * 1-5'",
                         "timezone: 'Asia/Seoul'", "github.event.schedule == '35 15 * * 1-5'",
                         "cancel-in-progress: false",
                         "python-version: '3.11'", "if: always()", "git add state.json",
                         "vars.ENABLE_LIVE_ORDERS || 'NO'", "contents: write"):
            self.assertIn(expected, text)


class EntrypointTests(unittest.TestCase):
    def launch(self, hour, minute, report="N"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            state = run_cloud.default_state()
            state.update(shares=32, avg_price=105756, current_step=4,
                         pending_order={"side": "sell", "status": "submitting"})
            (path / "state.json").write_text(json.dumps(state))
            # Freeze time and forbid SDK/network in a fresh Python process, as Actions invokes it.
            (path / "sitecustomize.py").write_text('''
import datetime,sys,types
RealDatetime=datetime.datetime
class Clock(RealDatetime):
    @classmethod
    def now(cls,tz=None):
        return RealDatetime(2026,10,22,HOUR,MINUTE,tzinfo=tz)
datetime.datetime=Clock
sdk=types.ModuleType('nhplug')
sdk.NhplugError=type('NhplugError',(Exception,),{})
def forbidden(*a,**k):
    raise sdk.NhplugError('offline inquiry failure')
sdk.call=forbidden
sdk.paginate=forbidden
sdk.status_of=lambda d:(None,None)
sys.modules['nhplug']=sdk
import requests
requests.post=forbidden
'''.replace("HOUR", str(hour)).replace("MINUTE", str(minute)))
            env = {**os.environ, "PYTHONPATH": str(path) + os.pathsep + str(ROOT),
                   "NH_APP_KEY": "offline", "NH_APP_SECRET": "offline", "NH_ACCOUNT_NO": "00000000000",
                   "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
                   "RUN_MIDDAY_REPORT": report, "ENABLE_LIVE_ORDERS": "YES"}
            result = subprocess.run([sys.executable, str(ROOT / "run_cloud.py")], cwd=path,
                                    env=env, capture_output=True, text=True, timeout=10)
            saved = json.loads((path / "state.json").read_text())
            self.assertEqual(saved["shares"], 32)
            self.assertEqual(saved["avg_price"], 105756)
            self.assertEqual(saved["pending_order"], state["pending_order"])
            return result, saved

    def test_actions_night_entrypoint_succeeds_without_api(self):
        result, saved = self.launch(21, 20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(saved["last_run"])

    def test_actions_1535_entrypoint_report_uses_saved_state_only(self):
        result, _ = self.launch(15, 35, "Y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("실계좌·시세 미조회", result.stderr)

    def test_actions_daytime_inquiry_error_fails_instead_of_success(self):
        result, saved = self.launch(11, 0)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(saved["last_run"], "")


if __name__ == "__main__":
    unittest.main()
