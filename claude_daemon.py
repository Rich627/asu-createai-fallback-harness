#!/usr/bin/env python3
"""Long-running login service: Claude Code with CreateAI usage-limit fallback (macOS and Windows)."""

import argparse
import getpass
import sys
from pathlib import Path
import threading
import time

from anthropic_bridge import DEFAULT_MODEL
from model_map import AUTO
from createai import BridgeError, Upstream
from claude_router import ANTHROPIC_URL, Primary, RouterServer
from codex_asu import ENVIRONMENTS
import credstore
from credstore import load_password, password_exists

KEYCHAIN_SERVICE = "edu.asu.createai.claude-fallback"
SHARED_SERVICE = "edu.asu.createai.codex-fallback"
DEFAULT_PORT = 41118


def keychain_token():
    account = getpass.getuser()
    for service in (KEYCHAIN_SERVICE, SHARED_SERVICE):
        if password_exists(service, account):
            token = load_password(service, account).strip()
            if token:
                return token
            raise BridgeError(f"The CreateAI token stored in {credstore.BACKEND} as {service} is empty.")
    raise BridgeError(f"No CreateAI token found in {credstore.BACKEND}. Run the installer for this platform (setup_claude_macos.py or setup_claude_windows.py) once.")


def add_arguments(parser):
    parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    parser.add_argument("--model", default=AUTO,
                        help=f"auto maps each requested Claude model to its CreateAI counterpart "
                             f"(unmapped models use {DEFAULT_MODEL}); or pass one exact CreateAI id")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default=None,
                        help="append stdout and stderr here. A LaunchAgent redirects for us; "
                             "a Windows scheduled task has no equivalent, so the service does it")
    return parser


def build_server(args, token=None):
    upstream = Upstream(ENVIRONMENTS[args.environment], token, timeout=900) if token else None
    return RouterServer(upstream, Primary(ANTHROPIC_URL), model=args.model, port=args.port)


def load_token_later(server, args, delay=30):
    """The credential store can still be locked when the service starts."""
    def loop():
        while server.upstream is None:
            try:
                server.upstream = Upstream(ENVIRONMENTS[args.environment], keychain_token(), timeout=900)
                print("CreateAI token loaded from Keychain.", file=sys.stderr, flush=True)
            except BridgeError as exc:
                print(f"CreateAI token unavailable ({exc}); retrying in {delay}s.", file=sys.stderr, flush=True)
                time.sleep(delay)
    threading.Thread(target=loop, daemon=True).start()


def main():
    args = add_arguments(argparse.ArgumentParser(description=__doc__)).parse_args()
    if getattr(args, "log", None):
        # A scheduled task cannot redirect for us, so do it before anything is written.
        destination = Path(args.log)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream = open(destination, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = stream
    try:
        server = build_server(args)
        print(f"Claude bridge on {server.base_url}; primary {ANTHROPIC_URL}, "
              f"fallback {args.environment}/{args.model}", file=sys.stderr, flush=True)
        load_token_later(server, args)
        server.serve_forever()
    except (BridgeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
