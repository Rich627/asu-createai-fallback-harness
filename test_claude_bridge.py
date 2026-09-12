"""Offline tests for the Claude Code -> CreateAI fallback path."""

import io
import json
import unittest
import urllib.request

import anthropic_bridge
import claude_router
from anthropic_bridge import ToolMap, message_events, translate
from model_map import KNOWN_MODELS, Resolver, resolve
from bridge import BridgeError, dumps
from claude_router import Fallback, PrimaryQuota, RouterServer, is_quota, quota_window


def sse(chunks):
    body = "".join("data: " + dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return io.BytesIO(body.encode())


def text_chunk(text, finish=None):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


def tool_chunk(name, arguments, index=0, call_id="call_1", finish=None):
    return {"choices": [{"index": 0, "finish_reason": finish, "delta": {"tool_calls": [
        {"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}}]}}]}


class FakeUpstream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.bodies = []

    def open(self, path, body=None):
        self.bodies.append(body)
        return sse(self.chunks)


TOOL = {"name": "Bash", "description": "Run a command",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                         "$schema": "http://json-schema.org/draft-07/schema#"}}


class TranslateTest(unittest.TestCase):
    def body(self, request, model="aws/claude5_opus"):
        return translate(request, model)[0]

    def test_system_and_text(self):
        body = self.body({"system": [{"type": "text", "text": "You are Claude Code."}],
                          "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(body["messages"][0], {"role": "system", "content": "You are Claude Code."})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hi"})
        self.assertTrue(body["stream"])

    def test_tool_call_round_trip_order(self):
        request = {"tools": [TOOL], "messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "opaque", "signature": "x"},
                {"type": "text", "text": "Running it."},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "a.txt"}]},
                {"type": "text", "text": "thanks"}]}]}
        body = self.body(request)
        self.assertEqual([message["role"] for message in body["messages"]],
                         ["user", "assistant", "tool", "user"])
        self.assertEqual(body["messages"][1]["content"], "Running it.")
        self.assertEqual(body["messages"][1]["tool_calls"][0]["function"]["name"], "Bash")
        self.assertEqual(body["messages"][2], {"role": "tool", "tool_call_id": "toolu_1", "content": "a.txt"})
        self.assertNotIn("$schema", body["tools"][0]["function"]["parameters"])

    def test_error_result_and_image(self):
        body = self.body({"messages": [
            {"role": "user", "content": [{"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "AAA"}}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "boom", "is_error": True}]}]})
        self.assertEqual(body["messages"][0]["content"][0]["image_url"]["url"],
                         "data:image/png;base64,AAA")
        self.assertEqual(body["messages"][-1]["content"], "Error: boom")

    def test_replayed_tool_gets_a_definition(self):
        body = self.body({"messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Gone", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]})
        self.assertEqual([tool["function"]["name"] for tool in body["tools"]], ["Gone"])
        self.assertEqual(body["tool_choice"], "auto")

    def test_tool_choice_none_without_history_drops_tools(self):
        body = self.body({"tools": [TOOL], "tool_choice": {"type": "none"},
                          "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)

    def test_tool_choice_none_with_history_keeps_tools(self):
        body = self.body({"tools": [TOOL], "tool_choice": {"type": "none"}, "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]})
        self.assertEqual(body["tool_choice"], "auto")

    def test_tool_choice_matches_what_each_model_family_accepts(self):
        request = {"tools": [TOOL], "tool_choice": {"type": "tool", "name": "Bash"},
                   "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(translate(request, "aws/claude5_opus")[0]["tool_choice"],
                         {"type": "function", "function": {"name": "Bash"}})
        # CreateAI's OpenAI-hosted models answer a forced single tool with HTTP 500.
        self.assertEqual(translate(request, "openai/gpt5_6_sol")[0]["tool_choice"], "auto")
        request["tool_choice"] = {"type": "none"}
        request["messages"] = [{"role": "user", "content": "x"},
                               {"role": "assistant", "content": [
                                   {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
                               {"role": "user", "content": [
                                   {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
        self.assertEqual(translate(request, "openai/gpt5_6_sol")[0]["tool_choice"], "none")
        # Bedrock-hosted models answer "none" with HTTP 500.
        self.assertEqual(translate(request, "aws/claude5_opus")[0]["tool_choice"], "auto")

    def test_long_mcp_tool_name_round_trips(self):
        name = "mcp__" + "s" * 70 + "__tool"
        toolmap = ToolMap([{"name": name, "input_schema": {"type": "object"}}])
        wire = toolmap.tools[0]["function"]["name"]
        self.assertLessEqual(len(wire), 64)
        self.assertEqual(toolmap.original(wire), name)

    def test_hosted_tools_are_dropped(self):
        toolmap = ToolMap([{"type": "web_search_20250305", "name": "web_search"}, TOOL])
        self.assertEqual([tool["function"]["name"] for tool in toolmap.tools], ["Bash"])

    def test_model_mapping_follows_the_users_choice(self):
        default = anthropic_bridge.DEFAULT_MODEL
        for requested, expected in (("claude-opus-5", "aws/claude5_opus"),
                                    ("claude-sonnet-5", "aws/claude5_sonnet"),
                                    ("claude-haiku-4-5-20251001", "aws/claude4_5_haiku"),
                                    ("claude-opus-4-1-20250805", "aws/claude4_1_opus"),
                                    ("gpt-5.6-sol", "openai/gpt5_6_sol"),
                                    ("gpt-6-astra", "openai/gpt6_astra")):
            self.assertEqual(resolve(requested, KNOWN_MODELS, default), expected, requested)
        self.assertEqual(resolve("something-else", KNOWN_MODELS, default), default)
        # A model CreateAI has not got yet falls back to the newest of the same family.
        self.assertEqual(resolve("claude-opus-9", KNOWN_MODELS, default), "aws/claude5_opus")

    def test_resolver_survives_a_missing_upstream(self):
        resolver = Resolver(lambda: None, "aws/claude5_opus")
        self.assertEqual(resolver.target("claude-sonnet-5"), "aws/claude5_sonnet")
        self.assertEqual(resolver.target("claude-sonnet-5", "aws/claude4_8_opus"), "aws/claude4_8_opus")


class EventTest(unittest.TestCase):
    def events(self, chunks, request=None):
        upstream = FakeUpstream(chunks)
        result = []
        events = list(message_events(upstream, request or {"model": "claude-opus-5", "messages": [
            {"role": "user", "content": "hi"}]}, "aws/claude5_opus", result))
        return events, result[0] if result else None, upstream

    def test_text_stream(self):
        events, message, _ = self.events([text_chunk("Hel"), text_chunk("lo", "stop"),
                                          {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}])
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "message_start")
        self.assertEqual(kinds[-1], "message_stop")
        self.assertEqual([event["delta"]["text"] for event in events
                          if event["type"] == "content_block_delta"], ["Hel", "lo"])
        self.assertEqual(message["content"], [{"type": "text", "text": "Hello"}])
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertEqual(message["usage"]["input_tokens"], 7)
        self.assertEqual(message["model"], "claude-opus-5")

    def test_tool_use_stream(self):
        request = {"model": "claude-opus-5", "tools": [TOOL],
                   "messages": [{"role": "user", "content": "ls"}]}
        events, message, _ = self.events(
            [text_chunk("ok"), tool_chunk("Bash", '{"command":'), tool_chunk("", '"ls"}', finish="tool_calls")],
            request)
        blocks = [event for event in events if event["type"] == "content_block_start"]
        self.assertEqual([block["content_block"]["type"] for block in blocks], ["text", "tool_use"])
        self.assertEqual(blocks[1]["index"], 1)
        self.assertEqual(message["content"][1],
                         {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}})
        self.assertEqual(message["stop_reason"], "tool_use")

    def test_invalid_tool_arguments_stop_the_turn(self):
        request = {"model": "claude-opus-5", "tools": [TOOL], "messages": [{"role": "user", "content": "ls"}]}
        with self.assertRaises(BridgeError):
            self.events([tool_chunk("Bash", "{not json", finish="tool_calls")], request)

    def test_truncated_stream_is_an_error(self):
        upstream = FakeUpstream([])
        upstream.open = lambda path, body=None: io.BytesIO(b"data: {}\n\n")
        with self.assertRaises(BridgeError):
            list(message_events(upstream, {"messages": [{"role": "user", "content": "hi"}]}, "m"))


class RetryTest(unittest.TestCase):
    def upstream(self, statuses):
        from bridge import Upstream
        upstream = Upstream("https://example.invalid/v1", "token")
        self.calls = []

        def request(path, body=None):
            status = statuses[len(self.calls)]
            self.calls.append(path)
            if status != 200:
                raise BridgeError(f"ASU HTTP {status}", status)
            return sse([text_chunk("ok", "stop")])

        upstream.request = request
        return upstream

    def test_transient_server_error_is_retried(self):
        upstream = self.upstream([500, 200])
        with upstream.open("/chat/completions", {}, backoff=0):
            pass
        self.assertEqual(len(self.calls), 2)

    def test_retries_are_bounded_and_the_error_survives(self):
        upstream = self.upstream([503, 502, 500])
        with self.assertRaises(BridgeError):
            upstream.open("/chat/completions", {}, backoff=0)
        self.assertEqual(len(self.calls), 3)

    def test_client_errors_are_not_retried(self):
        upstream = self.upstream([400])
        with self.assertRaises(BridgeError):
            upstream.open("/chat/completions", {}, backoff=0)
        self.assertEqual(len(self.calls), 1)


class QuotaTest(unittest.TestCase):
    def test_usage_limit_message(self):
        payload = {"error": {"type": "rate_limit_error", "message": "Claude AI usage limit reached|1789200000"}}
        self.assertTrue(is_quota(429, payload, {}))

    def test_unified_status_header(self):
        self.assertTrue(is_quota(429, {"error": {"type": "rate_limit_error", "message": "slow down"}},
                                 {"anthropic-ratelimit-unified-status": "rejected"}))

    def test_short_rate_limit_is_not_a_usage_limit(self):
        self.assertFalse(is_quota(429, {"error": {"type": "rate_limit_error",
                                                  "message": "Number of requests has exceeded"}}, {}))

    def test_overloaded_and_auth_are_not_quota(self):
        self.assertFalse(is_quota(529, {"error": {"type": "overloaded_error", "message": "overloaded"}}, {}))
        self.assertFalse(is_quota(401, {"error": {"type": "authentication_error", "message": "bad token"}}, {}))

    def test_low_credit_balance(self):
        self.assertTrue(is_quota(400, {"error": {"type": "invalid_request_error",
                                                 "message": "Your credit balance is too low"}}, {}))

    def test_window_from_headers(self):
        self.assertEqual(quota_window({}, {"retry-after": "120"}), 120)
        self.assertEqual(quota_window({}, {}), claude_router.DEFAULT_WINDOW)

    def test_fallback_window_expires(self):
        fallback = Fallback()
        self.assertFalse(fallback.active())
        fallback.engage(PrimaryQuota(60, "test"))
        self.assertTrue(fallback.active())
        fallback.until = 0
        self.assertFalse(fallback.active())


class QuotaPrimary:
    def __init__(self):
        self.calls = 0

    def open(self, method, path, body, headers):
        self.calls += 1
        raise PrimaryQuota(300, "HTTP 429 rate_limit_error: usage limit reached")


class ServerTest(unittest.TestCase):
    def test_usage_limit_switches_the_live_request_to_createai(self):
        upstream = FakeUpstream([text_chunk("from createai", "stop")])
        server = RouterServer(upstream, QuotaPrimary(), model="aws/claude5_opus", port=0).start()
        try:
            payload = dumps({"model": "claude-opus-5", "max_tokens": 64, "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}).encode()
            request = urllib.request.Request(server.base_url + "/v1/messages?beta=true", data=payload,
                                             headers={"Authorization": "Bearer sk-ant-test",
                                                      "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.headers["X-ASU-Fallback"], "aws/claude5_opus")
                body = response.read().decode()
            self.assertIn("from createai", body)
            self.assertIn("event: message_stop", body)
            self.assertEqual(upstream.bodies[0]["model"], "aws/claude5_opus")
            self.assertTrue(server.fallback.active())
            with urllib.request.urlopen(server.base_url + "/health", timeout=5) as response:
                self.assertTrue(json.load(response)["fallback_active"])
            # The exhausted primary is not retried while the window is open.
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            self.assertEqual(server.primary.calls, 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_token_relays_the_limit_instead_of_crashing(self):
        server = RouterServer(None, QuotaPrimary(), port=0).start()
        try:
            payload = dumps({"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}).encode()
            request = urllib.request.Request(server.base_url + "/v1/messages", data=payload,
                                             headers={"Authorization": "Bearer sk-ant-test"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 429)
            with urllib.request.urlopen(server.base_url + "/health", timeout=5) as response:
                self.assertFalse(json.load(response)["createai_ready"])
        finally:
            server.shutdown()
            server.server_close()

    def test_forcing_one_instance_leaves_the_global_flag_alone(self):
        upstream = FakeUpstream([text_chunk("forced", "stop"),
                                 {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}])
        primary = QuotaPrimary()
        logged = []
        original = claude_router.log_usage
        claude_router.log_usage = lambda model, stats: logged.append(
            (model, stats.get("input_tokens"), stats.get("output_tokens")))
        self.addCleanup(setattr, claude_router, "log_usage", original)
        server = RouterServer(upstream, primary, port=0, forced=True).start()
        try:
            payload = dumps({"model": "claude-opus-5", "max_tokens": 16,
                             "messages": [{"role": "user", "content": "hi"}]}).encode()
            request = urllib.request.Request(server.base_url + "/v1/messages", data=payload,
                                             headers={"Authorization": "Bearer sk-ant-test"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(response.headers["X-ASU-Fallback"], "aws/claude5_opus")
            # Forced instances never touch the primary, and never write the machine-wide flag.
            self.assertEqual(primary.calls, 0)
            self.assertEqual(logged, [("aws/claude5_opus", 7, 2)])
            self.assertFalse(claude_router.FORCE_FLAG.exists())
            self.assertFalse(RouterServer(upstream, primary, port=0).fallback.active())
        finally:
            server.shutdown()
            server.server_close()

    def test_browser_origin_is_rejected(self):
        server = RouterServer(FakeUpstream([]), QuotaPrimary(), port=0).start()
        try:
            request = urllib.request.Request(server.base_url + "/v1/messages", data=b"{}",
                                             headers={"Authorization": "Bearer x", "Origin": "https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
