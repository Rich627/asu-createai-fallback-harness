#!/usr/bin/env python3
"""Install, inspect, or remove the per-user macOS ASU Codex bridge."""

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

from createai import BridgeError, Upstream
from codex_asu import ENVIRONMENTS, diagnose, doctor
from codex_daemon import DEFAULT_PORT, KEYCHAIN_SERVICE
from keychain import delete_password, load_password, password_exists, save_password
from model_map import AUTO, KNOWN_MODELS, resolve

LABEL = "com.rich.asu-codex-bridge"
BEGIN_PREFIX = "# BEGIN ASU CODEX BRIDGE"
BEGIN = f"{BEGIN_PREFIX} (managed by setup_codex_macos.py)"
END = "# END ASU CODEX BRIDGE"
ROOT = Path(__file__).resolve().parent
CONFIG = Path.home() / ".codex" / "config.toml"
STATE = Path.home() / ".codex" / "asu-codex-bridge-state.json"
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


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


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
    return "gpt-5.6-sol"


def store_token():
    print("Paste the ASU CreateAI Service token once; input is hidden.")
    token = getpass.getpass("ASU Service token: ").strip()
    if not token or "\n" in token or "\r" in token:
        raise BridgeError("A valid ASU Service token is required.")
    save_password(KEYCHAIN_SERVICE, getpass.getuser(), token)
    saved = load_token()
    if not hmac.compare_digest(saved, token):
        raise BridgeError("The token read back from Keychain did not match the entered token.")
    print("Keychain round-trip verification: PASS")


def load_token():
    return load_password(KEYCHAIN_SERVICE, getpass.getuser()).strip()


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


def bridge_healthy(port, attempts=40, delay=0.25):
    for _ in range(attempts):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                         headers={"Authorization": "Bearer status-check"})
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(delay)
    return False


def install(args):
    if sys.platform != "darwin":
        raise BridgeError("This installer supports macOS only.")
    if args.use_keychain:
        if not password_exists(KEYCHAIN_SERVICE, getpass.getuser()):
            raise BridgeError("No saved CreateAI token was found. Run install without --use-keychain once.")
        print("Using the existing CreateAI token from macOS Keychain.")
    else:
        store_token()
    token = load_token()
    upstream = Upstream(ENVIRONMENTS[args.environment], token)
    checked = args.model
    if checked == AUTO:
        available = [item["id"] for item in upstream.models().get("data", [])] or list(KNOWN_MODELS)
        checked = resolve(codex_model(), available, "defaults")
        print(f"Model mapping is automatic; verifying {checked} for the configured Codex model.")
    print("Testing CreateAI before changing Codex configuration...")
    if not diagnose(upstream, checked):
        raise BridgeError("CreateAI diagnostics failed. Codex configuration was not changed.")
    print("Testing CreateAI tool calls and tool-result continuation...")
    doctor(upstream, checked)

    original = CONFIG.read_text() if CONFIG.exists() else ""
    if BEGIN_PREFIX in original or "[model_providers.asu_autofallback]" in original:
        raise BridgeError("ASU Codex bridge is already installed. Run status or uninstall first.")
    prior_provider = top_level_value(original, "model_provider")
    original_without_provider = remove_top_level_key(original, "model_provider").lstrip()
    updated = 'model_provider = "asu_autofallback"\n'
    if original_without_provider:
        updated += original_without_provider.rstrip() + "\n\n"
    updated += config_block(args.port)
    backup = CONFIG.with_name(f"config.toml.asu-backup-{int(time.time())}")
    if CONFIG.exists():
        shutil.copy2(CONFIG, backup)
    state = {"prior_model_provider_line": prior_provider, "backup": str(backup) if CONFIG.exists() else None,
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
    atomic_write(STATE, json.dumps(state, indent=2).encode())
    atomic_write(PLIST, plistlib.dumps(plist))
    try:
        launchctl("stop")
        launchctl("start")
        if not bridge_healthy(args.port):
            raise BridgeError(f"LaunchAgent started but the bridge did not answer on port {args.port}.")
        # Route Codex only after the background service is confirmed healthy.
        atomic_write(CONFIG, updated.encode())
    except Exception:
        launchctl("stop")
        if PLIST.exists():
            PLIST.unlink()
        if STATE.exists():
            STATE.unlink()
        raise
    print("Installed. Fully quit and reopen ChatGPT/Codex so it reloads config.toml.")


def uninstall(args):
    launchctl("stop")
    if CONFIG.exists():
        text = remove_block(CONFIG.read_text())
        text = remove_top_level_key(text, "model_provider")
        if STATE.exists():
            state = json.loads(STATE.read_text())
            prior = state.get("prior_model_provider_line")
            if prior:
                text = prior + "\n" + text.lstrip()
        atomic_write(CONFIG, text.encode())
    if PLIST.exists():
        PLIST.unlink()
    if STATE.exists():
        STATE.unlink()
    if not args.keep_token:
        delete_password(KEYCHAIN_SERVICE, getpass.getuser())
    print("Removed. Fully quit and reopen ChatGPT/Codex.")


def status(_args):
    configured = CONFIG.exists() and BEGIN_PREFIX in CONFIG.read_text()
    try:
        token = password_exists(KEYCHAIN_SERVICE, getpass.getuser())
    except BridgeError:
        token = False
    service = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    healthy = False
    port = DEFAULT_PORT
    if STATE.exists():
        try:
            port = int(json.loads(STATE.read_text()).get("port", DEFAULT_PORT))
        except (ValueError, json.JSONDecodeError):
            pass
    healthy = bridge_healthy(port, attempts=2)
    print(f"Config: {'installed' if configured else 'not installed'}")
    print(f"Keychain token: {'present' if token else 'missing'}")
    print(f"LaunchAgent: {'loaded' if service else 'not loaded'}")
    print(f"Bridge health: {'ok' if healthy else 'unavailable'}")
    return configured and token and service and healthy


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
                                help="auto maps each Codex model to its CreateAI counterpart, "
                                     "or pass one exact CreateAI id")
    install_parser.add_argument("--use-keychain", action="store_true",
                                help="Use the CreateAI token already stored in macOS Keychain")
    install_parser.add_argument("--primary", choices=("chatgpt", "api"), default="chatgpt")
    install_parser.add_argument("--port", type=lambda value: port_number(value), default=DEFAULT_PORT)
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
