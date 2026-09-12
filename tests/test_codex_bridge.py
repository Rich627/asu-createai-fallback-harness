import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asu.codex_bridge import BridgeServer, ToolMap, response_events, translate
from asu.createai import BridgeError, Upstream, dumps
from codex_asu import child_environment, codex_overrides
from asu.codex_router import (DEFAULT_WINDOW, Fallback, FallbackServer, Primary,
                              PrimaryQuota, is_quota, quota_window)


def stream_bytes(chunks, done=True):
    text = "".join("data: " + dumps(c) + "\n\n" for c in chunks)
    return (text + ("data: [DONE]\n\n" if done else "")).encode()


def chunk(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class FakeUpstream:
    def __init__(self, chunks, done=True):
        self.chunks, self.done = chunks, done

    def open(self, path, body):
        self.body = body
        chunks = self.chunks(body) if callable(self.chunks) else self.chunks
        return io.BytesIO(stream_bytes(chunks, self.done))


class BridgeTests(unittest.TestCase):
    def test_primary_http_quota_and_header_isolation(self):
        class Opener:
            def open(self, request, timeout):
                self.headers = {k.lower(): v for k, v in request.header_items()}
                raise urllib.error.HTTPError(request.full_url, 429, "quota", {},
                                             io.BytesIO(b'{"error":{"code":"insufficient_quota"}}'))
        primary = Primary("api")
        primary.opener = Opener()
        with self.assertRaises(PrimaryQuota):
            list(primary.events({"model": "test", "input": "hello"}, {
                "Authorization": "Bearer fake-primary", "ASU-Bridge-Key": "fake-local",
                "Cookie": "never-forward", "ChatGPT-Account-Id": "fake-account"}))
        self.assertEqual(primary.opener.headers["authorization"], "Bearer fake-primary")
        self.assertNotIn("asu-bridge-key", primary.opener.headers)
        self.assertNotIn("cookie", primary.opener.headers)

    def test_primary_stream_quota(self):
        class Opener:
            def open(self, request, timeout):
                return io.BytesIO(stream_bytes([{"type": "error", "code": "usage_limit_reached"}]))
        primary = Primary("chatgpt")
        primary.opener = Opener()
        with self.assertRaises(PrimaryQuota):
            list(primary.events({"model": "test", "input": "hi"}, {}))

    def test_truncated_response_does_not_dispatch_valid_tool(self):
        tools = [{"type": "function", "name": "run", "parameters": {"type": "object"}}]
        name = ToolMap(tools).tools[0]["function"]["name"]
        upstream = FakeUpstream([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {
            "name": name, "arguments": "{}"}}]}, "length")])
        seen = []
        with self.assertRaises(BridgeError):
            for event in response_events(upstream, {"model": "defaults", "input": "hi", "tools": tools}):
                seen.append(event)
        self.assertFalse(any(e["type"] == "response.output_item.done" for e in seen))

    def test_quota_classification_does_not_treat_every_error_as_quota(self):
        self.assertTrue(is_quota({"code": "insufficient_quota"}))
        self.assertTrue(is_quota({"type": "usage_limit_reached"}))
        self.assertFalse(is_quota({"code": "rate_limit_exceeded"}))
        self.assertFalse(is_quota({"code": "invalid_api_key"}))

    def test_quota_latches_and_preserves_history(self):
        class Exhausted:
            count = 0

            def events(self, request, headers):
                self.count += 1
                raise PrimaryQuota()
                yield
        primary = Exhausted()
        upstream = FakeUpstream([chunk({"content": "continued"}, "stop")])
        server = FallbackServer(upstream, "test", primary, "asu/model")
        self.addCleanup(server.server_close)
        request = {"model": "primary/model", "input": [{"role": "user", "content": "continue"}]}
        list(server.events(request, {}))
        list(server.events(request, {}))
        self.assertEqual(primary.count, 1)
        self.assertEqual(upstream.body["model"], "asu/model")
        self.assertEqual(upstream.body["messages"], request["input"])
        self.assertEqual(request["model"], "primary/model")

    def test_no_replay_after_partial_primary_output(self):
        class Partial:
            def events(self, request, headers):
                yield {"type": "response.output_item.done", "item": {"type": "function_call"}}
                raise PrimaryQuota()
        upstream = FakeUpstream([])
        server = FallbackServer(upstream, "test", Partial(), "defaults")
        self.addCleanup(server.server_close)
        with self.assertRaises(BridgeError):
            list(server.events({"model": "primary", "input": "hi"}, {}))
        self.assertFalse(hasattr(upstream, "body"))
        self.assertTrue(server.fallback_active.is_set())

    def test_quota_window_parsing(self):
        self.assertEqual(quota_window({"error": {"resets_after": 300}}, {}), 300)
        self.assertEqual(quota_window({}, {"retry-after": "120"}), 120)
        self.assertEqual(quota_window({}, {"x-ratelimit-reset-requests": "450"}), 450)
        self.assertEqual(quota_window({}, {}), DEFAULT_WINDOW)

    def test_fallback_resets_after_window_expires_and_retries_primary(self):
        class RecoverablePrimary:
            def __init__(self):
                self.count = 0
                self.recovered = False

            def events(self, request, headers):
                self.count += 1
                if not self.recovered:
                    raise PrimaryQuota(seconds=1800)
                yield {"type": "response.completed", "response": {"status": "completed", "output": []}}

        primary = RecoverablePrimary()
        upstream = FakeUpstream([chunk({"content": "from-asu"}, "stop")])
        server = FallbackServer(upstream, "test", primary, "asu/model")
        self.addCleanup(server.server_close)

        # First request: primary quota fails, routes to ASU
        request = {"model": "primary/model", "input": [{"role": "user", "content": "hello"}]}
        list(server.events(request, {}))
        self.assertEqual(primary.count, 1)
        self.assertTrue(server.fallback_active.is_set())
        self.assertEqual(upstream.body["model"], "asu/model")

        # Second request before expiration: stays on ASU, primary not called
        list(server.events(request, {}))
        self.assertEqual(primary.count, 1)

        # Quota window expires (simulate time passing) and primary recovers
        server.fallback.until = 0.0
        self.assertFalse(server.fallback_active.is_set())
        primary.recovered = True

        # Third request: tries primary, primary succeeds, upstream is not called again
        del upstream.body
        events = list(server.events(request, {}))
        self.assertEqual(primary.count, 2)
        self.assertFalse(hasattr(upstream, "body"))
        self.assertEqual(events[0]["type"], "response.completed")

    def test_health_endpoint_reports_fallback_state(self):
        server = FallbackServer(FakeUpstream([]), "secret", Primary("api"))
        server.accept_any_bearer = True
        server.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = server.base_url.replace("/v1", "/health")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer any"})
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            self.assertEqual(data["status"], "ok")
            self.assertFalse(data["fallback_active"])

        server.fallback.set(PrimaryQuota(seconds=600, reason="test limit"))
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            self.assertTrue(data["fallback_active"])
            self.assertGreater(data["fallback_seconds_remaining"], 0)
            self.assertEqual(data["reason"], "test limit")

    def test_text_stream_and_usage(self):
        upstream = FakeUpstream([chunk({"content": "繁體"}), chunk({"content": "中文"}, "stop"),
                                 {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}])
        events = list(response_events(upstream, {"model": "defaults", "input": "hi"}))
        self.assertEqual([e["delta"] for e in events if e["type"] == "response.output_text.delta"], ["繁體", "中文"])
        self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 12)
        self.assertEqual(events[-1]["response"]["output"][0]["content"][0]["text"], "繁體中文")

    def test_namespaces_custom_tool_and_result_round_trip(self):
        tools = [{"type": "namespace", "name": "functions", "tools": [
            {"type": "custom", "name": "apply_patch", "description": "Apply a patch"}]}]
        def chunks(body):
            name = body["tools"][0]["function"]["name"]
            return [chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": name, "arguments": '{"input":'}}]}),
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"patch"}'}}]}, "tool_calls")]
        events = list(response_events(FakeUpstream(chunks), {"model": "defaults", "input": "edit", "tools": tools}))
        output = events[-1]["response"]["output"][0]
        self.assertEqual((output["type"], output["namespace"], output["input"]), ("custom_tool_call", "functions", "patch"))
        self.assertTrue(output["id"].startswith("ctc_"))
        body, _ = translate({"model": "defaults", "tools": tools, "input": [output,
                             {"type": "custom_tool_call_output", "call_id": "call_1", "output": "success"}]})
        self.assertEqual(body["messages"][1], {"role": "tool", "tool_call_id": "call_1", "content": "success"})

    def test_sanitize_for_primary_rewrites_custom_tool_call_id(self):
        from asu.codex_router import sanitize_for_primary
        request = {"model": "gpt-5", "input": [
            {"type": "message", "id": "msg_123", "role": "user", "content": "hi"},
            {"type": "function_call", "id": "fc_123", "call_id": "c1", "name": "f"},
            {"type": "custom_tool_call", "id": "fc_a4186fba75af3dd31e43a6d1", "call_id": "c2", "name": "custom"}
        ]}
        cleaned = sanitize_for_primary(request)
        self.assertEqual(cleaned["input"][0]["id"], "msg_123")
        self.assertEqual(cleaned["input"][1]["id"], "fc_123")
        self.assertEqual(cleaned["input"][2]["id"], "ctc_a4186fba75af3dd31e43a6d1")

    def test_multiple_calls_group_into_one_assistant_message(self):
        body, _ = translate({"model": "defaults", "input": [
            {"type": "function_call", "name": "a", "call_id": "c1", "arguments": "{}"},
            {"type": "function_call", "name": "b", "call_id": "c2", "arguments": "{}"}]})
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(len(body["messages"][0]["tool_calls"]), 2)

    def test_partial_stream_never_dispatches_tools(self):
        tools = [{"type": "function", "name": "run", "parameters": {"type": "object"}}]
        name = ToolMap(tools).tools[0]["function"]["name"]
        upstream = FakeUpstream([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": name, "arguments": "{}"}}]})], done=False)
        seen = []
        with self.assertRaises(BridgeError):
            for event in response_events(upstream, {"model": "defaults", "input": "hi", "tools": tools}):
                seen.append(event)
        self.assertFalse(any(e["type"] == "response.output_item.done" for e in seen))

    def test_unsupported_history_is_explicit(self):
        for item in [{"type": "compaction", "encrypted_content": "opaque"}, {"type": "item_reference", "id": "x"}]:
            with self.assertRaises(BridgeError):
                translate({"model": "defaults", "input": [item]})

    def test_local_auth_and_origin_rejection(self):
        server = BridgeServer(FakeUpstream([]), "test-only").start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for headers in ({}, {"Authorization": "Bearer test-only", "Origin": "https://example.com"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(server.base_url.replace("/v1", "/health"), headers=headers))
            self.assertEqual(error.exception.code, 401)

    def test_daemon_auth_accepts_bearer_but_rejects_browser_origin(self):
        server = BridgeServer(FakeUpstream([]), "not-used")
        server.accept_any_bearer = True
        server.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = server.base_url.replace("/v1", "/health")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer primary"})) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, headers={
                "Authorization": "Bearer primary", "Origin": "https://example.com"}))
        self.assertEqual(error.exception.code, 401)

    def test_asu_token_not_passed_to_codex(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"ASU_CREATEAI_TOKEN": "test-only"}):
            env = child_environment("local-only")
        self.assertNotIn("ASU_CREATEAI_TOKEN", env)
        self.assertEqual(env["ASU_BRIDGE_SESSION_TOKEN"], "local-only")

    def test_get_broken_pipe_is_silently_handled(self):
        from asu.codex_bridge import Handler
        server = BridgeServer(FakeUpstream([]), "test-only")
        server.proxy_get = lambda path, headers: (200, b'{"data": []}')
        handler = Handler.__new__(Handler)
        handler.server = server
        handler.headers = {"Authorization": "Bearer test-only"}
        handler.path = "/v1/models"

        class BrokenWriter:
            def write(self, data):
                raise BrokenPipeError(32, "Broken pipe")

        handler.wfile = BrokenWriter()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_GET()


