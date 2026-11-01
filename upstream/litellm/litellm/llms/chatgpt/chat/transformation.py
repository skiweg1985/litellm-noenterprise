from typing import Any, Final

from litellm.exceptions import AuthenticationError
from litellm.llms.openai.openai import OpenAIConfig
from litellm.types.llms.openai import AllMessageValues

from ..authenticator import Authenticator
from ..common_utils import (
    GetAccessTokenError,
    ensure_chatgpt_session_id,
    get_chatgpt_default_headers,
)
from .streaming_utils import ChatGPTToolCallNormalizer


class ChatGPTConfig(OpenAIConfig):
    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        custom_llm_provider: str = "openai",
    ) -> None:
        super().__init__()
        self.authenticator = Authenticator()

    def api_base_without_login(self) -> str:
        return self.authenticator.get_api_base()

    def _get_openai_compatible_provider_info(
        self,
        model: str,
        api_base: str | None,
        api_key: str | None,
        custom_llm_provider: str,
    ) -> tuple[str | None, str | None, str]:
        # Subscription authentication: resolve the provider without a token;
        # validate_environment fetches it per request.
        dynamic_api_base: Final = self.authenticator.get_api_base()
        return dynamic_api_base, api_key, custom_llm_provider

    def get_openai_compatible_provider_info(
        self,
        model: str,
        api_base: str | None,
        api_key: str | None,
        custom_llm_provider: str,
    ) -> tuple[str | None, str | None, str]:
        return self._get_openai_compatible_provider_info(model, api_base, api_key, custom_llm_provider)

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict:
        # Subscription authentication: api_key names the subscription; the token comes from its file.
        try:
            authenticator: Final = Authenticator.for_request(litellm_params, api_key)
            access_token: Final = authenticator.get_access_token()
        except GetAccessTokenError as e:
            raise AuthenticationError(model=model, llm_provider="chatgpt", message=str(e))
        validated_headers: Final = super().validate_environment(
            headers, model, messages, optional_params, litellm_params, access_token, api_base
        )

        account_id: Final = authenticator.get_account_id()
        session_id: Final = ensure_chatgpt_session_id(litellm_params)
        default_headers: Final = get_chatgpt_default_headers(access_token, account_id, session_id)
        return {**default_headers, **validated_headers}

    def post_stream_processing(self, stream: Any) -> Any:
        return ChatGPTToolCallNormalizer(stream)

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        optional_params = super().map_openai_params(non_default_params, optional_params, model, drop_params)
        optional_params.setdefault("stream", False)
        return optional_params
