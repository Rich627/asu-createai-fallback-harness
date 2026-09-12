"""Loopback Anthropic endpoint: Claude first, CreateAI when the usage limit is hit.

The client's own Anthropic credentials are relayed to api.anthropic.com untouched.
The CreateAI token stays in this process and is never sent to Anthropic.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from anthropic_bridge import DEFAULT_MODEL, message_events
from createai import BridgeError, NoRedirect, dumps
from model_map import AUTO, Resolver

ANTHROPIC_URL = "https://api.anthropic.com"
FORCE_FLAG = Path.home() / ".claude" / "asu-fallback-force"
DEFAULT_WINDOW = 1800
MAX_WINDOW = 6 * 3600
MAX_BODY = 64 * 1024 * 1024
# Request headers that belong to this hop, plus the browser markers we refuse.
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
               "accept-encoding", "origin", "referer", "proxy-authorization", "proxy-connection",
               "te", "trailer", "cookie"}
RESPONSE_SKIP = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding",
                 "date", "server"}
QUOTA_HINTS = ("usage limit", "limit reached", "quota", "credit balance", "out of credits",
               "exceeded your", "insufficient")
QUOTA_TYPES = {"rate_limit_error", "quota_exceeded", "insufficient_quota", "billing_error",
               "usage_limit_error", "credit_balance_too_low"}


class PrimaryQuota(Exception):
    def __init__(self, seconds, reason):
        super().__init__(reason)
        self.seconds = seconds
        self.reason = reason


def error_payload(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_quota(status, payload, headers):
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    kind = str(error.get("type") or payload.get("type") or "")
    message = str(error.get("message") or "").lower()
    if status in (402, 429):
        if str(headers.get("anthropic-ratelimit-unified-status", "")).lower() == "rejected":
            return True
        if kind in QUOTA_TYPES and any(hint in message for hint in QUOTA_HINTS):
            return True
        if status == 402:
            return True
        if os.environ.get("ASU_CLAUDE_FALLBACK_ON_ANY_429") == "1":
            return True
    if status in (400, 403) and "credit balance" in message:
        return True
    return False


def quota_window(payload, headers):
    for key in ("anthropic-ratelimit-unified-reset", "anthropic-ratelimit-requests-reset"):
        value = headers.get(key)
        if value and str(value).isdigit():
            return int(value) - int(time.time())
    retry = headers.get("retry-after")
    if retry and str(retry).isdigit():
        return int(retry)
    return DEFAULT_WINDOW


def quota_reason(status, payload):
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    return f"HTTP {status} {error.get('type', 'error')}: {str(error.get('message', ''))[:200]}"


class Fallback:
    def __init__(self, forced=False):
        self.until = 0.0
        self.reason = ""
        self.forced = forced
        self.lock = threading.Lock()

    def active(self):
        # FORCE_FLAG is global to the machine; self.forced belongs to this instance only.
        if self.forced or FORCE_FLAG.exists():
            return True
        return time.time() < self.until

    def engage(self, quota):
        seconds = max(60, min(MAX_WINDOW, quota.seconds or DEFAULT_WINDOW))
        with self.lock:
            self.until = max(self.until, time.time() + seconds)
            self.reason = quota.reason
        print(f"[{time.strftime('%F %T')}] Claude usage limit reached; CreateAI fallback active for "
              f"{seconds // 60} min. {quota.reason}", flush=True)

    def state(self):
        remaining = max(0, int(self.until - time.time()))
        return {"fallback_active": self.active(), "fallback_seconds_remaining": remaining,
                "forced": self.forced or FORCE_FLAG.exists(), "reason": self.reason}


def fallback_ready(server):
    return getattr(server, "upstream", None) is not None


def log_usage(model, stats):
    if not stats:
        return
    cost = stats.get("cost")
    # CreateAI reports cost only on non-streaming replies; tokens are always there.
    print(f"[{time.strftime('%F %T')}] CreateAI {model}: "
          f"{stats.get('input_tokens', 0)} in / {stats.get('output_tokens', 0)} out"
          + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""), flush=True)


class Primary:
    def __init__(self, base_url=ANTHROPIC_URL, timeout=900):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, method, path, body, headers):
        forwarded = {key: value for key, value in headers.items() if key.lower() not in HOP_HEADERS}
        request = urllib.request.Request(self.base_url + path, data=body, headers=forwarded, method=method)
        try:
            response = self.opener.open(request, timeout=self.timeout)
            return response.status, dict(response.headers), response
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read(1024 * 1024)
            response_headers = dict(exc.headers)
            exc.close()
            lowered = {key.lower(): value for key, value in response_headers.items()}
            payload = error_payload(raw)
            if is_quota(status, payload, lowered):
                raise PrimaryQuota(quota_window(payload, lowered), quota_reason(status, payload)) from None
            if status in (402, 429):
                # Relayed as-is, but recorded: this is how an unknown usage-limit shape is found.
                print(f"[{time.strftime('%F %T')}] relayed {quota_reason(status, payload)} "
                      f"without switching; report it if Claude said the usage limit was reached.",
                      flush=True)
            return status, response_headers, io.BytesIO(raw)
        except (OSError, urllib.error.URLError):
            raise BridgeError("Could not reach api.anthropic.com. No usage-limit fallback was triggered.", 502) from None


def stream_quota(head):
    """An exhausted subscription can also arrive as the first event of a 200 stream."""
    for line in head.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        payload = error_payload(line[5:].strip())
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        if payload.get("type") == "error" and is_quota(429, payload, {}):
            raise PrimaryQuota(DEFAULT_WINDOW, quota_reason(429, payload))
        if error and error.get("type") in QUOTA_TYPES and any(
                hint in str(error.get("message", "")).lower() for hint in QUOTA_HINTS):
            raise PrimaryQuota(DEFAULT_WINDOW, quota_reason(200, payload))


def read_first_event(reader, limit=65536):
    buffer = b""
    while b"\n\n" not in buffer and len(buffer) < limit:
        chunk = reader.read1(limit) if hasattr(reader, "read1") else reader.read(limit)
        if not chunk:
            break
        buffer += chunk
    return buffer


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, upstream=None, primary=None, model=AUTO, port=0, address="127.0.0.1",
                 forced=False):
        self.upstream = upstream
        self.primary = primary
        self.model = model
        self.resolver = Resolver(lambda: self.upstream, DEFAULT_MODEL)
        self.fallback = Fallback(forced)
        super().__init__((address, port), Handler)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


class Handler(BaseHTTPRequestHandler):
    server_version = "ASUClaudeBridge"

    def log_message(self, *args):
        pass

    def authorized(self):
        # Anthropic credentials authenticate the caller; browser requests carry an Origin.
        if self.headers.get("Origin"):
            return False
        return bool(self.headers.get("Authorization") or self.headers.get("x-api-key"))

    def json_response(self, status, value, extra=None):
        body = dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def error_response(self, status, message):
        self.json_response(status, {"type": "error", "error": {
            "type": "api_error" if status >= 500 else "invalid_request_error", "message": message}})

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            raise BridgeError("Request too large.", 413)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        try:
            if self.path.split("?")[0] in ("/health", "/asu/health"):
                return self.json_response(200, {"status": "ok", "createai_ready": fallback_ready(self.server),
                                                **self.server.fallback.state()})
            if not self.authorized():
                return self.error_response(401, "Local bridge requires Anthropic authentication.")
            self.passthrough("GET", None)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            if not self.authorized():
                return self.error_response(401, "Local bridge requires Anthropic authentication.")
            body = self.read_body()
            path = self.path.split("?")[0]
            if path == "/v1/messages":
                return self.messages(body)
            if path == "/v1/messages/count_tokens" and self.server.fallback.active():
                return self.json_response(200, {"input_tokens": max(1, len(body) // 4)})
            self.passthrough("POST", body)
        except BridgeError as exc:
            self.error_response(exc.status, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def passthrough(self, method, body):
        try:
            status, headers, reader = self.server.primary.open(method, self.path, body, self.headers)
        except PrimaryQuota as quota:
            self.server.fallback.engage(quota)
            return self.error_response(429, "Claude usage limit reached; CreateAI fallback is now active.")
        with reader:
            self.relay(status, headers, b"", reader)

    def messages(self, body):
        if not self.server.fallback.active() or not fallback_ready(self.server):
            try:
                status, headers, reader = self.server.primary.open("POST", self.path, body, self.headers)
                with reader:
                    head = b""
                    if status == 200 and "event-stream" in headers.get("Content-Type", ""):
                        head = read_first_event(reader)
                        stream_quota(head)
                    return self.relay(status, headers, head, reader)
            except PrimaryQuota as quota:
                if not fallback_ready(self.server):
                    # Without a CreateAI token the client must see Anthropic's own limit error.
                    return self.error_response(429, "Claude usage limit reached and no CreateAI token "
                                                    "is loaded yet. " + quota.reason)
                self.server.fallback.engage(quota)
        self.fallback_messages(body)

    def relay(self, status, headers, head, reader):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in RESPONSE_SKIP:
                self.send_header(key, value)
        self.end_headers()
        if head:
            self.wfile.write(head)
            self.wfile.flush()
        while True:
            chunk = reader.read1(65536) if hasattr(reader, "read1") else reader.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def fallback_messages(self, body):
        streaming = False
        try:
            if not fallback_ready(self.server):
                raise BridgeError("No CreateAI token is loaded; the fallback provider is unavailable.", 503)
            request = json.loads(body)
            if not isinstance(request, dict):
                raise BridgeError("Invalid Messages request.")
            model = self.server.resolver.target(request.get("model"), self.server.model)
            result = []
            stats = {}
            events = message_events(self.server.upstream, request, model, result, stats)
            first = next(events)  # Open CreateAI before committing to HTTP 200.
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-ASU-Fallback", model)
                self.end_headers()
                streaming = True
                self.send_event(first)
                for event in events:
                    self.send_event(event)
            else:
                for _ in events:
                    pass
                self.json_response(200, result[0], {"X-ASU-Fallback": model})
            log_usage(model, stats)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            message = str(exc) if isinstance(exc, BridgeError) else "CreateAI fallback request failed."
            print(f"[{time.strftime('%F %T')}] fallback error: {message}", flush=True)
            if streaming:
                try:
                    self.send_event({"type": "error", "error": {"type": "api_error", "message": message}})
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.error_response(getattr(exc, "status", 502), message)

    def send_event(self, event):
        self.wfile.write(("event: " + event["type"] + "\ndata: " + dumps(event) + "\n\n").encode())
        self.wfile.flush()