class CodexIntegration(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RUN_CODEX_INTEGRATION") == "1", "opt-in local Codex test")
    def test_real_codex_tool_round_trip(self):
        """No real model/key: Codex executes pwd in a temporary directory."""
        observed = []
        class MockHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                observed.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if any(m["role"] == "tool" for m in body["messages"]):
                    chunks = [chunk({"content": "BRIDGE_INTEGRATION_OK"}, "stop")]
                else:
                    functions = [t["function"] for t in body.get("tools", [])]
                    tool = next((f for f in functions if "cmd" in f.get("parameters", {}).get("properties", {})), None)
                    if tool is None:
                        chunks = [chunk({"content": "MISSING_SHELL_TOOL"}, "stop")]
                    else:
                        chunks = [chunk({"tool_calls": [{"index": 0, "id": "call_test", "function": {
                            "name": tool["name"], "arguments": dumps({"cmd": "pwd", "max_output_tokens": 100})}}]}, "tool_calls")]
                self.wfile.write(stream_bytes(chunks))

        mock = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        threading.Thread(target=mock.serve_forever, daemon=True).start()
        self.addCleanup(mock.server_close)
        self.addCleanup(mock.shutdown)
        server = BridgeServer(Upstream(f"http://127.0.0.1:{mock.server_port}/v1", "fake-only"), "fake-local").start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="asu-codex-test-") as directory:
            command = ["codex", *codex_overrides(server.base_url, "bridge-test"), "exec",
                       "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--json", "-C", directory, "Run pwd once, then reply BRIDGE_INTEGRATION_OK."]
            result = subprocess.run(command, env=child_environment("fake-local"), text=True,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:] + result.stdout[-4000:])
        self.assertIn("BRIDGE_INTEGRATION_OK", result.stdout)
        self.assertTrue(any(m["role"] == "tool" for body in observed for m in body["messages"]), result.stdout)

    @unittest.skipUnless(os.environ.get("RUN_CODEX_INTEGRATION") == "1", "opt-in local Codex test")
    def test_real_codex_same_session_quota_fallback(self):
        class PrimaryMock:
            count = 0

            def get(self, path, headers):
                return 200, b'{"data": []}'

            def events(self, request, headers):
                self.count += 1
                if self.count > 1:
                    raise PrimaryQuota()
                def tool_chunks(body):
                    tool = next(t["function"] for t in body["tools"]
                                if "cmd" in t["function"].get("parameters", {}).get("properties", {}))
                    return [chunk({"tool_calls": [{"index": 0, "id": "primary_pwd", "function": {
                        "name": tool["name"], "arguments": dumps({"cmd": "pwd", "max_output_tokens": 100})}}]}, "tool_calls")]
                yield from response_events(FakeUpstream(tool_chunks), request)
        primary = PrimaryMock()
        fallback = FakeUpstream([chunk({"content": "FALLBACK_SAME_SESSION_OK"}, "stop")])
        server = FallbackServer(fallback, "fake-local", primary, "asu-test").start()
        # The test uses fake local auth, never the user's actual primary credentials.
        server.uses_primary_auth = False
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="asu-fallback-test-") as directory:
            command = ["codex", *codex_overrides(server.base_url, "bridge-test"), "exec",
                       "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                       "--json", "-C", directory, "Run pwd once and finish the task."]
            result = subprocess.run(command, env=child_environment("fake-local"), text=True,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:] + result.stdout[-3000:])
        self.assertEqual(primary.count, 2)
        self.assertIn("FALLBACK_SAME_SESSION_OK", result.stdout)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(sum(e["type"] == "thread.started" for e in events), 1)
        self.assertEqual(sum(m["role"] == "tool" for m in fallback.body["messages"]), 1)
        self.assertEqual(fallback.body["model"], "asu-test")


class AdditionalToolsTest(unittest.TestCase):
    def test_tools_declared_in_an_additional_tools_item(self):
        request = {"model": "m", "input": [
            {"type": "additional_tools", "role": "developer", "tools": [
                {"type": "namespace", "name": "functions", "tools": [
                    {"type": "custom", "name": "exec", "description": "Run JavaScript"}]}]},
            {"role": "user", "content": "hi"}]}
        body, toolmap = translate(request)
        self.assertEqual(len(body["tools"]), 1)
        self.assertEqual(toolmap.by_wire[body["tools"][0]["function"]["name"]], ("functions", "exec", "custom"))
        self.assertEqual([message["role"] for message in body["messages"]], ["user"])


if __name__ == "__main__":
    unittest.main()
