"""Same-request failover; primary authentication comes from Codex, never its auth files."""

import json
import sys
import threading
import urllib.error
import urllib.request

from codex_bridge import BridgeServer, response_events
from createai import BridgeError, NoRedirect, dumps, sse_data
from model_map import AUTO, Resolver

PRIMARY_URLS = {
    "chatgpt": "https://chatgpt.com/backend-api/codex/responses",
    "api": "https://api.openai.com/v1/responses",
}
QUOTA_CODES = {"insufficient_quota", "usage_limit_reached", "quota_exceeded", "billing_hard_limit_reached"}


def is_quota(error):
    if not isinstance(error, dict):
        return False
    return error.get("code") in QUOTA_CODES or error.get("type") in QUOTA_CODES


class PrimaryQuota(Exception):
    pass


ALLOWED_HEADERS = {"authorization", "chatgpt-account-id", "openai-organization", "openai-project",
                   "openai-beta", "originator", "user-agent", "session-id", "thread-id",
                   "conversation_id", "x-client-request-id", "x-codex-window-id",
                   "x-codex-turn-state", "x-codex-turn-metadata",
                   "x-openai-internal-codex-responses-lite"}


class Primary:
    def __init__(self, kind):
        self.url = PRIMARY_URLS[kind]
        # Everything but /responses lives next to it: .../codex/models, /v1/models.
        self.root = self.url[: -len("/responses")]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path, incoming):
        """Relay Codex's own GETs (model refresh) so its /model list is the real one."""
        headers = {key: value for key, value in incoming.items() if key.lower() in ALLOWED_HEADERS}
        target = self.root + (path[len("/v1"):] if path.startswith("/v1/") else path)
        request = urllib.request.Request(target, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read(65536)
            exc.close()
            return status, body
        except (OSError, urllib.error.URLError):
            raise BridgeError("Primary connection failed.", 502) from None

    def events(self, request, incoming):
        # Allow only model API headers; never forward ASU tokens or local bridge auth.
        headers = {key: value for key, value in incoming.items() if key.lower() in ALLOWED_HEADERS}
        headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
        body = {**request, "stream": True}
        req = urllib.request.Request(self.url, data=dumps(body).encode(), headers=headers)
        try:
            response = self.opener.open(req, timeout=180)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                payload = json.loads(exc.read(65536))
            except (ValueError, OSError):
                payload = {}
            finally:
                exc.close()
            if status in (402, 429) and is_quota(payload.get("error")):
                raise PrimaryQuota() from None
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            detail = f"{error.get('type') or error.get('code') or 'unknown'}: {str(error.get('message', ''))[:160]}"
            raise BridgeError(f"Primary provider HTTP {status} ({detail}); not a recognized "
                              f"exhausted-quota error. No failover.", status) from None
        except (OSError, urllib.error.URLError):
            raise BridgeError("Primary connection failed. No quota failover was triggered.", 502) from None
        with response:
            completed = False
            for raw in sse_data(response):
                if raw == "[DONE]":
                    break
                event = json.loads(raw)
                error = event.get("error") or event.get("response", {}).get("error")
                if event.get("type") == "error":
                    error = error or event
                if error:
                    if is_quota(error):
                        raise PrimaryQuota()
                    raise BridgeError("Primary provider returned a streaming error. No quota failover.", 502)
                yield event
                if event.get("type") == "response.completed":
                    completed = True
            if not completed:
                raise BridgeError("Primary stream ended before completion. No automatic replay.", 502)


class FallbackServer(BridgeServer):
    uses_primary_auth = True

    def proxy_get(self, path, headers):
        return self.primary.get(path, headers)

    def __init__(self, upstream, token, primary, asu_model=AUTO, port=0):
        self.primary = primary
        self.asu_model = asu_model
        self.resolver = Resolver(lambda: self.upstream, "defaults")
        self.fallback_active = threading.Event()
        super().__init__(upstream, token, port)

    def events(self, request, headers):
        if not self.fallback_active.is_set():
            emitted = False
            buffered = []
            try:
                for event in self.primary.events(request, headers):
                    # Created/in-progress carry no tool work; hold them in case quota arrives next.
                    if event.get("type") in ("response.created", "response.in_progress") and not emitted:
                        buffered.append(event)
                        continue
                    emitted = True
                    yield from buffered
                    buffered.clear()
                    yield event
                return
            except PrimaryQuota:
                self.fallback_active.set()
                if emitted:
                    raise BridgeError("Primary quota exhausted after partial output. ASU is selected for the next request; current output was not replayed.", 429)
                print("Primary quota exhausted. Continuing this request with ASU CreateAI.", file=sys.stderr)
        # Full input history and tool results are passed on; no new task/session is created.
        model = self.resolver.target(request.get("model"), self.asu_model)
        asu_request = {**request, "model": model}
        asu_request.pop("reasoning", None)
        stats = {}
        yield from response_events(self.upstream, asu_request, stats)
        cost = stats.get("cost")
        print(f"CreateAI {model}: {stats.get('input_tokens', 0)} in / {stats.get('output_tokens', 0)} out"
              + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""), file=sys.stderr, flush=True)
