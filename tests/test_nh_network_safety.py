"""Offline NH API error-classification, secret-redaction and bounded-retry regressions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nh_client
import run_cloud


class FakeNhplugError(Exception):
    pass


class NetworkSafetyTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(nh_client.NHClient)

    def test_url_and_credentials_never_in_exception_message(self):
        raw = ("[auth] token failed: HTTPSConnectionPool(host='api.nhplug.com', port=8443): "
               "Max retries exceeded with url: /oauth2/token?appkey=FAKE_API_KEY&"
               "appsecretkey=FAKE_SECRET&grant_type=client_credentials "
               "(Caused by ConnectTimeoutError(... timed out))")
        with patch.object(nh_client, "NhplugError", FakeNhplugError), \
             patch.object(nh_client, "call", side_effect=FakeNhplugError(raw)), \
             patch.object(nh_client.time, "sleep") as sleeping:
            with self.assertRaises(nh_client.NHClientError) as caught:
                self.client._call("/krstock/inquiry/v1/assetStatus", {}, "자산조회")
        rendered = str(caught.exception)
        self.assertIn("[network_timeout]", rendered)
        self.assertNotIn("FAKE_API_KEY", rendered)
        self.assertNotIn("FAKE_SECRET", rendered)
        self.assertNotIn("oauth2/token", rendered)
        sleeping.assert_called_once()

    def test_read_only_retry_exactly_once(self):
        with patch.object(nh_client, "NhplugError", FakeNhplugError), \
             patch.object(nh_client, "call", side_effect=[
                 FakeNhplugError("ConnectTimeoutError: timed out"), {"Output_0": {}}
             ]) as request, patch.object(nh_client.time, "sleep"):
            result = self.client._call("/krstock/inquiry/v1/assetStatus", {}, "자산조회")
        self.assertEqual(result, {"Output_0": {}})
        self.assertEqual(request.call_count, 2)

    def test_authentication_rejection_does_not_retry(self):
        with patch.object(nh_client, "NhplugError", FakeNhplugError), \
             patch.object(nh_client, "call", side_effect=FakeNhplugError("401 invalid_client")) as request, \
             patch.object(nh_client.time, "sleep") as sleeping:
            with self.assertRaisesRegex(nh_client.NHClientError, "auth_rejected"):
                self.client._call("/krstock/inquiry/v1/assetStatus", {}, "자산조회")
        request.assert_called_once()
        sleeping.assert_not_called()

    def test_order_is_never_retried(self):
        with patch.object(nh_client, "NhplugError", FakeNhplugError), \
             patch.object(nh_client, "call", side_effect=FakeNhplugError("ConnectTimeoutError: timed out")) as request, \
             patch.object(nh_client.time, "sleep") as sleeping:
            with self.assertRaisesRegex(nh_client.NHClientError, "network_timeout"):
                self.client._call("/krstock/order/v1/cashBuy", {}, "매수 주문")
        request.assert_called_once()
        sleeping.assert_not_called()

    def test_message_sanitizer_never_echoes_secrets(self):
        raw = "https://api.nhplug.com/oauth2/token?appkey=KEY123&appsecretkey=SECRET456"
        with patch.dict(os.environ, {"NH_APP_KEY": "KEY123", "NH_APP_SECRET": "SECRET456"}):
            clean = nh_client.redact_sensitive(raw)
        self.assertNotIn("KEY123", clean)
        self.assertNotIn("SECRET456", clean)
        self.assertNotIn("https://", clean)

    def test_classification_distinguishes_rate_limit_and_server_error(self):
        self.assertEqual(nh_client.classify_nh_failure("429 Too Many Requests"), "rate_limited")
        self.assertEqual(nh_client.classify_nh_failure("503 Service Unavailable"), "temporary_server_error")
        self.assertEqual(nh_client.classify_nh_failure("403 Forbidden"), "auth_rejected")

    def test_consecutive_failures_are_persisted_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nh_api_health.json"
            with patch.object(run_cloud, "API_HEALTH_FILE", str(path)):
                self.assertEqual(run_cloud.update_api_health(failed=True), 1)
                self.assertEqual(run_cloud.update_api_health(failed=True), 2)
                self.assertEqual(json.loads(path.read_text())["consecutive_failures"], 2)
                self.assertEqual(run_cloud.update_api_health(failed=False), 0)
                self.assertEqual(json.loads(path.read_text())["consecutive_failures"], 0)


if __name__ == "__main__":
    unittest.main()
