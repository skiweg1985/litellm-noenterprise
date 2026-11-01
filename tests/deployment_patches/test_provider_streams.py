"""Behavioral checks against the real Responses-to-chat bridge, using synthetic events."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from litellm.completion_extras.litellm_responses_transformation.transformation import (
    OpenAiResponsesToChatCompletionStreamIterator as Bridge,
)
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig


def assemble(events):
    bridge = Bridge(streaming_response=iter([]), sync_stream=True)
    names, arguments, ids = {}, {}, set()
    for event in events:
        chunk = bridge.chunk_parser(event)
        ids.add(chunk.id)
        for choice in chunk.choices or []:
            for call in (choice.delta.tool_calls or []) if choice.delta else []:
                if call.function.name:
                    names[call.index] = names.get(call.index, "") + call.function.name
                arguments[call.index] = arguments.get(call.index, "") + (call.function.arguments or "")
    return {index: (names.get(index, ""), arguments[index]) for index in sorted(arguments)}, ids


def stream(with_deltas=False, item_only=False, added_arguments=False):
    events = [{"type": "response.created", "response": {"id": "resp_synthetic"}}]
    # Non-contiguous output indexes test sequential Chat Completions indexes.
    for index, value in ((1, "alpha"), (3, "beta")):
        full = json.dumps({"value": value})
        item = {"type": "function_call", "id": f"fc_synthetic_{index}",
                "call_id": f"call_synthetic_{index}", "name": "echo",
                "arguments": full if added_arguments else ""}
        events.append({"type": "response.output_item.added", "output_index": index, "item": item})
        if with_deltas:
            for part in (full[:5], full[5:]):
                events.append({"type": "response.function_call_arguments.delta", "output_index": index,
                               "item_id": item["id"], "delta": part})
        if not item_only:
            events.append({"type": "response.function_call_arguments.done", "output_index": index,
                           "item_id": item["id"], "arguments": full})
        events.append({"type": "response.output_item.done", "output_index": index,
                       "item": {**item, "arguments": full}})
    return events


class ProviderStreams(unittest.TestCase):
    expected = {0: ("echo", '{"value": "alpha"}'), 1: ("echo", '{"value": "beta"}')}

    def test_done_only_parallel_arguments(self):
        calls, ids = assemble(stream())
        self.assertEqual(set(calls), {0, 1})
        self.assertEqual([json.loads(value)["value"] for _, value in calls.values()], ["alpha", "beta"])
        self.assertEqual([name for name, _ in calls.values()], ["echo", "echo"])
        self.assertEqual(len(ids), 1)

    def test_deltas_are_not_duplicated_by_done(self):
        self.assertEqual(assemble(stream(with_deltas=True))[0], self.expected)

    def test_output_item_done_is_sufficient(self):
        self.assertEqual(assemble(stream(item_only=True))[0], self.expected)

    def test_arguments_on_item_added_are_not_duplicated(self):
        self.assertEqual(assemble(stream(added_arguments=True))[0], self.expected)

    def test_final_output_recovery_is_request_local_and_keeps_output(self):
        config = ChatGPTResponsesAPIConfig()
        first, second = SimpleNamespace(), SimpleNamespace()
        item = {"type": "function_call", "name": "echo", "arguments": "{}"}
        with patch.object(OpenAIResponsesAPIConfig, "transform_streaming_response", side_effect=lambda *args, **kw: kw["parsed_chunk"] if kw else args[1]):
            config.transform_streaming_response("test", {"type": "response.output_item.done", "output_index": 2, "item": item}, first)
            final = {"type": "response.completed", "response": {"output": []}}
            self.assertEqual(config.transform_streaming_response("test", final, first)["response"]["output"], [item])
            unrelated = {"type": "response.completed", "response": {"output": []}}
            self.assertEqual(config.transform_streaming_response("test", unrelated, second)["response"]["output"], [])
            supplied = [{"type": "message", "content": []}]
            incomplete = {"type": "response.incomplete", "response": {"output": supplied}}
            self.assertEqual(config.transform_streaming_response("test", incomplete, first)["response"]["output"], supplied)


if __name__ == "__main__":
    unittest.main()
