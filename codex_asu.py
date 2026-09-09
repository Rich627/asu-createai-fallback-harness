#!/usr/bin/env python3
"""Launch Codex with a private loopback CreateAI bridge, or check ASU access."""

import argparse
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys

from bridge import BridgeError, BridgeServer, Upstream, response_events
from router import FallbackServer, Primary

ENVIRONMENTS = {
    "production": "https://api-main.aiml.asu.edu/v1",
    "beta": "https://api-main-beta.aiml.asu.edu/v1",
    "poc": "https://api-main-poc.aiml.asu.edu/v1",
}


def codex_overrides(base_url, model):
    # Per-invocation flags preserve the user's default provider and login.
    values = {
        "model_provider": '"asu_bridge"',
        "model": json.dumps(model),
        "model_providers.asu_bridge.name": '"ASU CreateAI bridge"',
        "model_providers.asu_bridge.base_url": '"' + base_url + '"',
        "model_providers.asu_bridge.env_key": '"ASU_BRIDGE_SESSION_TOKEN"',
        "model_providers.asu_bridge.wire_api": '"responses"',
        "model_providers.asu_bridge.requires_openai_auth": "false",
        "model_providers.asu_bridge.supports_websockets": "false",
        "model_providers.asu_bridge.request_max_retries": "0",
        "model_providers.asu_bridge.stream_max_retries": "0",
        "features.enable_request_compression": "false",
        "features.remote_compaction_v2": "false",
        "features.apps": "false",
        "features.plugins": "false",
        "features.code_mode": "false",
        "features.code_mode_host": "false",
        "features.image_generation": "false",
        "features.browser_use": "false",
        "features.computer_use": "false",
        "web_search": '"disabled"',
        "model_supports_reasoning_summaries": "false",
    }
    return [part for key, value in values.items() for part in ("-c", key + "=" + value)]


def get_token():
    token = os.environ.get("ASU_CREATEAI_TOKEN")
    if not token:
        if not sys.stdin.isatty():
            raise BridgeError("Set ASU_CREATEAI_TOKEN or run in a terminal to enter it privately.")
        token = getpass.getpass("ASU Service token (hidden, kept in memory only): ").strip()
    if not token or "\n" in token or "\r" in token:
        raise BridgeError("A valid ASU Service token is required.")
    return token


def child_environment(local_token):
    env = dict(os.environ)
    env.pop("ASU_CREATEAI_TOKEN", None)
    env["ASU_BRIDGE_SESSION_TOKEN"] = local_token
    return env


def auto_overrides(base_url, model):
    args = codex_overrides(base_url, model)
    args += ["-c", "model_providers.asu_bridge.requires_openai_auth=true",
             "-c", 'model_providers.asu_bridge.env_http_headers={"ASU-Bridge-Key"="ASU_BRIDGE_SESSION_TOKEN"}']
    return args


def doctor(upstream, model):
    models = upstream.models()
    ids = [item["id"] for item in models.get("data", [])]
    print("API authentication OK. Available model IDs:")
    for name in ids:
        print("  " + name)
    if model != "defaults" and model not in ids:
        raise BridgeError("Selected model is not in this token's model list.")
    tool = {"type": "function", "name": "connection_check", "description": "Echo a test marker.",
            "parameters": {"type": "object", "properties": {"marker": {"type": "string"}},
                           "required": ["marker"], "additionalProperties": False}}
    request = {"model": model, "input": "Call connection_check with marker ASU_OK.",
               "tools": [tool], "tool_choice": {"type": "function", "name": "connection_check"}}
    events = list(response_events(upstream, request))
    output = events[-1]["response"]["output"]
    calls = [item for item in output if item["type"] == "function_call"]
    if not calls:
        raise BridgeError("Streaming worked, but the model did not call the test function.")
    request["input"] = [{"role": "user", "content": "Call connection_check with marker ASU_OK."},
                        *output, *[{"type": "function_call_output", "call_id": item["call_id"],
                                  "output": "ASU_OK"} for item in calls],
                        {"role": "user", "content": "Reply with ASU_OK only. Do not call tools."}]
    request["tool_choice"] = "none"
    final = list(response_events(upstream, request))[-1]["response"]
    if not any("ASU_OK" in part.get("text", "") for item in final["output"] for part in item.get("content", [])):
        raise BridgeError("The model did not complete the tool-result round trip.")
    print("PASS: streaming, function call, tool result, and follow-up response.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Pass Codex arguments after --. No token is stored.")
    parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    parser.add_argument("--model", default=os.environ.get("ASU_MODEL", "defaults"),
                        help="Exact ASU model ID, or defaults for the Builder project's model")
    parser.add_argument("--doctor", action="store_true", help="List models and run two small live model requests")
    parser.add_argument("--auto", action="store_true", help="Use the primary provider until a recognized quota error, then ASU")
    parser.add_argument("--primary", choices=("chatgpt", "api"), default="chatgpt")
    parser.add_argument("--primary-model", default="gpt-6-astra")
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    remaining = args.codex_args
    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    try:
        upstream = Upstream(ENVIRONMENTS[args.environment], get_token())
        if args.doctor:
            doctor(upstream, args.model)
            return 0
        binary = shutil.which("codex")
        if not binary:
            raise BridgeError("Codex CLI is not installed or not on PATH.")
        local_token = secrets.token_urlsafe(32)
        if args.auto:
            server = FallbackServer(upstream, local_token, Primary(args.primary), args.model).start()
            overrides = auto_overrides(server.base_url, args.primary_model)
            print(f"Auto mode: {args.primary}/{args.primary_model} -> ASU/{args.model} on exhausted quota.", file=sys.stderr)
        else:
            server = BridgeServer(upstream, local_token).start()
            overrides = codex_overrides(server.base_url, args.model)
            print(f"ASU mode: {args.environment}, model={args.model}. Quit Codex to stop the bridge.", file=sys.stderr)
        try:
            return subprocess.call([binary, *overrides, *remaining],
                                   env=child_environment(local_token))
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
