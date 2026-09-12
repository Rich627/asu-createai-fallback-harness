"""The CreateAI client and the wire primitives both bridges share.

Python standard library only. Credentials and request bodies are never logged.
Owned by no client: `anthropic_bridge` and `codex_bridge` both build on this, so nothing
client-specific belongs here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class BridgeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Upstream:
    def __init__(self, base_url, token, timeout=180):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Do not route credentials via environment-configured HTTP proxies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, path, body=None, attempts=3, backoff=0.75):
        """CreateAI returns intermittent 5xx; retry before the client's turn fails.

        Safe to repeat: nothing has been streamed to the client yet, and tool calls are
        only handed over once a stream completes.
        """
        for attempt in range(1, attempts + 1):
            try:
                return self.request(path, body)
            except BridgeError as exc:
                if exc.status not in (500, 502, 503, 504) or attempt == attempts:
                    raise
                time.sleep(backoff * attempt)

    def request(self, path, body=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        req = urllib.request.Request(self.base_url + path, headers=headers,
                                     data=None if body is None else dumps(body).encode())
        try:
            return self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            advice = {401: "Check the ASU service token and environment.",
                      403: "Check project/API access and model permission.",
                      404: "Check the ASU environment and model ID.",
                      429: "ASU quota or rate limit reached; wait or contact ASU.",
                      500: "ASU returned a server error and retries did not help; try again."}.get(
                          status, "Check ASU model compatibility and service status.")
            raise BridgeError(f"ASU HTTP {status} at {path}. {advice}", status) from None
        except (OSError, urllib.error.URLError):
            raise BridgeError("ASU connection failed or timed out.", 502) from None

    def models(self):
        with self.open("/models") as response:
            return json.load(response)


def sse_data(response):
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield "\n".join(data)
