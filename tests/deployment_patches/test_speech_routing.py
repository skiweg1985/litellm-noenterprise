"""Provider selection tests against the real speech implementation; no network."""
import importlib
import unittest
from unittest.mock import Mock, patch

import litellm

main = importlib.import_module("litellm.main")


class SpeechRoutingTests(unittest.TestCase):
    def test_openrouter_uses_openai_audio_transport_with_its_own_endpoint(self):
        response = object()
        with patch.object(main.openai_chat_completions, "audio_speech", return_value=response) as transport:
            result = main.speech.__wrapped__(
                model="openrouter/openai/tts-1", input="Synthetic speech", voice="alloy",
                api_key="synthetic-key", api_base="http://example.test/api/v1",
                litellm_logging_obj=Mock(),
            )
        self.assertIs(result, response)
        self.assertEqual(transport.call_args.kwargs["api_base"], "http://example.test/api/v1")
        self.assertEqual(transport.call_args.kwargs["api_key"], "synthetic-key")
        self.assertEqual(transport.call_args.kwargs["model"], "openai/tts-1")

    def test_openai_routing_is_retained(self):
        response = object()
        with patch.object(main.openai_chat_completions, "audio_speech", return_value=response) as transport:
            result = main.speech.__wrapped__(
                model="openai/tts-1", input="Synthetic speech", voice="alloy",
                api_key="synthetic-key", litellm_logging_obj=Mock(),
            )
        self.assertIs(result, response)
        self.assertEqual(transport.call_args.kwargs["model"], "tts-1")

    def test_openrouter_still_requires_voice(self):
        with self.assertRaises(litellm.BadRequestError):
            main.speech.__wrapped__(
                model="openrouter/openai/tts-1", input="Synthetic speech",
                api_key="synthetic-key", litellm_logging_obj=Mock(),
            )


if __name__ == "__main__":
    unittest.main()
