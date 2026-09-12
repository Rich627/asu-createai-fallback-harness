"""The credential store for whichever platform this is running on.

`keychain.py` (macOS Security.framework) and `credvault.py` (Windows Credential Manager) both
expose the same four functions and key on the same (service, account) pair. Everything that
needs the CreateAI token goes through here, so no caller has to branch on the platform. Both
backends are safe to import anywhere — each one's native library is loaded only on its own
platform — so this module can be imported on Linux too, where using it raises instead.
"""

from __future__ import annotations

import sys

from asu import credvault
from asu import keychain
from asu.createai import BridgeError

if sys.platform == "darwin":
    _backend = keychain
    BACKEND = "macOS Keychain"
elif sys.platform == "win32":
    _backend = credvault
    BACKEND = "Windows Credential Manager"
else:
    _backend = None
    BACKEND = "no supported credential store"


def _require_backend():
    if _backend is None:
        raise BridgeError(
            f"Storing the CreateAI token needs macOS or Windows; this is {sys.platform}. "
            "The bridges themselves are portable — pass the token to the daemon another way.")
    return _backend


def load_password(service, account):
    return _require_backend().load_password(service, account)


def save_password(service, account, password):
    return _require_backend().save_password(service, account, password)


def password_exists(service, account):
    return _require_backend().password_exists(service, account)


def delete_password(service, account):
    return _require_backend().delete_password(service, account)
