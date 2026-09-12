#!/usr/bin/env python3
"""Install, inspect, or remove the per-user Windows ASU Codex bridge.

The Windows twin of setup_codex_macos.py. The config.toml editing is shared through
codex_config, so the two platforms cannot drift; what differs is a scheduled task instead of a
LaunchAgent and Credential Manager instead of the Keychain.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from asu import codex_config as config
from asu import credstore
from asu import installer
from asu import winservice
from codex_asu import ENVIRONMENTS, diagnose, doctor
from codex_daemon import DEFAULT_PORT, KEYCHAIN_SERVICE
from asu.createai import BridgeError, Upstream
from asu.model_map import AUTO, KNOWN_MODELS, resolve

TASK = "ASU Codex Bridge"
ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "asu-unlimited-tokens"
LOG = DATA / "codex-bridge.log"
TASK_XML = DATA / "codex-bridge-task.xml"


def task_definition(args):
    options = ["--environment", args.environment, "--model", args.model,
               "--primary", args.primary, "--port", str(args.port), "--log", str(LOG)]
    arguments = winservice.argument_line(ROOT / "codex_daemon.py", options)
    return winservice.task_xml(winservice.interpreter(), arguments,
                               "ASU Codex bridge: Codex with CreateAI usage-limit fallback")


def install(args):
    winservice.require_windows()
    if args.use_stored_token:
        if not installer.token_available(KEYCHAIN_SERVICE):
            raise BridgeError(f"No saved CreateAI token was found in {credstore.BACKEND}. "
                              "Run install without --use-stored-token once.")
        print(f"Using the existing CreateAI token from {credstore.BACKEND}.")
    else:
        installer.store_token(KEYCHAIN_SERVICE)
    token = installer.load_token(KEYCHAIN_SERVICE)

    upstream = Upstream(ENVIRONMENTS[args.environment], token)
    checked = args.model
    if checked == AUTO:
        available = [item["id"] for item in upstream.models().get("data", [])] or list(KNOWN_MODELS)
        checked = resolve(config.codex_model(), available, "defaults")
        print(f"Model mapping is automatic; verifying {checked} for the configured Codex model.")
    print("Testing CreateAI before changing Codex configuration...")
    if not diagnose(upstream, checked):
        raise BridgeError("CreateAI diagnostics failed. Codex configuration was not changed.")
    print("Testing CreateAI tool calls and tool-result continuation...")
    doctor(upstream, checked)

    original = config.CONFIG.read_text() if config.CONFIG.exists() else ""
    if config.managed(original):
        raise BridgeError("ASU Codex bridge is already installed. Run status or uninstall first.")
    prior_provider = config.top_level_value(original, "model_provider")
    without_provider = config.remove_top_level_key(original, "model_provider").lstrip()
    updated = 'model_provider = "asu_autofallback"\n'
    if without_provider:
        updated += without_provider.rstrip() + "\n\n"
    updated += config.config_block(args.port)

    backup = config.CONFIG.with_name(f"config.toml.asu-backup-{int(time.time())}")
    if config.CONFIG.exists():
        shutil.copy2(config.CONFIG, backup)
    state = {"prior_model_provider_line": prior_provider,
             "backup": str(backup) if config.CONFIG.exists() else None, "port": args.port}

    installer.atomic_write(config.STATE, json.dumps(state, indent=2).encode())
    winservice.write_task_xml(TASK_XML, task_definition(args))
    try:
        winservice.stop_task(TASK)
        winservice.create_task(TASK, TASK_XML)
        winservice.start_task(TASK)
        if not installer.bridge_healthy(args.port):
            raise BridgeError(f"The scheduled task started but the bridge did not answer on port "
                              f"{args.port}. See {LOG}. Codex configuration was not changed.")
        # Route Codex only after the background service is confirmed healthy.
        installer.atomic_write(config.CONFIG, updated.encode())
    except Exception:
        winservice.stop_task(TASK)
        winservice.delete_task(TASK)
        TASK_XML.unlink(missing_ok=True)
        config.STATE.unlink(missing_ok=True)
        raise
    print("Installed. Fully quit and reopen Codex so it reloads config.toml.")


def uninstall(args):
    winservice.require_windows()
    winservice.stop_task(TASK)
    winservice.delete_task(TASK)
    if config.CONFIG.exists():
        text = config.remove_block(config.CONFIG.read_text())
        text = config.remove_top_level_key(text, "model_provider")
        if config.STATE.exists():
            try:
                prior = json.loads(config.STATE.read_text()).get("prior_model_provider_line")
            except json.JSONDecodeError:
                prior = None
            if prior:
                text = prior + "\n" + text.lstrip()
        installer.atomic_write(config.CONFIG, text.encode())
    TASK_XML.unlink(missing_ok=True)
    config.STATE.unlink(missing_ok=True)
    if not args.keep_token:
        credstore.delete_password(KEYCHAIN_SERVICE, installer.account())
    print("Removed. Fully quit and reopen Codex.")


def status(_args):
    winservice.require_windows()
    configured = config.CONFIG.exists() and config.managed(config.CONFIG.read_text())
    token = installer.token_available(KEYCHAIN_SERVICE)
    port = DEFAULT_PORT
    if config.STATE.exists():
        try:
            port = int(json.loads(config.STATE.read_text()).get("port", DEFAULT_PORT))
        except (ValueError, json.JSONDecodeError):
            pass
    registered = winservice.task_exists(TASK)
    healthy = installer.bridge_healthy(port, attempts=2)
    print(f"Config: {'installed' if configured else 'not installed'}")
    print(f"{credstore.BACKEND} token: {'present' if token else 'missing'}")
    print(f"Scheduled task: {'registered' if registered else 'not registered'}")
    print(f"Bridge health: {'ok' if healthy else 'unavailable'}")
    return configured and token and registered and healthy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    install_parser.add_argument("--model", default=AUTO,
                                help="auto maps each Codex model to its CreateAI counterpart, "
                                     "or pass one exact CreateAI id")
    install_parser.add_argument("--use-stored-token", "--use-keychain", action="store_true",
                                help="Use the CreateAI token already in Windows Credential Manager")
    install_parser.add_argument("--primary", choices=("chatgpt", "api"), default="chatgpt")
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
