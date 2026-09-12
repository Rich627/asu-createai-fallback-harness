#!/usr/bin/env python3
"""Long-running macOS login service for ASU Codex automatic failover."""

import argparse
import getpass
import sys

from bridge import BridgeError, Upstream
from codex_asu import ENVIRONMENTS
from router import FallbackServer, Primary
from keychain import load_password

KEYCHAIN_SERVICE = "edu.asu.createai.codex-fallback"
DEFAULT_PORT = 41117


def keychain_token():
    token = load_password(KEYCHAIN_SERVICE, getpass.getuser()).strip()
    if not token:
        raise BridgeError("CreateAI token in macOS Keychain is empty.")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=ENVIRONMENTS, default="production")
    parser.add_argument("--model", default="defaults")
    parser.add_argument("--primary", choices=("chatgpt", "api"), default="chatgpt")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
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
