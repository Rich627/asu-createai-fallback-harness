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

from asu.codex_bridge import BridgeServer, response_events
from asu.createai import BridgeError, Upstream, sse_data
from asu.codex_router import FallbackServer, Primary

ENVIRONMENTS = {
    "production": "https://api-main.aiml.asu.edu/v1",
    "beta": "https://api-main-beta.aiml.asu.edu/v1",
    "poc": "https://api-main-poc.aiml.asu.edu/v1",
}
PROVIDERS = {
    "createai": ENVIRONMENTS["production"],
    "rc": "https://openai.rc.asu.edu/v1",
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


def get_token(provider="createai"):
    variable = "ASU_RC_TOKEN" if provider == "rc" else "ASU_CREATEAI_TOKEN"
    label = "RC API" if provider == "rc" else "ASU CreateAI Service"
    token = os.environ.get(variable)
    if not token:
        if not sys.stdin.isatty():
            raise BridgeError(f"Set {variable} or run in a terminal to enter it privately.")
        token = getpass.getpass(f"{label} token (hidden, kept in memory only): ").strip()
    if not token or "\n" in token or "\r" in token:
        raise BridgeError("A valid ASU Service token is required.")
    return token


def child_environment(local_token):
    env = dict(os.environ)
    env.pop("ASU_CREATEAI_TOKEN", None)
    env["ASU_BRIDGE_SESSION_TOKEN"] = local_token
    return env


def auto_overrides(base_url, model):
    # env_key would replace the ChatGPT bearer with the local token, so in auto mode the
    # primary provider stays the only source of Authorization.
    dropped = ("model_providers.asu_bridge.env_key", "model_providers.asu_bridge.requires_openai_auth")
    pairs = codex_overrides(base_url, model)
    args = [part for index in range(0, len(pairs), 2)
            for part in pairs[index:index + 2] if not pairs[index + 1].startswith(dropped)]
    return args + ["-c", "model_providers.asu_bridge.requires_openai_auth=true",
                   "-c", 'model_providers.asu_bridge.env_http_headers={"ASU-Bridge-Key"="ASU_BRIDGE_SESSION_TOKEN"}']


def doctor(upstream, model):
    print("[1/3] GET /models", flush=True)
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
    # Ask for the call in the prompt instead of forcing it, so the check also covers the
    # path every real client request takes.
    request = {"model": model, "input": "Call connection_check with marker ASU_OK. Do not answer in text.",
               "tools": [tool], "tool_choice": "auto"}
    print("[2/3] Streaming tool call via /chat/completions", flush=True)
    events = list(response_events(upstream, request))
    output = events[-1]["response"]["output"]
    calls = [item for item in output if item["type"] == "function_call"]
    if not calls:
        raise BridgeError("Streaming worked, but the model did not call the test function.")
    request["input"] = [{"role": "user", "content": "Call connection_check with marker ASU_OK."},
                        *output, *[{"type": "function_call_output", "call_id": item["call_id"],
                                  "output": "ASU_OK"} for item in calls],
                        {"role": "user", "content": "Reply with ASU_OK only. Do not call tools."}]
    print("[3/3] Tool-result follow-up via /chat/completions", flush=True)
    final = list(response_events(upstream, request))[-1]["response"]
    if not any("ASU_OK" in part.get("text", "") for item in final["output"] for part in item.get("content", [])):
        raise BridgeError("The model did not complete the tool-result round trip.")
    print("PASS: streaming, function call, tool result, and follow-up response.")


def diagnose(upstream, model):
    """Probe independently: a broken /models must not prevent a minimal chat test."""
    print(f"Diagnostic target: {upstream.base_url}; model={model}", flush=True)
    print("No tokens, response bodies, or model-generated text will be printed.", flush=True)
    results = []

    def probe(label, check):
        print(label, flush=True)
        try:
            check()
            print("  PASS", flush=True)
            results.append(True)
        except Exception as exc:
            message = str(exc) if isinstance(exc, BridgeError) else "Unexpected response format or connection failure."
            print("  FAIL: " + message, flush=True)
            results.append(False)

    def models():
        value = upstream.models()
        entries = value.get("data")
        if not isinstance(entries, list):
            raise BridgeError("The model list is not in the expected format.")
        print(f"  Available models: {len(entries)}", flush=True)
        if model != "defaults":
            print(f"  Selected model listed: {any(item.get('id') == model for item in entries)}", flush=True)

    basic = {"model": model, "messages": [{"role": "user", "content": "Reply with ASU_OK only."}]}

    def chat():
        with upstream.open("/chat/completions", basic) as response:
            value = json.load(response)
        if not value.get("choices", [{}])[0].get("message", {}).get("content"):
            raise BridgeError("No text in the Chat Completions response.")

    def stream():
        content = False
        completed = False
        with upstream.open("/chat/completions", {**basic, "stream": True}) as response:
            for data in sse_data(response):
                if data == "[DONE]":
                    completed = True
                    break
                value = json.loads(data)
                if value.get("error"):
                    raise BridgeError("ASU returned an error inside the stream.")
                content |= any(c.get("delta", {}).get("content") for c in value.get("choices", []))
        if not content or not completed:
            raise BridgeError("No text or no completion marker in the stream.")

    def responses():
        with upstream.open("/responses", {"model": model, "input": "Reply with ASU_OK only."}) as response:
            value = json.load(response)
        if value.get("error") or not any(part.get("text") for item in value.get("output", []) for part in item.get("content", [])):
            raise BridgeError("No text in the Responses result, or an API error was returned.")

    probe("[1/4] GET /models", models)
    probe("[2/4] Minimal POST /chat/completions (no tools or extra parameters)", chat)
    probe("[3/4] Minimal streaming POST /chat/completions", stream)
    probe("[4/4] Minimal POST /responses", responses)
    print("Diagnostic complete. Share the PASS/FAIL lines; do not share your token.", flush=True)
    return all(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Pass Codex arguments after --. No token is stored.")
    parser.add_argument("--provider", choices=PROVIDERS, default="createai",
                        help="upstream provider; rc uses ASU Research Computing's OpenAI-compatible API")
    parser.add_argument("--environment", choices=ENVIRONMENTS, default="production",
                        help="CreateAI environment (ignored for --provider rc)")
    parser.add_argument("--model", default=os.environ.get("ASU_MODEL", "auto"),
                        help="auto maps the requested model to its CreateAI counterpart; or an exact "
                             "ASU model ID, or defaults for the Builder project's model")
    parser.add_argument("--doctor", action="store_true", help="List models and run two small live model requests")
    parser.add_argument("--diagnose", action="store_true", help="Probe models, basic chat, streaming, and Responses independently (three small model requests)")
    parser.add_argument("--auto", action="store_true", help="Use the primary provider until a recognized quota error, then ASU")
    parser.add_argument("--primary", choices=("chatgpt", "api"), default="chatgpt")
    parser.add_argument("--primary-model", default="gpt-6-astra")
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    remaining = args.codex_args
    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    try:
        endpoint = PROVIDERS[args.provider] if args.provider == "rc" else ENVIRONMENTS[args.environment]
        upstream = Upstream(endpoint, get_token(args.provider))
        if args.model == "auto" and (args.doctor or args.diagnose):
            from asu.model_map import resolve
            args.model = resolve(args.primary_model, [item["id"] for item in upstream.models().get("data", [])],
                                 "defaults")
            print(f"auto model check resolved to {args.model}", file=sys.stderr)
        if args.diagnose:
            return 0 if diagnose(upstream, args.model) else 1
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
            print(f"Auto mode: {args.primary}/{args.primary_model} -> {args.provider}/{args.model} on exhausted quota.", file=sys.stderr)
        else:
            if args.model == "auto":
                raise BridgeError("ASU-only mode needs an explicit --model (auto maps from the primary model).")
            server = BridgeServer(upstream, local_token).start()
            overrides = codex_overrides(server.base_url, args.model)
            print(f"ASU mode: {args.provider}, model={args.model}. Quit Codex to stop the bridge.", file=sys.stderr)
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
