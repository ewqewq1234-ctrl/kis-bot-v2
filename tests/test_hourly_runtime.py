import os
import unittest
from datetime import datetime as RealDatetime
from unittest.mock import patch
# Existing offline fixture installs the nhplug stub before importing run_cloud.
from test_midday_report import run_cloud as bot

class HourlyRuntimeTests(unittest.TestCase):
    def fixed(self, day, hour, minute=12):
        class Clock(RealDatetime):
            @classmethod
            def now(cls,tz=None): return RealDatetime(2026,10,day,hour,minute,tzinfo=tz)
        return Clock

    def test_weekend_and_offhours_do_nothing(self):
        for day,hour in [(23,8),(23,17),(24,11)]:
            with patch.dict(os.environ,{"HOURLY_OPERATION":"Y"}), \
                 patch.object(bot,"datetime",self.fixed(day,hour)), \
                 patch.object(bot,"_run_strategy_core") as core, \
                 patch.object(bot,"send_midday_report") as report, \
                 patch.object(bot,"NHClient") as client:
                bot.run_strategy()
                core.assert_not_called();report.assert_not_called();client.assert_not_called()

    def test_actual_hours_route_trading_and_reporting(self):
        for hour,trade,reporting in [(10,True,False),(15,True,True),(16,False,True)]:
            with patch.dict(os.environ,{"HOURLY_OPERATION":"Y"}), \
                 patch.object(bot,"datetime",self.fixed(23,hour)), \
                 patch.object(bot,"_run_strategy_core") as core, \
                 patch.object(bot,"send_midday_report") as report:
                bot.run_strategy()
                self.assertEqual(core.called,trade);self.assertEqual(report.called,reporting)

    def test_report_outside_window_has_no_state_or_broker_calls(self):
        with patch.object(bot,"datetime",self.fixed(23,18)), \
             patch.object(bot,"load_state") as state,patch.object(bot,"NHClient") as client:
            self.assertFalse(bot.send_midday_report(hourly=True))
            state.assert_not_called();client.assert_not_called()

    def test_closing_report_retries_and_preserves_positions(self):
        state=bot.default_state();state.update({"shares":32,"avg_price":105756})
        with patch.object(bot,"datetime",self.fixed(23,16)), \
             patch.object(bot,"load_state",side_effect=lambda:dict(state)), \
             patch.object(bot,"save_state",side_effect=lambda value:state.update(value)), \
             patch.object(bot,"NHClient") as client, \
             patch.object(bot,"send_telegram",return_value=False) as send:
            self.assertFalse(bot.send_midday_report(hourly=True))
            self.assertNotIn("last_hourly_report",state)
            send.return_value=True
            self.assertTrue(bot.send_midday_report(hourly=True))
            self.assertFalse(bot.send_midday_report(hourly=True))
            self.assertEqual(state["shares"],32);client.assert_not_called()
