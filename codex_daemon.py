#!/usr/bin/env python3
"""Long-running login service for ASU Codex automatic failover (macOS and Windows)."""

import argparse
import getpass
import sys
from pathlib import Path

from createai import BridgeError, Upstream
from codex_asu import ENVIRONMENTS
from codex_router import FallbackServer, Primary
import credstore
from credstore import load_password
from model_map import AUTO

KEYCHAIN_SERVICE = "edu.asu.createai.codex-fallback"
DEFAULT_PORT = 41117


def keychain_token():
    token = load_password(KEYCHAIN_SERVICE, getpass.getuser()).strip()
    if not token:
        raise BridgeError(f"The CreateAI token stored in {credstore.BACKEND} is empty.")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    parser.add_argument("--model", default=AUTO,
                        help="auto maps each requested model to its CreateAI counterpart; "
                             "or pass one exact CreateAI id")
    parser.add_argument("--primary", choices=("chatgpt", "api"), default="chatgpt")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default=None,
                        help="append stdout and stderr here. A LaunchAgent redirects for us; "
                             "a Windows scheduled task has no equivalent, so the service does it")
    args = parser.parse_args()
    if args.log:
        # A scheduled task cannot redirect for us, so do it before anything is written.
        destination = Path(args.log)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream = open(destination, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = stream
    try:
        token = keychain_token()
        server = FallbackServer(
            Upstream(ENVIRONMENTS[args.environment], token),
            token="unused-local-secret",
            primary=Primary(args.primary),
            asu_model=args.model,
            port=args.port,
        )
        # The OpenAI bearer token authenticates Codex to this loopback service. Browser
        # requests remain rejected by Origin; the ASU token never enters Codex.
        server.accept_any_bearer = True
        print(f"ASU Codex bridge listening on {server.base_url}", file=sys.stderr, flush=True)
        server.serve_forever()
    except (BridgeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
