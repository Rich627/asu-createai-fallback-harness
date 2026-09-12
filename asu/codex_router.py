"""Same-request failover; primary authentication comes from Codex, never its auth files."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

from asu.codex_bridge import BridgeServer, response_events
from asu.createai import BridgeError, NoRedirect, dumps, sse_data
from asu.model_map import AUTO, Resolver

PRIMARY_URLS = {
    "chatgpt": "https://chatgpt.com/backend-api/codex/responses",
    "api": "https://api.openai.com/v1/responses",
}
QUOTA_CODES = {"insufficient_quota", "usage_limit_reached", "quota_exceeded", "billing_hard_limit_reached"}
DEFAULT_WINDOW = 1800
MAX_WINDOW = 6 * 3600


def is_quota(error):
    if not isinstance(error, dict):
        return False
    return error.get("code") in QUOTA_CODES or error.get("type") in QUOTA_CODES


class PrimaryQuota(Exception):
    def __init__(self, seconds=DEFAULT_WINDOW, reason=""):
        super().__init__(reason)
        self.seconds = seconds
        self.reason = reason


def quota_window(payload, headers):
    retry = headers.get("retry-after")
    if retry and str(retry).isdigit():
        return int(retry)
    for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        value = headers.get(key)
        if value and str(value).isdigit():
            return int(value)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    if isinstance(error, dict):
        for key in ("resets_after", "resets_in_seconds", "retry_after"):
            val = error.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
            if isinstance(val, str) and val.isdigit():
                return int(val)
    return DEFAULT_WINDOW


def quota_reason(status, payload):
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    if not isinstance(error, dict):
        error = {}
    return f"HTTP {status} {error.get('type') or error.get('code') or 'error'}: {str(error.get('message', ''))[:200]}"


class Fallback:
    def __init__(self):
        self.until = 0.0
        self.reason = ""
        self.lock = threading.Lock()

    def is_set(self):
        return time.time() < self.until

    def set(self, quota=None):
        seconds = DEFAULT_WINDOW
        reason = ""
        if isinstance(quota, PrimaryQuota):
            seconds = quota.seconds or DEFAULT_WINDOW
            reason = quota.reason
        elif isinstance(quota, (int, float)):
            seconds = int(quota)
        seconds = max(60, min(MAX_WINDOW, seconds))
        with self.lock:
            self.until = max(self.until, time.time() + seconds)
            if reason:
                self.reason = reason

    def clear(self):
        with self.lock:
            self.until = 0.0
            self.reason = ""

    def state(self):
        remaining = max(0, int(self.until - time.time()))
        return {"fallback_active": self.is_set(), "fallback_seconds_remaining": remaining,
                "reason": self.reason}


ALLOWED_HEADERS = {"authorization", "chatgpt-account-id", "openai-organization", "openai-project",
                   "openai-beta", "originator", "user-agent", "session-id", "thread-id",
                   "conversation_id", "x-client-request-id", "x-codex-window-id",
                   "x-codex-turn-state", "x-codex-turn-metadata",
                   "x-openai-internal-codex-responses-lite"}


def sanitize_for_primary(request):
    """Codex sessions may contain custom_tool_call items created with fc_ prefixes."""
    items = request.get("input")
    if not isinstance(items, list):
        return request
    cleaned = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "custom_tool_call":
            item_id = str(item.get("id") or "")
            if item_id.startswith("fc_"):
                item = {**item, "id": "ctc_" + item_id[3:]}
        cleaned.append(item)
    return {**request, "input": cleaned}


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
        body = {**sanitize_for_primary(request), "stream": True}
        req = urllib.request.Request(self.url, data=dumps(body).encode(), headers=headers)
        try:
            response = self.opener.open(req, timeout=180)
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = dict(exc.headers)
            try:
                payload = json.loads(exc.read(65536))
            except (ValueError, OSError):
                payload = {}
            finally:
                exc.close()
            lowered = {key.lower(): value for key, value in response_headers.items()}
            if status in (402, 429) and is_quota(payload.get("error")):
                raise PrimaryQuota(quota_window(payload, lowered), quota_reason(status, payload)) from None
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
                        err_payload = error if isinstance(error, dict) else {}
                        raise PrimaryQuota(quota_window(err_payload, {}), quota_reason(200, {"error": err_payload}))
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

    def health_state(self):
        return {"status": "ok", **self.fallback.state()}

    def __init__(self, upstream, token, primary, asu_model=AUTO, port=0):
        self.primary = primary
        self.asu_model = asu_model
        self.resolver = Resolver(lambda: self.upstream, "defaults")
        self.fallback = Fallback()
        super().__init__(upstream, token, port)

    @property
    def fallback_active(self):
        return self.fallback

    @fallback_active.setter
    def fallback_active(self, value):
        self.fallback = value

    def events(self, request, headers):
        if not self.fallback.is_set():
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
            except PrimaryQuota as quota:
                self.fallback.set(quota)
                if emitted:
                    raise BridgeError("Primary quota exhausted after partial output. ASU is selected for the next request; current output was not replayed.", 429)
                seconds = max(60, min(MAX_WINDOW, quota.seconds or DEFAULT_WINDOW))
                print(f"[{time.strftime('%F %T')}] Primary quota exhausted. Continuing this request with ASU CreateAI (fallback active for {seconds // 60} min).", file=sys.stderr, flush=True)
        # Full input history and tool results are passed on; no new task/session is created.
        model = self.resolver.target(request.get("model"), self.asu_model)
        asu_request = {**request, "model": model}
        asu_request.pop("reasoning", None)
        stats = {}
        yield from response_events(self.upstream, asu_request, stats)
        cost = stats.get("cost")
        print(f"CreateAI {model}: {stats.get('input_tokens', 0)} in / {stats.get('output_tokens', 0)} out"
              + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""), file=sys.stderr, flush=True)
