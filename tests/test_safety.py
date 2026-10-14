"""실주문 없이 안전 규칙을 검증하는 테스트."""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# 테스트 중 외부 nhplug 패키지, 계좌 네트워크에 연결하지 않는다.
fake_sdk = types.ModuleType("nhplug")
class FakeNhplugError(Exception):
    pass
fake_sdk.NhplugError = FakeNhplugError
fake_sdk.call = lambda *a, **k: None
fake_sdk.paginate = lambda *a, **k: iter(())
fake_sdk.status_of = lambda d: (d.get("rsp_cd"), d.get("rsp_msg"))
sys.modules["nhplug"] = fake_sdk

import nh_client
import run_cloud


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.folder.name, "state.json")
        self.state_patcher = patch.object(run_cloud, "STATE_FILE", self.state_path)
        self.telegram_patcher = patch.object(run_cloud, "send_telegram")
        self.state_patcher.start()
        self.telegram_patcher.start()
        self.addCleanup(self.state_patcher.stop)
        self.addCleanup(self.telegram_patcher.stop)
        self.addCleanup(self.folder.cleanup)

    def test_32_shares_recover_stage4_without_extra_order(self):
        state = run_cloud.default_state()
        state.update({"current_step": 3, "current_stage": 3, "shares": 17,
                      "avg_price": 109308.53, "entry_prices": [111467, 111540, 105650]})
        run_cloud.reconcile_existing(state, 32, 105756)
        self.assertEqual(state["current_step"], 4)
        self.assertEqual(state["shares"], 32)
        self.assertEqual(state["avg_price"], 105756)
        with open(self.state_path, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["current_step"], 4)

    def test_unexplained_intermediate_quantity_halts(self):
        state = run_cloud.default_state()
        state.update({"current_step": 3, "shares": 17, "entry_prices": [1, 2, 3]})
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.reconcile_existing(state, 22, 107000)

    def test_pending_buy_partial_fill_halts(self):
        state = run_cloud.default_state()
        state.update({"current_step": 3, "shares": 17, "entry_prices": [111467, 111540, 105650],
                      "pending_order": {"before_qty": 17, "before_avg": 109308.53,
                                        "qty": 15, "side": "buy", "target_step": 4}})
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.reconcile_pending(state, 20, 108000)
        self.assertIsNotNone(state["pending_order"])

    def test_pending_full_buy_confirms_stage_only_then(self):
        state = run_cloud.default_state()
        state.update({"current_step": 3, "shares": 17, "entry_prices": [111467, 111540, 105650],
                      "pending_order": {"before_qty": 17, "before_avg": 109308.53,
                                        "qty": 15, "side": "buy", "target_step": 4}})
        run_cloud.reconcile_pending(state, 32, 105756)
        self.assertEqual(state["current_step"], 4)
        self.assertEqual(len(state["entry_prices"]), 4)
        self.assertIsNone(state["pending_order"])

    def test_order_receipt_is_not_called_fill(self):
        state = run_cloud.default_state()
        class Stub:
            def submit_order(self, *args, **kwargs):
                return {"order_no": "45678", "message": "접수"}
        with patch.dict(os.environ, {"ENABLE_LIVE_ORDERS": "YES"}):
            from datetime import datetime
            run_cloud.place_order(Stub(), state, "buy", 3, 100000, 1, 0, 0, datetime.now())
        self.assertEqual(state["current_step"], 0)
        self.assertEqual(state["pending_order"]["status"], "accepted")

    def test_order_exceeding_32_never_sent(self):
        state = run_cloud.default_state()
        class Stub:
            def submit_order(self, *args, **kwargs):
                raise AssertionError("should never send")
        with self.assertRaises(run_cloud.SafetyHalt):
            from datetime import datetime
            run_cloud.place_order(Stub(), state, "buy", 3, 100000, 4, 31, 100000, datetime.now())

    def test_asset_result_missing_holdings_is_error(self):
        client = object.__new__(nh_client.NHClient)
        client.account_no = "00000000000"
        with patch.object(client, "_call", return_value={"Output_0": {"tot_aet_amt": 3000000}}):
            with self.assertRaises(nh_client.NHClientError):
                client.get_asset_status()

    def test_receipt_number_is_required(self):
        client = object.__new__(nh_client.NHClient)
        client.account_no = "00000000000"
        with patch.object(client, "_call", return_value={"Output_0": {}, "rsp_msg": "정상"}):
            with self.assertRaises(nh_client.NHClientError):
                client.submit_order("069500", 1, "buy", price=95000)

    def test_tokenless_data_does_not_reset_state(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("broken JSON")
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.load_state()


if __name__ == "__main__":
    unittest.main()

class IntegrationTests(unittest.TestCase):
    def test_running_with_live_32_shares_never_buys_more(self):
        from datetime import datetime as RealDatetime
        class TradingHour(RealDatetime):
            @classmethod
            def now(cls, tz=None):
                return RealDatetime(2026, 10, 14, 11, 0, tzinfo=tz)
        class Broker:
            def get_asset_status(self):
                return {"total_asset": 3047700, "available_cash": 0,
                        "holdings": {"069500": {"qty": 32, "avg_price": 105756}}}
            def get_current_price(self, symbol):
                return 95250
            def get_daily_orders(self, symbol, day):
                return []
            def submit_order(self, *args, **kwargs):
                raise AssertionError("32주 보유일 때 추가 주문을 내면 안 됨")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state.json")
            old = run_cloud.default_state()
            old.update({"current_step": 3, "current_stage": 3, "shares": 17,
                        "entry_prices": [111467, 111540, 105650], "avg_price": 109308.53})
            with open(path, "w", encoding="utf-8") as file:
                json.dump(old, file)
            with patch.object(run_cloud, "STATE_FILE", path), \
                 patch.object(run_cloud, "NHClient", return_value=Broker()), \
                 patch.object(run_cloud, "datetime", TradingHour), \
                 patch.object(run_cloud, "send_telegram"):
                run_cloud.run_strategy()
                with open(path, encoding="utf-8") as file:
                    saved = json.load(file)
                self.assertEqual(saved["current_step"], 4)
                self.assertEqual(saved["shares"], 32)

    def test_daily_order_check_blocks_unknown_history(self):
        client = object.__new__(nh_client.NHClient)
        client.account_no = "00000000000"
        class Meta:
            has_next = False
        with patch.object(nh_client, "paginate", return_value=iter([({"rsp_cd": "?"}, Meta())])):
            with self.assertRaises(nh_client.NHClientError):
                client.get_daily_orders("069500", "20261014")
