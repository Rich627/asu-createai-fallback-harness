"""Minimal Windows Credential Manager access using advapi32 (no secret in argv).

The macOS counterpart is `keychain.py`; `credstore.py` picks between them. Both expose the
same four functions and key on the same (service, account) pair, so nothing above this layer
has to know which platform it is on.

A Credential Manager entry is keyed by (TargetName, Type), not by a service/account pair, so
the two are joined into one target name. The blob holds UTF-8 — the same encoding the Keychain
backend uses — rather than the UTF-16 some Windows tools assume, because only this project
reads it and one encoding is easier to reason about than two.
"""

from __future__ import annotations

import ctypes
import sys

from asu.createai import BridgeError

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


if sys.platform == "win32":
    import ctypes.wintypes as wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _CredentialAttribute(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", ctypes.POINTER(ctypes.c_byte)),
        ]

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(_CredentialAttribute)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.POINTER(ctypes.POINTER(_Credential))]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None


def _require_windows():
    if sys.platform != "win32":
        raise BridgeError("Windows Credential Manager is available only on Windows.")


def target_name(service, account):
    """(service, account) joined into the single key Credential Manager actually stores."""
    return f"{service}:{account}"


def _read(service, account):
    """Returns the token, or None when no such credential exists."""
    _require_windows()
    pointer = ctypes.POINTER(_Credential)()
    ok = _advapi32.CredReadW(target_name(service, account), CRED_TYPE_GENERIC, 0,
                             ctypes.byref(pointer))
    if not ok:
        code = ctypes.get_last_error()
        if code == ERROR_NOT_FOUND:
            return None
        raise BridgeError(f"Windows Credential Manager lookup failed (error {code}).")
    try:
        blob = pointer.contents
        raw = ctypes.string_at(blob.CredentialBlob, blob.CredentialBlobSize)
    finally:
        _advapi32.CredFree(pointer)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BridgeError("The stored credential is not valid UTF-8. Save the CreateAI token "
                          "again.") from None


def load_password(service, account):
    token = _read(service, account)
    if token is None:
        raise BridgeError("CreateAI token was not found in Windows Credential Manager.")
    return token


def save_password(service, account, password):
    _require_windows()
    blob = password.encode("utf-8")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = _Credential(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=target_name(service, account),
        Comment="ASU CreateAI fallback token",
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=account,
    )
    if not _advapi32.CredWriteW(ctypes.byref(credential), 0):
        code = ctypes.get_last_error()
        raise BridgeError(f"Windows Credential Manager could not save the CreateAI token "
                          f"(error {code}).")


def password_exists(service, account):
    return _read(service, account) is not None


def delete_password(service, account):
    _require_windows()
    if not _advapi32.CredDeleteW(target_name(service, account), CRED_TYPE_GENERIC, 0):
        code = ctypes.get_last_error()
        if code == ERROR_NOT_FOUND:
            return
        raise BridgeError(f"Windows Credential Manager could not delete the CreateAI token "
                          f"(error {code}).")
