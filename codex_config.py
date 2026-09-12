"""Reading and editing Codex's config.toml, independent of platform.

Both Codex installers share this. The managed block is delimited by comment markers, and the
block is found by BEGIN_PREFIX while the longer BEGIN is what gets written: a block left by any
older version is still recognized and removed, so changing the trailing text can never orphan
one in a user's config.toml.
"""

from __future__ import annotations

from pathlib import Path

from createai import BridgeError

BEGIN_PREFIX = "# BEGIN ASU CODEX BRIDGE"
BEGIN = f"{BEGIN_PREFIX} (managed by asu-unlimited-tokens)"
END = "# END ASU CODEX BRIDGE"

CONFIG = Path.home() / ".codex" / "config.toml"
STATE = Path.home() / ".codex" / "asu-codex-bridge-state.json"

DEFAULT_CODEX_MODEL = "gpt-5.6-sol"


def remove_block(text):
    start = text.find(BEGIN_PREFIX)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        raise BridgeError("Codex config contains an incomplete ASU managed block.")
    end += len(END)
    while end < len(text) and text[end] == "\n":
        end += 1
    return text[:start].rstrip() + ("\n" if text[:start].strip() else "") + text[end:]


def top_level_value(text, key):
    for line in remove_block(text).splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if stripped.startswith(key + " ") or stripped.startswith(key + "="):
            return line
    return None


def remove_top_level_key(text, key):
    lines = text.splitlines(keepends=True)
    in_top = True
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_top = False
        if in_top and (stripped.startswith(key + " ") or stripped.startswith(key + "=")):
            continue
        result.append(line)
    return "".join(result)


def config_block(port):
    return f'''{BEGIN}
[model_providers.asu_autofallback]
name = "OpenAI with ASU CreateAI fallback"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
{END}
'''


def codex_model():
    """The model Codex is configured to use, so an auto install can verify its counterpart."""
    if CONFIG.exists():
        line = top_level_value(CONFIG.read_text(), "model")
        if line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return DEFAULT_CODEX_MODEL


def managed(text):
    """True when this config.toml already carries a managed block from any version."""
    return BEGIN_PREFIX in text or "[model_providers.asu_autofallback]" in text
