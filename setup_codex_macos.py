#!/usr/bin/env python3
"""Install, inspect, or remove the per-user macOS ASU Codex bridge."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

from asu import codex_config as config
from asu import credstore
from asu import installer
from asu.createai import BridgeError, Upstream
from codex_asu import ENVIRONMENTS, diagnose, doctor
from codex_daemon import DEFAULT_PORT, KEYCHAIN_SERVICE
from asu.model_map import AUTO, KNOWN_MODELS, resolve

LABEL = "com.rich.asu-codex-bridge"
ROOT = Path(__file__).resolve().parent
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "ASUCodexBridge.log"


def interpreter():
    """One stable interpreter for both agents: a Keychain item's ACL trusts binaries, so a
    different python would make macOS prompt for the token on every service start."""
    for candidate in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", sys.executable, "/usr/bin/python3"):
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def run(command, **kwargs):
    return subprocess.run(command, check=False, **kwargs)


def launchctl(action):
    domain = f"gui/{os.getuid()}"
    if action == "stop":
        run(["/bin/launchctl", "bootout", domain, str(PLIST)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    elif action == "start":
        result = run(["/bin/launchctl", "bootstrap", domain, str(PLIST)])
        if result.returncode:
            raise BridgeError("LaunchAgent could not be started.")
        run(["/bin/launchctl", "kickstart", "-k", f"{domain}/{LABEL}"])


def install(args):
    if sys.platform != "darwin":
        raise BridgeError("This installer supports macOS only.")
    if args.use_keychain:
        if not installer.token_available(KEYCHAIN_SERVICE):
            raise BridgeError("No saved CreateAI token was found. Run install without --use-keychain once.")
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
    original_without_provider = config.remove_top_level_key(original, "model_provider").lstrip()
    updated = 'model_provider = "asu_autofallback"\n'
    if original_without_provider:
        updated += original_without_provider.rstrip() + "\n\n"
    updated += config.config_block(args.port)
    backup = config.CONFIG.with_name(f"config.toml.asu-backup-{int(time.time())}")
    if config.CONFIG.exists():
        shutil.copy2(config.CONFIG, backup)
    state = {"prior_model_provider_line": prior_provider, "backup": str(backup) if config.CONFIG.exists() else None,
             "port": args.port}

    plist = {
        "Label": LABEL,
        "ProgramArguments": [interpreter(), str(ROOT / "codex_daemon.py"), "--environment", args.environment,
                             "--model", args.model, "--primary", args.primary, "--port", str(args.port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "ProcessType": "Background",
    }
    installer.atomic_write(config.STATE, json.dumps(state, indent=2).encode())
    installer.atomic_write(PLIST, plistlib.dumps(plist))
    try:
        launchctl("stop")
        launchctl("start")
        if not installer.bridge_healthy(args.port):
            raise BridgeError(f"LaunchAgent started but the bridge did not answer on port {args.port}.")
        # Route Codex only after the background service is confirmed healthy.
        installer.atomic_write(config.CONFIG, updated.encode())
    except Exception:
        launchctl("stop")
        if PLIST.exists():
            PLIST.unlink()
        if config.STATE.exists():
            config.STATE.unlink()
        raise
    print("Installed. Fully quit and reopen ChatGPT/Codex so it reloads config.toml.")


def uninstall(args):
    launchctl("stop")
    if config.CONFIG.exists():
        text = config.remove_block(config.CONFIG.read_text())
        text = config.remove_top_level_key(text, "model_provider")
        if config.STATE.exists():
            state = json.loads(config.STATE.read_text())
            prior = state.get("prior_model_provider_line")
            if prior:
                text = prior + "\n" + text.lstrip()
        installer.atomic_write(config.CONFIG, text.encode())
    if PLIST.exists():
        PLIST.unlink()
    if config.STATE.exists():
        config.STATE.unlink()
    if not args.keep_token:
        credstore.delete_password(KEYCHAIN_SERVICE, installer.account())
    print("Removed. Fully quit and reopen ChatGPT/Codex.")


def status(_args):
    configured = config.CONFIG.exists() and config.managed(config.CONFIG.read_text())
    try:
        token = installer.token_available(KEYCHAIN_SERVICE)
    except BridgeError:
        token = False
    service = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    healthy = False
    port = DEFAULT_PORT
    if config.STATE.exists():
        try:
            port = int(json.loads(config.STATE.read_text()).get("port", DEFAULT_PORT))
        except (ValueError, json.JSONDecodeError):
            pass
    healthy = installer.bridge_healthy(port, attempts=2)
    print(f"Config: {'installed' if configured else 'not installed'}")
    print(f"{credstore.BACKEND} token: {'present' if token else 'missing'}")
    print(f"LaunchAgent: {'loaded' if service else 'not loaded'}")
    print(f"Bridge health: {'ok' if healthy else 'unavailable'}")
    return configured and token and service and healthy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    install_parser.add_argument("--model", default=AUTO,
                                help="auto maps each Codex model to its CreateAI counterpart, "
                                     "or pass one exact CreateAI id")
    install_parser.add_argument("--use-keychain", action="store_true",
                                help="Use the CreateAI token already stored in the platform credential store")
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
        return 0 if args.function(args) is not False else 1
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
