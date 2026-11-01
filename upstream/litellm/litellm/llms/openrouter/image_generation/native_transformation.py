"""OpenRouter's dedicated, buffered Image API."""

import math
import re
from typing import Any

import httpx

from litellm.llms.openrouter.common_utils import OpenRouterException
from litellm.types.llms.openai import OpenAIImageGenerationOptionalParams
from litellm.types.utils import (
    ImageObject,
    ImageResponse,
    ImageUsage,
    ImageUsageInputTokensDetails,
)

from .transformation import OpenRouterImageGenerationConfig


class OpenRouterNativeImageGenerationConfig(OpenRouterImageGenerationConfig):
    """Use native image requests while sharing OpenRouter authentication/errors."""

    def get_supported_openai_params(
        self, model: str
    ) -> list[OpenAIImageGenerationOptionalParams]:
        return [
            "size",
            "quality",
            "n",
            "response_format",
            "user",
            "background",
            "output_format",
            "output_compression",
            "seed",
            "aspect_ratio",
        ]

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        optional_params.update(non_default_params)
        return optional_params

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
    ) -> str:
        base = (api_base or "https://openrouter.ai/api/v1").rstrip("/")
        return base if base.endswith("/images") else f"{base}/images"

    def transform_image_generation_request(
        self,
        model: str,
        prompt: str,
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        params = dict(optional_params)
        # The shared HTTP handler consumes these as transport headers.
        params.pop("extra_headers", None)
        extra = params.pop("extra_body", {})
        if not isinstance(extra, dict):
            self._invalid("extra_body must be an object")
        if params.keys() & extra.keys():
            self._invalid(
                "Do not supply the same image setting both directly and in extra_body"
            )
        params.update(extra)
        allowed = set(self.get_supported_openai_params(model)) | {
            "resolution",
            "provider",
        }
        for key in params:
            if key not in allowed:
                self._invalid(f"Unsupported buffered Image API parameter: {key}")
        if params.pop("response_format", "b64_json") != "b64_json":
            self._invalid(
                "OpenRouter's Image API returns b64_json; response_format=url is unsupported"
            )
        enums = {
            "quality": {"auto", "low", "medium", "high", "xhigh", "max"},
            "output_format": {"png", "jpeg", "webp", "svg"},
            "background": {"auto", "transparent", "opaque"},
            "resolution": {"512", "768", "1K", "1.5K", "2K", "4K"},
        }
        for key, values in enums.items():
            if key in params and (
                not isinstance(params[key], str) or params[key] not in values
            ):
                self._invalid(
                    f"Unsupported {key}; accepted values: {', '.join(sorted(values))}"
                )
        for key, minimum, maximum in (("n", 1, 10), ("output_compression", 0, 100)):
            if key in params and (
                type(params[key]) is not int or not minimum <= params[key] <= maximum
            ):
                self._invalid(
                    f"{key} must be an integer between {minimum} and {maximum}"
                )
        if "seed" in params and type(params["seed"]) is not int:
            self._invalid("seed must be an integer")
        if "size" in params:
            size = params["size"]
            if size == "auto":
                # No fixed dimensions: leave selection to the provider.
                params.pop("size")
            elif not isinstance(size, str) or (
                size not in enums["resolution"]
                and not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size)
            ):
                self._invalid(
                    "size must be auto, a resolution tier, or WIDTHxHEIGHT pixels"
                )
        if "aspect_ratio" in params and (
            not isinstance(params["aspect_ratio"], str)
            or not re.fullmatch(r"auto|[1-9][0-9]*:[1-9][0-9]*", params["aspect_ratio"])
        ):
            self._invalid("aspect_ratio must be auto or WIDTH:HEIGHT")
        if "provider" in params and not isinstance(params["provider"], dict):
            self._invalid("provider must be an object")
        return {"model": model, "prompt": prompt, **params}

    @staticmethod
    def _invalid(message: str) -> None:
        raise OpenRouterException(status_code=400, message=message, headers={})

    def transform_image_generation_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ImageResponse,
        logging_obj: Any,
        request_data: dict,
        optional_params: dict,
        litellm_params: dict,
        encoding: Any,
        api_key: str | None = None,
        json_mode: bool | None = None,
    ) -> ImageResponse:
        try:
            response = raw_response.json()
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("data"), list)
                or not response["data"]
            ):
                raise ValueError("missing image data")
            images = []
            for item in response["data"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("b64_json"), str)
                    or not item["b64_json"]
                ):
                    raise ValueError("missing image bytes")
                media_type = item.get("media_type")
                if media_type is not None and not isinstance(media_type, str):
                    raise ValueError("invalid media type")
                images.append(
                    ImageObject(
                        b64_json=item["b64_json"],
                        provider_specific_fields={"media_type": media_type}
                        if media_type
                        else None,
                    )
                )
            model_response.data = images
            if "created" in response:
                if type(response["created"]) is not int or response["created"] < 0:
                    raise ValueError("invalid creation time")
                model_response.created = response["created"]
            usage = response.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("invalid usage")
            token_keys = ("prompt_tokens", "completion_tokens", "total_tokens")
            for key in token_keys:
                if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                    raise ValueError("invalid token count")
            # ImageResponse initializes zero usage; do not present it as observed.
            model_response.usage = None
            # Per-image billing often reports only cost. Missing counts are unknown.
            if all(key in usage for key in token_keys):
                model_response.usage = ImageUsage(
                    input_tokens=usage["prompt_tokens"],
                    output_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    input_tokens_details=ImageUsageInputTokensDetails(
                        image_tokens=0,
                        text_tokens=usage["prompt_tokens"],
                    ),
                )
            cost = usage.get("cost")
            if cost is not None:
                if (
                    type(cost) not in (float, int)
                    or not math.isfinite(cost)
                    or cost < 0
                ):
                    raise ValueError("invalid cost")
                model_response.hidden_params.setdefault("additional_headers", {})[
                    "llm_provider-x-litellm-response-cost"
                ] = float(cost)
            if isinstance(usage.get("cost_details"), dict):
                model_response.hidden_params["response_cost_details"] = usage[
                    "cost_details"
                ]
            model_response.hidden_params["model"] = model
            return model_response
        except (ValueError, TypeError, KeyError):
            # Never embed provider response bodies or validation inputs in errors.
            raise OpenRouterException(
                status_code=502,
                message="Invalid OpenRouter Image API response: expected image data and valid usage metadata",
                headers={},
            ) from None
