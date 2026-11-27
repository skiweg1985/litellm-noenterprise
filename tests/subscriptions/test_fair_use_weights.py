"""Price resolution and consumption with the distribution's bundled price list."""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import litellm
    from litellm.proxy.extensions.chatgpt_subscriptions import fair_use_weights as weights
    PRICE_LIST = litellm.model_cost
except ModuleNotFoundError:
    root = Path(__file__).resolve().parents[2] / "upstream/litellm"
    source = root / "litellm/proxy/extensions/chatgpt_subscriptions/fair_use_weights.py"
    spec = importlib.util.spec_from_file_location("fair_use_weights", source)
    weights = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(weights)
    PRICE_LIST = json.loads((root / "model_prices_and_context_window.json").read_text())


class ModelPricingTests(unittest.TestCase):
    def setUp(self):
        reference = patch.dict(os.environ, {"CHATGPT_FAIR_USE_REFERENCE_MODEL": "gpt-6-sol"})
        reference.start()
        self.addCleanup(reference.stop)

    def test_list_prices_and_cached_inputs(self):
        pricing = weights.ModelPricing({}, PRICE_LIST)
        ids = ["gpt-6-sol", "gpt-6-astra", "gpt-6-luna"]
        rows = pricing.status(ids)["models"]
        self.assertEqual([row["model"] for row in rows], sorted(ids))
        for model, relative in zip(ids, (1, 5, 1/20)):
            count, consumption = pricing.measure({model: [7, 3, 2]})
            self.assertEqual(count, 12)
            self.assertAlmostEqual(consumption, relative * (7 * 2e-6 + 3 * 2e-7 + 2 * 1e-5))
            row = next(row for row in rows if row["model"] == model)
            self.assertEqual(row["source"], "list")
            self.assertAlmostEqual(row["weight"], relative)
        for model in ("gpt-6-sol", "chatgpt/gpt-6-sol", "openai/gpt-6-sol"):
            self.assertEqual(weights.model_id(model), "gpt-6-sol")

    def test_overrides_and_price_lookup_order(self):
        prices = {"input_cost_per_token": 2, "output_cost_per_token": 5}
        catalog = {**PRICE_LIST, "openai/alternate": prices, "openai/listed": prices,
                   "listed": {**prices, "input_cost_per_token": 7}, "alternate": {}}
        overrides = {"scaled": {"factor": 2}, "explicit": {"input": 1, "output": 9, "cache_read": 0},
                     "alias": {"like": "scaled"}, "explicit-alias": {"like": "explicit"},
                     "list-alias": {"like": "listed"}, "free": {"factor": 0}}
        pricing = weights.ModelPricing(overrides, catalog)
        cases = [("alternate", "list", 7), ("listed", "list", 12), ("list-alias", "like", 12),
                 ("explicit-alias", "like", 9), ("explicit", "prices", 9), ("free", "factor", 0),
                 ("alias", "like", 2 * (2e-7 + 1e-5)), ("scaled", "factor", 2 * (2e-7 + 1e-5))]
        for model, source, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(pricing.resolve(model)[1], source)
                self.assertAlmostEqual(pricing.measure({model: [0, 1, 1]})[1], expected)

    def test_configured_reference_and_missing_explicit_cache_rate(self):
        with patch.dict(os.environ, {"CHATGPT_FAIR_USE_REFERENCE_MODEL": "alternate"}):
            pricing = weights.ModelPricing(
                {"scaled": {"factor": 3}, "manual": {"input": 5, "output": 7}},
                {"openai/alternate": {"input_cost_per_token": 2, "output_cost_per_token": 4}},
            )
        self.assertFalse(pricing.reference_fallback)
        self.assertEqual(pricing.measure({"scaled": [0, 2, 1], "manual": [0, 1, 1]}), (5, 36))
        self.assertEqual(pricing.measure({None: [0, 2, 1]}), (3, 8))

    def test_unpriced_and_reference_fallback(self):
        overrides = {"alias": {"like": "unknown"}}
        for reference, fallback in [("gpt-6-sol", False), ("unknown", True), ("free-ref", True)]:
            with self.subTest(reference=reference), patch.dict(os.environ, {"CHATGPT_FAIR_USE_REFERENCE_MODEL": reference}):
                pricing = weights.ModelPricing(overrides, {**PRICE_LIST, "free-ref": {
                    "input_cost_per_token": 0, "output_cost_per_token": 0}})
                status = pricing.status(["unknown"])
                self.assertEqual(status["reference_model"], reference)
                self.assertIs(status["reference_fallback"], fallback)
                for row in status["models"]:
                    self.assertTrue(row["unpriced"])
                    self.assertEqual(row["weight"], 1)
                    self.assertIsNone(row["input"])
                    self.assertAlmostEqual(pricing.measure({row["model"]: [1, 2, 3]})[1], 2e-6 + 4e-7 + 3e-5)
