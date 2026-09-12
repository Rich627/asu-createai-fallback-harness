"""Installer pieces that are the same on every platform.

The four installers (Claude and Codex, macOS and Windows) differ only in how they register a
background service and which client file they point at. Everything else — writing files without
leaving a half-written one behind, waiting for the bridge to answer, taking the token and
proving it survived a round trip through the credential store — lives here, so a fix lands once
instead of four times.
"""

from __future__ import annotations

import getpass
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from asu import credstore
from asu.createai import BridgeError


def account():
    return getpass.getuser()


def run(command, **kwargs):
    return subprocess.run(command, check=False, **kwargs)


def atomic_write(path, data, mode=0o600):
    """Write via a temporary file so a crash cannot leave a client reading half a config.

    `mode` is honored on POSIX; on Windows os.chmod only really carries the read-only bit, so
    the permission narrowing is macOS-only and callers must not rely on it for secrecy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def bridge_state(port, attempts=40, delay=0.25):
    """The parsed /health body once the service answers, else None.

    The Claude router reports which provider is live here, so status output needs the body and
    not only whether the port is open.
    """
    for _ in range(attempts):
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                            headers={"Authorization": "Bearer status-check"})
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    try:
                        return json.load(response)
                    except ValueError:
                        return {}
        except (OSError, urllib.error.URLError):
            time.sleep(delay)
    return None


def bridge_healthy(port, attempts=40, delay=0.25):
    return bridge_state(port, attempts, delay) is not None


def port_number(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise BridgeError(f"{value!r} is not a port number.") from None
    if not 1024 <= port <= 65535:
        raise BridgeError("Pick a port between 1024 and 65535.")
    return port


def load_token(service):
    return credstore.load_password(service, account()).strip()


def store_token(service):
    """Prompt for the token, store it, and prove it reads back byte for byte.

    A store that silently truncates or re-encodes would otherwise surface much later as an
    authentication failure in the middle of a session.
    """
    print(f"Paste the ASU CreateAI Service token once; input is hidden. "
          f"It is stored in {credstore.BACKEND}.")
    token = getpass.getpass("ASU Service token: ").strip()
    if not token or "\n" in token or "\r" in token:
        raise BridgeError("A valid ASU Service token is required.")
    credstore.save_password(service, account(), token)
    if not hmac.compare_digest(load_token(service), token):
        raise BridgeError(f"The token read back from {credstore.BACKEND} did not match what was "
                          "entered.")
    print(f"{credstore.BACKEND} round-trip verification: PASS")


def token_available(*services):
    """True when any of these service names already holds a token."""
    for service in services:
        try:
            if credstore.password_exists(service, account()):
                return True
        except BridgeError:
            return False
    return False


def read_json(path):
    """A client settings file as a dict; {} when absent. Invalid JSON is refused rather than
    silently replaced, because overwriting it would destroy the user's own settings."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        raise BridgeError(f"{path} is not valid JSON; fix it before installing.") from None
