#!/usr/bin/env python3
"""Install, inspect, or remove the per-user Windows Claude Code CreateAI fallback.

The Windows twin of setup_claude_macos.py: a scheduled task instead of a LaunchAgent, Credential
Manager instead of the Keychain. As on macOS, settings.json is only touched after the background
service has answered a health check.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from asu import credstore
from asu import installer
from asu import winservice
from asu.anthropic_bridge import DEFAULT_MODEL
from claude_asu import doctor
from claude_daemon import DEFAULT_PORT, KEYCHAIN_SERVICE, SHARED_SERVICE
from codex_asu import ENVIRONMENTS
from asu.createai import BridgeError, Upstream
from asu.model_map import AUTO, KNOWN_MODELS, resolve

TASK = "ASU Claude Bridge"
ROOT = Path(__file__).resolve().parent
SETTINGS = Path.home() / ".claude" / "settings.json"
STATE = Path.home() / ".claude" / "asu-claude-bridge-state.json"
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "asu-unlimited-tokens"
LOG = DATA / "claude-bridge.log"
TASK_XML = DATA / "claude-bridge-task.xml"


def task_definition(args):
    options = ["--environment", args.environment, "--model", args.model,
               "--port", str(args.port), "--log", str(LOG)]
    arguments = winservice.argument_line(ROOT / "claude_daemon.py", options)
    return winservice.task_xml(winservice.interpreter(), arguments,
                               "ASU Claude bridge: Claude Code with CreateAI usage-limit fallback")


def install(args):
    winservice.require_windows()
    if args.use_stored_token or installer.token_available(KEYCHAIN_SERVICE, SHARED_SERVICE):
        print(f"Using the CreateAI token already in {credstore.BACKEND}.")
    else:
        installer.store_token(KEYCHAIN_SERVICE)
    token = installer.load_token(KEYCHAIN_SERVICE) if installer.token_available(KEYCHAIN_SERVICE) \
        else installer.load_token(SHARED_SERVICE)

    upstream = Upstream(ENVIRONMENTS[args.environment], token)
    checks = [args.model]
    if args.model == AUTO:
        available = [item["id"] for item in upstream.models().get("data", [])] or list(KNOWN_MODELS)
        checks = [resolve(name, available, DEFAULT_MODEL)
                  for name in ("claude-opus-5", "claude-haiku-4-5")]
        print(f"Model mapping is automatic; verifying {', '.join(checks)}.")
    for name in dict.fromkeys(checks):
        print(f"Testing CreateAI ({args.environment}/{name}) before changing Claude Code settings...")
        doctor(upstream, name)

    settings = installer.read_json(SETTINGS)
    environment = dict(settings.get("env") or {})
    previous = environment.get("ANTHROPIC_BASE_URL")
    base_url = f"http://127.0.0.1:{args.port}"
    if previous and previous != base_url:
        raise BridgeError(f"settings.json already sets ANTHROPIC_BASE_URL to {previous}. "
                          "Remove it first so nothing of yours is overwritten.")

    winservice.write_task_xml(TASK_XML, task_definition(args))
    winservice.stop_task(TASK)
    winservice.create_task(TASK, TASK_XML)
    winservice.start_task(TASK)
    if not installer.bridge_healthy(args.port):
        winservice.stop_task(TASK)
        winservice.delete_task(TASK)
        TASK_XML.unlink(missing_ok=True)
        raise BridgeError(f"The scheduled task started but nothing answered on port {args.port}. "
                          f"See {LOG}. Claude Code settings were not changed.")

    backup = SETTINGS.with_name(f"settings.json.asu-backup-{int(time.time())}")
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
    installer.atomic_write(STATE, json.dumps({"previous_base_url": previous, "port": args.port,
                                              "backup": str(backup) if SETTINGS.exists() else None},
                                             indent=2).encode())
    environment["ANTHROPIC_BASE_URL"] = base_url
    settings["env"] = environment
    # Route Claude Code only after the background service answers.
    installer.atomic_write(SETTINGS, (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode(),
                           mode=0o644)
    print(f"Installed. {base_url} -> api.anthropic.com, falling back to {args.model} on a usage limit.")
    print("Start a new Claude Code session so it picks up settings.json.")


def uninstall(args):
    winservice.require_windows()
    winservice.stop_task(TASK)
    winservice.delete_task(TASK)
    TASK_XML.unlink(missing_ok=True)
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except json.JSONDecodeError:
            state = {}
    if SETTINGS.exists():
        settings = installer.read_json(SETTINGS)
        environment = dict(settings.get("env") or {})
        if state.get("previous_base_url"):
            environment["ANTHROPIC_BASE_URL"] = state["previous_base_url"]
        else:
            environment.pop("ANTHROPIC_BASE_URL", None)
        if environment:
            settings["env"] = environment
        else:
            settings.pop("env", None)
        installer.atomic_write(SETTINGS,
                               (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode(),
                               mode=0o644)
    STATE.unlink(missing_ok=True)
    if not args.keep_token and installer.token_available(KEYCHAIN_SERVICE):
        credstore.delete_password(KEYCHAIN_SERVICE, installer.account())
    print("Removed. Start a new Claude Code session; it goes straight to api.anthropic.com again.")


def status(_args):
    winservice.require_windows()
    port = DEFAULT_PORT
    if STATE.exists():
        try:
            port = int(json.loads(STATE.read_text()).get("port", DEFAULT_PORT))
        except (ValueError, json.JSONDecodeError):
            pass
    settings = installer.read_json(SETTINGS)
    configured = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}"
    token = installer.token_available(KEYCHAIN_SERVICE, SHARED_SERVICE)
    registered = winservice.task_exists(TASK)
    state = installer.bridge_state(port, attempts=2)
    print(f"settings.json: {'routed to the bridge' if configured else 'not routed'}")
    print(f"{credstore.BACKEND} token: {'present' if token else 'missing'}")
    print(f"Scheduled task: {'registered' if registered else 'not registered'}")
    print(f"Bridge health: {'ok' if state else 'unavailable'}")
    if state:
        remaining = state.get("fallback_seconds_remaining", 0)
        print(f"Current provider: "
              f"{'CreateAI fallback' if state.get('fallback_active') else 'Claude (api.anthropic.com)'}"
              + (f", {remaining // 60} min left" if remaining else ""))
        if state.get("reason"):
            print(f"Last usage-limit error: {state['reason']}")
    return bool(configured and token and registered and state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    install_parser.add_argument("--model", default=AUTO,
                                help=f"auto maps each requested Claude model to its CreateAI "
                                     f"counterpart (unmapped models use {DEFAULT_MODEL}), or pass "
                                     f"one exact CreateAI id")
    install_parser.add_argument("--use-stored-token", "--use-keychain", action="store_true",
                                help="Use the CreateAI token already in Windows Credential Manager")
    install_parser.add_argument("--port", type=installer.port_number, default=DEFAULT_PORT)
    install_parser.set_defaults(function=install)
    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--keep-token", action="store_true")
    uninstall_parser.set_defaults(function=uninstall)
    status_parser = sub.add_parser("status")
    status_parser.set_defaults(function=status)
    args = parser.parse_args()
    try:
        result = args.function(args)
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0 if result in (None, True) else 1


if __name__ == "__main__":
    sys.exit(main())
