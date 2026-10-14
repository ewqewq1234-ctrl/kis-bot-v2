"""전량 청산 / 미체결 / 매수 후 당일 청산 회귀 테스트 (네트워크 주문 없음)."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime as RealDatetime
from unittest.mock import patch

# 임시 스텁으로 외부 API 연결을 원천 차단한다.
if "nhplug" not in sys.modules:
    stub = types.ModuleType("nhplug")
    class FakeNhplugError(Exception):
        pass
    stub.NhplugError = FakeNhplugError
    stub.call = lambda *a, **kw: None
    stub.paginate = lambda *a, **kw: iter(())
    stub.status_of = lambda data: (data.get("rsp_cd"), data.get("rsp_msg"))
    sys.modules["nhplug"] = stub

import run_cloud
import nh_client


class FixedClock(RealDatetime):
    instant = (2026, 10, 14, 11, 0)

    @classmethod
    def now(cls, tz=None):
        return RealDatetime(*cls.instant, tzinfo=tz)


class Broker:
    def __init__(self, qty=32, avg=105756.09, price=105850, bid=105820,
                 today_orders=None, past_orders=None):
        self.qty, self.avg, self.price, self.bid = qty, avg, price, bid
        self.today_orders = today_orders if today_orders is not None else []
        self.past_orders = past_orders if past_orders is not None else []
        self.submissions = []
        self.queried_dates = []

    def get_asset_status(self):
        holdings = {"069500": {"qty": self.qty, "avg_price": self.avg}} if self.qty else {}
        return {"total_asset": 4000000, "available_cash": 4000000, "holdings": holdings}

    def get_current_price(self, code):
        return self.price

    def get_best_bid(self, code):
        return self.bid

    def get_daily_orders(self, code, day):
        self.queried_dates.append(day)
        return self.today_orders if day == "20261014" else self.past_orders

    def submit_order(self, code, qty, side, price=0):
        self.submissions.append((code, qty, side, price))
        return {"order_no": "1234567", "message": "주문 접수"}


def buy_record(qty=5):
    return {"sby_dit_cd_nm": "현금매수", "iem_cd": "069500", "orr_qty": qty,
            "tot_cns_qty": qty, "ny_cns_qty": 0, "can_qty": 0}


def sell_record(qty=32, executed=32, remaining=0, cancelled=0):
    return {"sby_dit_cd_nm": "현금매도", "iem_cd": "069500", "orr_qty": qty,
            "tot_cns_qty": executed, "ny_cns_qty": remaining, "can_qty": cancelled,
            "itg_orr_no": 999888777}


class ExitLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.mocks = [patch.object(run_cloud, "STATE_FILE", self.path),
                      patch.object(run_cloud, "datetime", FixedClock),
                      patch.object(run_cloud, "send_telegram"),
                      patch.dict(os.environ, {"ENABLE_LIVE_ORDERS": "YES"})]
        for item in self.mocks:
            item.start()
            self.addCleanup(item.stop)
        FixedClock.instant = (2026, 10, 14, 11, 0)

    def stage4(self, **updates):
        state = run_cloud.default_state()
        state.update({"shares": 32, "current_step": 4, "current_stage": 4,
                      "avg_price": 105756.09, "entry_prices": [111467, 111540, 105650],
                      "pending_order": None})
        state.update(updates)
        with open(self.path, "w", encoding="utf8") as f:
            json.dump(state, f)
        return state

    def run_once(self, broker):
        with patch.object(run_cloud, "NHClient", return_value=broker):
            run_cloud.run_strategy()
        with open(self.path, encoding="utf8") as f:
            return json.load(f)

    def test_stage4_sells_after_same_day_buy_at_verified_bid_limit(self):
        self.stage4()
        broker = Broker(today_orders=[buy_record()])
        state = self.run_once(broker)
        self.assertEqual(broker.submissions, [("069500", 32, "sell", 105820)])
        self.assertEqual(state["current_step"], 4)  # 접수만 됐으므로 0단계 전환 금지
        self.assertEqual(state["pending_order"]["status"], "accepted")
        self.assertEqual(state["pending_order"]["limit_price"], 105820)

    def test_stage4_does_not_sell_if_best_bid_below_exit_floor(self):
        self.stage4()
        broker = Broker(price=105850, bid=105800)
        self.run_once(broker)
        self.assertEqual(broker.submissions, [])

    def test_stage4_blocks_second_sell_same_day(self):
        self.stage4()
        broker = Broker(today_orders=[sell_record()])
        with self.assertRaises(run_cloud.SafetyHalt):
            self.run_once(broker)
        self.assertEqual(broker.submissions, [])

    def test_unknown_order_side_fails_closed(self):
        self.stage4()
        broker = Broker(today_orders=[{"sby_dit_cd_nm": "기타", "ny_cns_qty": 0}])
        with self.assertRaises(run_cloud.SafetyHalt):
            self.run_once(broker)
        self.assertEqual(broker.submissions, [])

    def test_stage1_trailing_can_sell_after_buy(self):
        state = run_cloud.default_state()
        state.update({"shares": 5, "current_step": 1, "current_stage": 1,
                      "avg_price": 100000, "entry_prices": [100000],
                      "trailing_mode": True, "highest_price": 110000})
        with open(self.path, "w", encoding="utf8") as f:
            json.dump(state, f)
        broker = Broker(qty=5, avg=100000, price=108800,
                        today_orders=[buy_record()])
        saved = self.run_once(broker)
        self.assertEqual(broker.submissions, [("069500", 5, "sell", 0)])
        self.assertEqual(saved["current_step"], 1)

    def test_sell_full_fill_confirmed_and_cooldown(self):
        pending = {"side": "sell", "qty": 32, "target_step": 0,
                   "before_qty": 32, "before_avg": 105756.09,
                   "created_at": "2026-10-14T10:00:00+09:00",
                   "status": "accepted", "order_no": "1234567"}
        self.stage4(pending_order=pending)
        broker = Broker(qty=0, avg=0, today_orders=[sell_record()])
        saved = self.run_once(broker)
        self.assertEqual(saved["current_step"], 0)
        self.assertIsNone(saved["pending_order"])
        self.assertEqual(saved["last_exit_date"], "2026-10-14")
        self.assertEqual(broker.submissions, [])
        self.assertTrue(run_cloud.entry_cooldown_active(saved, RealDatetime(2026, 10, 15, 11)))
        self.assertFalse(run_cloud.entry_cooldown_active(saved, RealDatetime(2026, 10, 16, 11)))

    def test_sell_confirmation_previous_day_fetches_original_date(self):
        pending = {"side": "sell", "qty": 32, "target_step": 0,
                   "before_qty": 32, "before_avg": 105756.09,
                   "created_at": "2026-10-13T14:00:00+09:00",
                   "status": "accepted", "order_no": "1234567"}
        self.stage4(pending_order=pending)
        broker = Broker(qty=0, avg=0, past_orders=[sell_record()])
        saved = self.run_once(broker)
        self.assertIn("20261013", broker.queried_dates)
        self.assertEqual(saved["current_step"], 0)
        self.assertEqual(broker.submissions, [])  # 완료한 직후 신규매수 금지

    def test_partial_sell_cannot_clear_pending(self):
        pending = {"side": "sell", "qty": 32, "target_step": 0,
                   "before_qty": 32, "before_avg": 105756.09,
                   "created_at": "2026-10-14T10:00:00+09:00"}
        state = self.stage4(pending_order=pending)
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.reconcile_pending(state, 12, 105756.09, [sell_record(executed=20, remaining=12)])
        self.assertIsNotNone(state["pending_order"])

    def test_sell_qty_zero_without_execution_evidence_never_resets(self):
        pending = {"side": "sell", "qty": 32, "target_step": 0,
                   "before_qty": 32, "before_avg": 105756.09,
                   "created_at": "2026-10-14T10:00:00+09:00"}
        state = self.stage4(pending_order=pending)
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.reconcile_pending(state, 0, 0, [])
        self.assertEqual(state["current_step"], 4)

    def test_cancelled_sell_cannot_confirm_full_fill(self):
        pending = {"qty": 32}
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.verify_sell_filled(pending, [sell_record(executed=30, cancelled=2)])

    def test_multiple_sell_records_are_ambiguous(self):
        pending = {"qty": 32}
        with self.assertRaises(run_cloud.SafetyHalt):
            run_cloud.verify_sell_filled(pending, [sell_record(), sell_record()])

    def test_missing_best_bid_blocks_exit(self):
        client = object.__new__(nh_client.NHClient)
        with patch.object(client, "_call", return_value={"Output_1": {"stck_prpr": 105850}}):
            with self.assertRaises(nh_client.NHClientError):
                client.get_best_bid("069500")

    def test_selling_bid_with_valid_api_field(self):
        client = object.__new__(nh_client.NHClient)
        with patch.object(client, "_call", return_value={"Output_1": {"bidp": 105820}}):
            self.assertEqual(client.get_best_bid("069500"), 105820)

    def test_after_friday_exit_next_monday_is_blocked(self):
        state = {"last_exit_date": "2026-10-16"}  # Friday
        self.assertTrue(run_cloud.entry_cooldown_active(state, RealDatetime(2026, 10, 19, 11)))
        self.assertFalse(run_cloud.entry_cooldown_active(state, RealDatetime(2026, 10, 20, 11)))


if __name__ == "__main__":
    unittest.main()
