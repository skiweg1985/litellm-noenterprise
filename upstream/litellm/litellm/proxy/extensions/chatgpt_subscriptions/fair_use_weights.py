"""Resolve subscription consumption rates against one price list and policy snapshot."""
import math
import os
from typing import NamedTuple, Optional


def model_id(model):
    if isinstance(model, str) and model:
        return model.removeprefix("chatgpt/").removeprefix("openai/") or None
    return None


class Rates(NamedTuple):
    input: float
    output: float
    cache_read: Optional[float]

    def consumption(self, counts):
        uncached, cached, output = counts
        cache_rate = self.input if self.cache_read is None else self.cache_read
        return uncached * self.input + cached * cache_rate + output * self.output


def _valid_price(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class ModelPricing:
    def __init__(self, overrides, price_list):
        self.overrides = overrides
        self.price_list = price_list
        self.reference_model = model_id(os.getenv("CHATGPT_FAIR_USE_REFERENCE_MODEL")) or "gpt-6-sol"
        reference = self.list_rates(self.reference_model)
        self.reference_fallback = reference is None or reference.input + reference.output == 0
        self.reference = Rates(2e-6, 1e-5, 2e-7) if self.reference_fallback else reference
        self.resolved = {}

    def list_rates(self, model):
        for key in (model, f"openai/{model}"):
            entry = self.price_list.get(key) or {}
            input_rate = entry.get("input_cost_per_token")
            output_rate = entry.get("output_cost_per_token")
            if _valid_price(input_rate) and _valid_price(output_rate):
                cache = entry.get("cache_read_input_token_cost")
                return Rates(input_rate, output_rate, cache if _valid_price(cache) else None)
        return None

    def resolve(self, model):
        if model not in self.resolved:
            self.resolved[model] = self._resolve(model)
        return self.resolved[model]

    def _resolve(self, model):
        rule = self.overrides.get(model, {})
        if "like" in rule:
            rates, source = self.resolve(rule["like"])
            return rates, "unpriced" if source == "unpriced" else "like"
        if "factor" in rule:
            factor = rule["factor"]
            ref = self.reference
            return Rates(ref.input * factor, ref.output * factor,
                         None if ref.cache_read is None else ref.cache_read * factor), "factor"
        if "input" in rule:
            return Rates(rule["input"], rule["output"], rule.get("cache_read")), "prices"
        rates = self.list_rates(model) if model else None
        return (rates, "list") if rates else (self.reference, "unpriced")

    def measure(self, models):
        tokens = sum(sum(counts) for counts in models.values())
        consumption = math.fsum(self.resolve(model)[0].consumption(counts) for model, counts in models.items())
        return tokens, consumption

    def status(self, models):
        rows = []
        for model in sorted(set(models) | self.overrides.keys()):
            rates, source = self.resolve(model)
            unpriced = source == "unpriced"
            rows.append(dict(model=model, input=None if unpriced else rates.input,
                             output=None if unpriced else rates.output,
                             cache_read=None if unpriced else rates.cache_read, source=source,
                             weight=(rates.input + rates.output) / (self.reference.input + self.reference.output),
                             unpriced=unpriced))
        return dict(reference_model=self.reference_model, reference_fallback=self.reference_fallback,
                    overrides=self.overrides, models=rows)
