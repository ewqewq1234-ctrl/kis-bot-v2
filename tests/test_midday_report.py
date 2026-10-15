"""중간보고 스케줄과 상태 보존 테스트 (외부 네트워크 연결 없음)."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime as RealDatetime
from unittest.mock import patch

if "nhplug" not in sys.modules:
    stub = types.ModuleType("nhplug")

    class FakeNhplugError(Exception):
        pass

    stub.NhplugError = FakeNhplugError
    stub.call = lambda *args, **kwargs: None
    stub.paginate = lambda *args, **kwargs: iter(())
    stub.status_of = lambda data: (data.get("rsp_cd"), data.get("rsp_msg"))
    sys.modules["nhplug"] = stub

import run_cloud


class FixedClock(RealDatetime):
    @classmethod
    def now(cls, tz=None):
        return RealDatetime(2026, 10, 15, 14, 13, tzinfo=tz)


class Broker:
    def __init__(self):
        self.asset_queries = 0
        self.submissions = []

    def get_asset_status(self):
        self.asset_queries += 1
        return {
            "total_asset": 4_000_000,
            "available_cash": 0,
            "holdings": {"069500": {"qty": 32, "avg_price": 105756}},
        }

    def get_current_price(self, symbol):
        return 105000

    def get_daily_orders(self, symbol, day):
        return []

    def submit_order(self, *args, **kwargs):
        self.submissions.append((args, kwargs))
        raise AssertionError("midday reporting must not place orders")


class MiddayReportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state_path = os.path.join(self.folder.name, "state.json")
        self.state_patcher = patch.object(run_cloud, "STATE_FILE", self.state_path)
        self.clock_patcher = patch.object(run_cloud, "datetime", FixedClock)
        self.state_patcher.start()
        self.clock_patcher.start()
        self.addCleanup(self.state_patcher.stop)
        self.addCleanup(self.clock_patcher.stop)

    def write_position(self):
        state = run_cloud.default_state()
        state.update({
            "shares": 32,
            "current_step": 4,
            "current_stage": 4,
            "avg_price": 105756,
            "entry_prices": [111467, 111540, 105650, 105756],
        })
        with open(self.state_path, "w", encoding="utf-8") as file:
            json.dump(state, file)

    def read_state(self):
        with open(self.state_path, encoding="utf-8") as file:
            return json.load(file)

    def test_non_final_schedule_does_not_send_report(self):
        with patch.dict(os.environ, {"RUN_MIDDAY_REPORT": "N"}), \
                patch.object(run_cloud, "_run_strategy_core") as core, \
                patch.object(run_cloud, "send_midday_report") as report:
            run_cloud.run_strategy()
        core.assert_called_once_with()
        report.assert_not_called()

    def test_final_schedule_reports_once_and_preserves_position(self):
        self.write_position()
        broker = Broker()
        with patch.dict(os.environ, {"RUN_MIDDAY_REPORT": "Y"}), \
                patch.object(run_cloud, "NHClient", return_value=broker), \
                patch.object(run_cloud, "send_telegram", return_value=True) as telegram:
            run_cloud.run_strategy()
            run_cloud.run_strategy()

        telegram.assert_called_once()
        report = telegram.call_args.args[0]
        self.assertIn("장중 잠정 스냅샷", report)
        self.assertIn("보유 32주", report)
        self.assertIn("평단 105,756.0원", report)
        self.assertIn("현재가 105,000원", report)
        self.assertIn("총자산 4,000,000원", report)
        self.assertIn("미확인 주문: 없음", report)
        self.assertEqual(broker.submissions, [])

        state = self.read_state()
        self.assertEqual(state["last_midday_report_date"], "2026-10-15")
        self.assertEqual(state["shares"], 32)
        self.assertEqual(state["current_step"], 4)

    def test_failed_send_does_not_mark_report_date(self):
        self.write_position()
        broker = Broker()
        with patch.object(run_cloud, "NHClient", return_value=broker), \
                patch.object(run_cloud, "send_telegram", return_value=False) as telegram:
            self.assertFalse(run_cloud.send_midday_report())

        telegram.assert_called_once()
        state = self.read_state()
        self.assertEqual(state["last_midday_report_date"], "")
        self.assertEqual(state["shares"], 32)


if __name__ == "__main__":
    unittest.main()
