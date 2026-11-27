"""Pure fair-use calculations also run outside the proxy image."""
import importlib.util
from pathlib import Path
import unittest

try:
    from litellm.proxy.extensions.chatgpt_subscriptions import fair_use_math as math
except ModuleNotFoundError:
    source = Path(__file__).resolve().parents[2] / "upstream/litellm/litellm/proxy/extensions/chatgpt_subscriptions/fair_use_math.py"
    spec = importlib.util.spec_from_file_location("fair_use_math", source)
    math = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(math)


class FairUseMathTests(unittest.TestCase):
    def test_threshold_and_share_are_inclusive(self):
        usage = {"heavy": (5, 0.5), "light": (10, 0.1)}
        for used, expected in [(59, False), (60, True), (90, True)]:
            users = math.window_users(usage, {}, used, 60, True)
            self.assertIs(users["heavy"]["throttled"], expected)
            self.assertFalse(users["light"]["throttled"])
            self.assertEqual(users["heavy"]["allowance"], 50)
        self.assertFalse(math.window_users(usage, {}, 100, 60, False)["heavy"]["throttled"])

    def test_active_zero_tokens_single_user_and_exempt_factor(self):
        self.assertFalse(math.window_users({"solo": (10, 1.0)}, {}, 100, 1, True)["solo"]["throttled"])
        users = math.window_users({"solo": (100, 1.0), "exempt": (0, 0.0)}, {"exempt": 0}, 100, 1, True)
        self.assertFalse(users["solo"]["throttled"])
        self.assertFalse(users["exempt"]["throttled"])
        users = math.window_users({"a": (0, 0.0), "b": (0, 0.0)}, {"a": 3}, 99, 60, True)
        self.assertEqual((users["a"]["share"], users["a"]["allowance"]), (0, 75))
        users = math.window_users({"a": (50, 0.5), "b": (0, 0.0), "free": (50, 0.5)}, {"free": 0}, 90, 60, True)
        self.assertEqual(users["a"]["share"], 45)
        self.assertFalse(users["free"]["throttled"])

    def test_quota_age_reset_and_malformed_windows(self):
        window = dict(used_percent=85, window_seconds=3600, reset_at=2000)
        snapshot = dict(fetched_at=1000, primary=window, secondary=window)
        self.assertEqual(math.quota_state(snapshot, 1180, 60), "current")
        self.assertEqual(math.quota_state(snapshot, 1181, 60), "stale")
        self.assertEqual(math.quota_state(snapshot, 2000, 600), "stale")
        for quota in [None, {}, ["invalid"], {**snapshot, "secondary": None}, {**snapshot, "fetched_at": float("nan")}]:
            self.assertEqual(math.quota_state(quota, 1000, 60), "unavailable")
        for field, value in [("used_percent", float("nan")), ("used_percent", 101), ("window_seconds", True),
                             ("reset_at", float("inf")), ("window_seconds", -1)]:
            invalid = {**snapshot, "primary": {**window, field: value}}
            self.assertEqual(math.quota_state(invalid, 1000, 60), "unavailable")

    def test_window_labels(self):
        self.assertEqual(math.window_name(dict(id="primary", window_seconds=17999)), "5-hour")
        self.assertEqual(math.window_name(dict(id="primary", window_seconds=300)), "short")
        self.assertEqual(math.window_name(dict(id="secondary", window_seconds=604800)), "weekly")
        self.assertEqual(math.utc(3600), "1970-01-01T01:00:00Z")
