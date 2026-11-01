"""Image-generation API contract against a local synthetic OpenRouter service.

The public LiteLLM sync/async image APIs are the seam; only the external HTTP
service is substituted. No provider credential or billable request is used.
"""

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import litellm

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII="


class Backend(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.calls.append((self.path, body, self.headers.get("authorization")))
        self.server.last_headers = dict(self.headers)
        if self.path == "/api/v1/chat/completions" and body.get("model", "").startswith(
            "google/gemini"
        ):
            self.reply(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "images": [
                                    {
                                        "image_url": {
                                            "url": "data:image/png;base64," + PNG
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 16,
                        "completion_tokens_details": {"image_tokens": 272},
                        "total_tokens": 288,
                        "cost": 0.011,
                    },
                },
            )
        elif self.path != "/api/v1/images":
            self.reply(
                404, {"error": {"message": "This image model requires /api/v1/images"}}
            )
        else:
            self.reply(self.server.status, self.server.response)

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def log_message(self, *args):
        pass


class OpenRouterImageTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.server.calls = []
        self.server.status = 200
        self.server.response = {
            "created": 1748372400,
            "data": [{"b64_json": PNG, "media_type": "image/png"}],
            "usage": {
                "prompt_tokens": 16,
                "completion_tokens": 272,
                "total_tokens": 288,
                "cost": 0.011,
            },
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_base = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def generate(self, **kwargs):
        return litellm.image_generation(
            **{
                "model": "openrouter/openai/gpt-image-2.5-sunburst",
                "prompt": "Synthetic image",
                "api_key": "synthetic-image-key",
                "api_base": self.api_base,
                "num_retries": 0,
                **kwargs,
            }
        )

    def test_existing_chat_image_model_keeps_its_transport_and_variant(self):
        response = self.generate(
            model="openrouter/google/gemini-2.5-flash-image:free",
            size="1536x1024",
            quality="high",
            n=1,
        )
        self.assertEqual(self.server.calls[0][0], "/api/v1/chat/completions")
        self.assertEqual(
            self.server.calls[0][1],
            {
                "model": "google/gemini-2.5-flash-image:free",
                "messages": [{"role": "user", "content": "Synthetic image"}],
                "image_config": {"aspect_ratio": "3:2", "image_size": "4K"},
                "n": 1,
            },
        )
        self.assertEqual(response.data[0].b64_json, PNG)
        self.assertEqual(response.usage.output_tokens, 272)

    def test_explicit_chat_endpoint_retains_unknown_legacy_model(self):
        self.generate(
            model="openrouter/google/gemini-legacy-fixture",
            api_base=self.api_base + "/chat/completions/",
        )
        self.assertEqual(self.server.calls[0][0], "/api/v1/chat/completions")
        self.assertIn("messages", self.server.calls[0][1])

    def test_explicit_native_endpoint_can_use_existing_chat_image_model(self):
        self.generate(
            model="openrouter/google/gemini-2.5-flash-image",
            api_base=self.api_base + "/images/",
            size="1024x1024",
        )
        self.assertEqual(self.server.calls[0][0], "/api/v1/images")
        self.assertEqual(self.server.calls[0][1]["size"], "1024x1024")
        self.assertNotIn("messages", self.server.calls[0][1])

    def test_native_parameter_contract(self):
        self.generate(
            response_format="b64_json",
            size="2K",
            quality="max",
            n=2,
            output_format="webp",
            output_compression=80,
            background="transparent",
            extra_body={
                "resolution": "2K",
                "aspect_ratio": "16:9",
                "provider": {"only": ["fixture"]},
            },
        )
        body = self.server.calls[0][1]
        self.assertNotIn("response_format", body)
        self.assertNotIn("extra_body", body)
        self.assertEqual(body["size"], "2K")
        self.assertEqual(body["quality"], "max")
        self.assertEqual(body["resolution"], "2K")
        self.assertEqual(body["aspect_ratio"], "16:9")
        self.assertEqual(body["provider"], {"only": ["fixture"]})
        self.assertEqual(body["output_format"], "webp")
        self.assertEqual(body["output_compression"], 80)
        self.assertEqual(body["background"], "transparent")
        self.assertEqual(body["n"], 2)

    def test_unsupported_native_parameters_fail_before_dispatch(self):
        invalid = [
            {"response_format": "url"},
            {"quality": "hd"},
            {"quality": "standard"},
            {"size": "unrecognized"},
            {"n": 0},
            {"n": True},
            {"n": 11},
            {"output_compression": 101},
            {"background": "invisible"},
            {"extra_body": {"stream": True}},
            {"extra_body": {"input_references": ["anything"]}},
            {"extra_body": {"model": "replacement"}},
            {"extra_body": {"prompt": "replacement"}},
            {"extra_body": {"unknown_setting": "secret-value"}},
        ]
        for params in invalid:
            with (
                self.subTest(params=params),
                self.assertRaises(litellm.BadRequestError),
            ):
                self.generate(**params)
        self.assertEqual(self.server.calls, [])

    def test_malformed_response_does_not_expose_image_payload(self):
        invalid = [
            b"not-json-secret",
            [],
            {"data": []},
            {"data": [{"b64_json": ""}]},
            {"data": [{"b64_json": {"private-payload": PNG}}]},
            {"data": [{"b64_json": PNG}], "usage": {"cost": "secret-cost"}},
        ]
        for body in invalid:
            self.server.response = body
            with self.subTest(body=body), self.assertRaises(litellm.APIError) as error:
                self.generate()
            self.assertNotIn(PNG, str(error.exception))
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(error.exception.status_code, 502)

    def test_cost_without_token_counts_does_not_invent_usage(self):
        self.server.response["usage"] = {"cost": 0.04}
        response = self.generate()
        self.assertIsNone(response.usage)
        self.assertEqual(
            response.hidden_params["additional_headers"][
                "llm_provider-x-litellm-response-cost"
            ],
            0.04,
        )

    def test_extra_headers_are_transport_metadata(self):
        self.generate(
            extra_headers={
                "HTTP-Referer": "https://example.test",
                "X-Title": "Synthetic image test",
            }
        )
        self.assertEqual(
            self.server.last_headers.get("HTTP-Referer"), "https://example.test"
        )
        self.assertNotIn("extra_headers", self.server.calls[0][1])
        self.assertNotIn("headers", self.server.calls[0][1])

    def test_provider_error_keeps_actionable_status_and_message(self):
        self.server.status = 400
        self.server.response = {
            "error": {"message": "This provider does not support the requested size"}
        }
        with self.assertRaises(litellm.BadRequestError) as error:
            self.generate(size="2048x2048")
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("does not support the requested size", str(error.exception))

    def test_async_native_request_with_explicit_provider(self):
        response = asyncio.run(
            litellm.aimage_generation(
                model="openai/gpt-image-2.5-sunburst",
                custom_llm_provider="openrouter",
                prompt="Synthetic image",
                api_key="synthetic-image-key",
                api_base=self.api_base,
            )
        )
        self.assertEqual(self.server.calls[0][0], "/api/v1/images")
        self.assertEqual(response.data[0].b64_json, PNG)

    def test_media_type_and_multiple_images_are_preserved(self):
        self.server.response["data"] = [
            {"b64_json": PNG, "media_type": "image/webp"},
            {"b64_json": PNG},
        ]
        response = self.generate(n=2)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(
            response.data[0].provider_specific_fields["media_type"], "image/webp"
        )
        self.assertIsNone(response.data[1].provider_specific_fields)

    def test_global_api_base_selects_explicit_legacy_transport(self):
        previous = litellm.api_base
        try:
            litellm.api_base = self.api_base + "/chat/completions"
            self.generate(
                model="openrouter/google/gemini-global-fixture", api_base=None
            )
            self.assertEqual(self.server.calls[0][0], "/api/v1/chat/completions")
        finally:
            litellm.api_base = previous

    def test_openrouter_catalog_chat_image_entry_keeps_chat_transport(self):
        model = "openrouter/google/gemini-catalog-fixture"
        litellm.register_model(
            {
                model: {
                    "litellm_provider": "openrouter",
                    "mode": "chat",
                    "supported_output_modalities": ["text", "image"],
                }
            }
        )
        try:
            self.generate(model=model)
            self.assertEqual(self.server.calls[0][0], "/api/v1/chat/completions")
        finally:
            litellm.model_cost.pop(model, None)

    def test_auto_size_leaves_dimensions_to_provider(self):
        self.generate(size="auto")
        self.assertNotIn("size", self.server.calls[0][1])
        self.assertNotIn("aspect_ratio", self.server.calls[0][1])

    def test_native_image_request_and_response(self):
        response = self.generate(size="1536x1024", quality="high", n=1)
        self.assertEqual(
            self.server.calls,
            [
                (
                    "/api/v1/images",
                    {
                        "model": "openai/gpt-image-2.5-sunburst",
                        "prompt": "Synthetic image",
                        "size": "1536x1024",
                        "quality": "high",
                        "n": 1,
                    },
                    "Bearer synthetic-image-key",
                )
            ],
        )
        self.assertEqual(response.data[0].b64_json, PNG)
        self.assertEqual(response.created, 1748372400)
        self.assertEqual(response.usage.output_tokens, 272)
        self.assertEqual(response.usage.input_tokens, 16)
        self.assertEqual(response.usage.total_tokens, 288)
        self.assertEqual(
            response.hidden_params["additional_headers"][
                "llm_provider-x-litellm-response-cost"
            ],
            0.011,
        )


if __name__ == "__main__":
    unittest.main()
