"""The proxy offers no upstream release lookup and asks GitHub for nothing; no network."""
import unittest
from unittest.mock import patch

PATH = "/get/latest_release_info"


class UpgradeBannerRemovedTests(unittest.TestCase):
    def test_release_lookup_is_not_registered(self):
        from litellm.proxy.proxy_server import app

        self.assertNotIn(PATH, {getattr(route, "path", None) for route in app.routes})

    def test_request_makes_no_outbound_call(self):
        from fastapi.testclient import TestClient

        from litellm.proxy.proxy_server import app

        with patch("httpx.AsyncClient.send", side_effect=AssertionError("outbound request")) as send:
            response = TestClient(app).get(PATH)
        self.assertEqual(404, response.status_code)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
