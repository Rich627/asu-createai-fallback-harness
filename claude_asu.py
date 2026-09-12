#!/usr/bin/env python3
"""Check CreateAI for Claude Code, or run Claude Code through a temporary local bridge."""

import argparse
import getpass
import os
import shutil
import subprocess
import sys

from asu.anthropic_bridge import message_events
from asu.createai import BridgeError, Upstream
from claude_daemon import DEFAULT_PORT, add_arguments, build_server, keychain_token
from codex_asu import ENVIRONMENTS, PROVIDERS

MARKER = "ASU_OK"
TOOL = {"name": "connection_check", "description": "Echo a test marker.",
        "input_schema": {"type": "object", "properties": {"marker": {"type": "string"}},
                         "required": ["marker"], "additionalProperties": False}}


def run_message(upstream, request, model):
    result = []
    for _ in message_events(upstream, request, model, result):
        pass
    return result[0]


def doctor(upstream, model):
    print("[1/3] GET /models", flush=True)
    ids = [item["id"] for item in upstream.models().get("data", [])]
    if model not in ids:
        raise BridgeError(f"{model} is not in this token's model list ({len(ids)} models available).")
    print(f"Authentication OK; {model} is available.")
    # Ask for the call in the prompt instead of forcing it, so the check also covers the
    # path every real client request takes.
    request = {"model": model, "max_tokens": 1024, "tools": [TOOL], "tool_choice": {"type": "auto"},
               "messages": [{"role": "user", "content": f"Call connection_check with marker {MARKER}. "
                                                        f"Do not answer in text."}]}
    print("[2/3] Streaming tool call", flush=True)
    message = run_message(upstream, request, model)
    calls = [block for block in message["content"] if block["type"] == "tool_use"]
    if not calls:
        raise BridgeError("Streaming worked, but the model did not call the test tool.")
    print("[3/3] Tool-result follow-up", flush=True)
    request["messages"] += [
        {"role": "assistant", "content": message["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": MARKER}
                                     for call in calls] +
                                    [{"type": "text", "text": f"Reply with {MARKER} only. Do not call tools."}]}]
    final = run_message(upstream, request, model)
    if not any(MARKER in block.get("text", "") for block in final["content"]):
        raise BridgeError("The model did not complete the tool-result round trip.")
    print("PASS: streaming, tool call, tool result, and follow-up response.")


def main():
    parser = add_arguments(argparse.ArgumentParser(description=__doc__,
                                                  epilog="Claude Code arguments go after --."))
    parser.add_argument("--provider", choices=PROVIDERS, default="createai",
                        help="upstream provider; rc uses ASU Research Computing's OpenAI-compatible API")
    parser.add_argument("--doctor", action="store_true", help="Run two small live provider requests")
    parser.add_argument("--force-fallback", action="store_true",
                        help="Send every request to CreateAI for this run only; the installed "
                             "service and other sessions are untouched")
    parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.port == DEFAULT_PORT:
        args.port = 0
    remaining = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
    try:
        if args.provider == "rc":
            token = os.environ.get("ASU_RC_TOKEN")
            if not token:
                if not sys.stdin.isatty():
                    raise BridgeError("Set ASU_RC_TOKEN or run in a terminal to enter it privately.")
                token = getpass.getpass("RC API token (hidden, kept in memory only): ").strip()
            if not token or "\n" in token or "\r" in token:
                raise BridgeError("A valid RC API token is required.")
        else:
            token = keychain_token() if not os.environ.get("ASU_CREATEAI_TOKEN") else os.environ["ASU_CREATEAI_TOKEN"]
        endpoint = PROVIDERS[args.provider] if args.provider == "rc" else ENVIRONMENTS[args.environment]
        if args.doctor:
            doctor(Upstream(endpoint, token), args.model)
            return 0
        binary = shutil.which("claude")
        if not binary:
            raise BridgeError("Claude Code is not installed or not on PATH.")
        server = build_server(args, token)
        server.fallback.forced = args.force_fallback
        server.start()
        state = f"{args.provider} only (forced)" if args.force_fallback else f"Anthropic, {args.provider} on a usage limit"
        print(f"Claude Code via {server.base_url}; {state}; model {args.model}. Quit Claude to stop.",
              file=sys.stderr)
        environment = dict(os.environ, ANTHROPIC_BASE_URL=server.base_url)
        environment.pop("ASU_CREATEAI_TOKEN", None)
        try:
            return subprocess.call([binary, *remaining], env=environment)
        finally:
            server.shutdown()
            server.server_close()
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    sys.exit(main())
