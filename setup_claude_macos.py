#!/usr/bin/env python3
"""Install, inspect, or remove the per-user macOS Claude Code CreateAI fallback."""

import argparse
import getpass
import hmac
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from anthropic_bridge import DEFAULT_MODEL
from model_map import AUTO, KNOWN_MODELS, resolve
from bridge import BridgeError, Upstream
from claude_asu import doctor
from claude_daemon import DEFAULT_PORT, KEYCHAIN_SERVICE, SHARED_SERVICE
from codex_asu import ENVIRONMENTS
from keychain import delete_password, load_password, password_exists, save_password
from setup_macos import interpreter

LABEL = "com.rich.asu-claude-bridge"
ROOT = Path(__file__).resolve().parent
SETTINGS = Path.home() / ".claude" / "settings.json"
STATE = Path.home() / ".claude" / "asu-claude-bridge-state.json"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "ASUClaudeBridge.log"


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def account():
    return getpass.getuser()


def load_token():
    for service in (KEYCHAIN_SERVICE, SHARED_SERVICE):
        if password_exists(service, account()):
            token = load_password(service, account()).strip()
            if token:
                return token
    raise BridgeError("No CreateAI token found in macOS Keychain.")


def store_token():
    print("Paste the ASU CreateAI Service token once; input is hidden.")
    token = getpass.getpass("ASU Service token: ").strip()
    if not token or "\n" in token or "\r" in token:
        raise BridgeError("A valid ASU Service token is required.")
    save_password(KEYCHAIN_SERVICE, account(), token)
    if not hmac.compare_digest(load_password(KEYCHAIN_SERVICE, account()).strip(), token):
        raise BridgeError("The token read back from Keychain did not match the entered token.")
    print("Keychain round-trip verification: PASS")


def launchctl(action):
    domain = f"gui/{os.getuid()}"
    if action == "stop":
        subprocess.run(["/bin/launchctl", "bootout", domain, str(PLIST)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif action == "start":
        result = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(PLIST)], check=False)
        if result.returncode:
            raise BridgeError("LaunchAgent could not be started.")
        subprocess.run(["/bin/launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def health(port, attempts=40):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return json.load(response)
        except (OSError, urllib.error.URLError, ValueError):
            time.sleep(0.25)
    return None


def read_settings():
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text())
    except json.JSONDecodeError:
        raise BridgeError(f"{SETTINGS} is not valid JSON; fix it before installing.") from None


def install(args):
    if sys.platform != "darwin":
        raise BridgeError("This installer supports macOS only.")
    if args.use_keychain or password_exists(KEYCHAIN_SERVICE, account()) or password_exists(SHARED_SERVICE, account()):
        print("Using the CreateAI token already in macOS Keychain.")
    else:
        store_token()
    token = load_token()
    upstream = Upstream(ENVIRONMENTS[args.environment], token)
    checks = [args.model]
    if args.model == AUTO:
        available = [item["id"] for item in upstream.models().get("data", [])] or list(KNOWN_MODELS)
        checks = [resolve(name, available, DEFAULT_MODEL) for name in ("claude-opus-5", "claude-haiku-4-5")]
        print(f"Model mapping is automatic; verifying {', '.join(checks)}.")
    for name in dict.fromkeys(checks):
        print(f"Testing CreateAI ({args.environment}/{name}) before changing Claude Code settings...")
        doctor(upstream, name)

    settings = read_settings()
    environment = dict(settings.get("env") or {})
    previous = environment.get("ANTHROPIC_BASE_URL")
    base_url = f"http://127.0.0.1:{args.port}"
    if previous and previous != base_url:
        raise BridgeError(f"settings.json already sets ANTHROPIC_BASE_URL to {previous}. "
                          "Remove it first so nothing of yours is overwritten.")
    plist = {
        "Label": LABEL,
        "ProgramArguments": [interpreter(), str(ROOT / "claude_daemon.py"),
                             "--environment", args.environment, "--model", args.model,
                             "--port", str(args.port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "ProcessType": "Background",
    }
    atomic_write(PLIST, plistlib.dumps(plist))
    launchctl("stop")
    launchctl("start")
    state = health(args.port)
    if not state:
        launchctl("stop")
        PLIST.unlink(missing_ok=True)
        raise BridgeError(f"The LaunchAgent started but nothing answered on port {args.port}. "
                          f"See {LOG}. Claude Code settings were not changed.")
    backup = SETTINGS.with_name(f"settings.json.asu-backup-{int(time.time())}")
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
    atomic_write(STATE, json.dumps({"previous_base_url": previous, "port": args.port,
                                    "backup": str(backup) if SETTINGS.exists() else None}, indent=2).encode())
    environment["ANTHROPIC_BASE_URL"] = base_url
    settings["env"] = environment
    # Route Claude Code only after the background service answers.
    atomic_write(SETTINGS, (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode(), mode=0o644)
    print(f"Installed. {base_url} -> api.anthropic.com, falling back to {args.model} on a usage limit.")
    print("Start a new Claude Code session so it picks up settings.json.")


def uninstall(args):
    launchctl("stop")
    PLIST.unlink(missing_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if SETTINGS.exists():
        settings = read_settings()
        environment = dict(settings.get("env") or {})
        if state.get("previous_base_url"):
            environment["ANTHROPIC_BASE_URL"] = state["previous_base_url"]
        else:
            environment.pop("ANTHROPIC_BASE_URL", None)
        if environment:
            settings["env"] = environment
        else:
            settings.pop("env", None)
        atomic_write(SETTINGS, (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode(), mode=0o644)
    STATE.unlink(missing_ok=True)
    if not args.keep_token and password_exists(KEYCHAIN_SERVICE, account()):
        delete_password(KEYCHAIN_SERVICE, account())
    print("Removed. Start a new Claude Code session; it goes straight to api.anthropic.com again.")


def status(_args):
    port = DEFAULT_PORT
    if STATE.exists():
        try:
            port = int(json.loads(STATE.read_text()).get("port", DEFAULT_PORT))
        except (ValueError, json.JSONDecodeError):
            pass
    settings = read_settings() if SETTINGS.exists() else {}
    configured = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL") == f"http://127.0.0.1:{port}"
    token = any(password_exists(service, account()) for service in (KEYCHAIN_SERVICE, SHARED_SERVICE))
    loaded = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    state = health(port, attempts=2)
    print(f"settings.json: {'routed to the bridge' if configured else 'not routed'}")
    print(f"Keychain token: {'present' if token else 'missing'}")
    print(f"LaunchAgent: {'loaded' if loaded else 'not loaded'}")
    print(f"Bridge health: {'ok' if state else 'unavailable'}")
    if state:
        remaining = state.get("fallback_seconds_remaining", 0)
        print(f"Current provider: {'CreateAI fallback' if state.get('fallback_active') else 'Claude (api.anthropic.com)'}"
              + (f", {remaining // 60} min left" if remaining else ""))
        if state.get("reason"):
            print(f"Last usage-limit error: {state['reason']}")
    return bool(configured and token and loaded and state)


def port_number(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    install_parser.add_argument("--model", default=AUTO,
                                help="auto maps each Claude model to its CreateAI counterpart, "
                                     "or pass one exact CreateAI id to pin every fallback request")
    install_parser.add_argument("--use-keychain", action="store_true")
    install_parser.add_argument("--port", type=port_number, default=DEFAULT_PORT)
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
