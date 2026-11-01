import litellm
from litellm.llms.base_llm.image_generation.transformation import (
    BaseImageGenerationConfig,
)

from .native_transformation import OpenRouterNativeImageGenerationConfig
from .transformation import OpenRouterImageGenerationConfig

__all__ = ["OpenRouterImageGenerationConfig", "OpenRouterNativeImageGenerationConfig"]

# Existing chat/image models retain their previous request and response format.
# New dedicated image models default to the Image API, without a discovery call.
_CHAT_IMAGE_MODELS = frozenset(
    {
        "google/gemini-2.0-flash-exp",
        "google/gemini-2.0-flash-exp-image-generation",
        "google/gemini-2.5-flash-image-preview",
        "google/gemini-2.5-flash-image",
        "google/gemini-3-pro-image-preview",
        "google/gemini-3-pro-image",
        "google/gemini-3.1-flash-image-preview",
        "google/gemini-3.1-flash-image",
        "google/gemini-3.1-flash-lite-image",
        "google/gemini-nano-banana-2.1",
        "openai/gpt-5-image",
        "openai/gpt-5-image-mini",
        "openai/gpt-5.4-image-2",
        "openrouter/auto",
        "openrouter/auto-beta",
    }
)


def get_openrouter_image_generation_config(
    model: str, api_base: str | None = None
) -> BaseImageGenerationConfig:
    endpoint = (api_base or "").rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return OpenRouterImageGenerationConfig()
    if endpoint.endswith("/images"):
        return OpenRouterNativeImageGenerationConfig()
    base_model = model.split(":", 1)[0]
    model_info = litellm.model_cost.get(
        f"openrouter/{model}", litellm.model_cost.get(f"openrouter/{base_model}", {})
    )
    output_modalities = model_info.get("supported_output_modalities") or []
    if base_model in _CHAT_IMAGE_MODELS or {"text", "image"}.issubset(
        output_modalities
    ):
        return OpenRouterImageGenerationConfig()
    return OpenRouterNativeImageGenerationConfig()
