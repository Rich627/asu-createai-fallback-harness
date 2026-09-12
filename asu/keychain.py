"""Minimal macOS Keychain access using Security.framework (no secret in argv)."""

from __future__ import annotations

import ctypes
import sys

from asu.createai import BridgeError

ERR_NOT_FOUND = -25300


if sys.platform == "darwin":
    _security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    _core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    _security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    _security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    _security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    _security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    ]
    _security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    _security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
    _security.SecKeychainItemDelete.restype = ctypes.c_int32
    _security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    _core.CFRelease.argtypes = [ctypes.c_void_p]


def _require_macos():
    if sys.platform != "darwin":
        raise BridgeError("macOS Keychain is available only on macOS.")


def _encoded(service, account):
    return service.encode("utf-8"), account.encode("utf-8")


def _find(service, account, include_data):
    _require_macos()
    service_bytes, account_bytes = _encoded(service, account)
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = _security.SecKeychainFindGenericPassword(
        None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
        ctypes.byref(length) if include_data else None,
        ctypes.byref(data) if include_data else None, ctypes.byref(item),
    )
    return status, length, data, item


def load_password(service, account):
    status, length, data, item = _find(service, account, True)
    if status == ERR_NOT_FOUND:
        raise BridgeError("CreateAI token was not found in macOS Keychain.")
    if status:
        raise BridgeError(f"macOS Keychain could not read the CreateAI token (OSStatus {status}).")
    try:
        return ctypes.string_at(data, length.value).decode("utf-8")
    except UnicodeDecodeError:
        raise BridgeError("The Keychain item is not valid UTF-8. Save the CreateAI token again.") from None
    finally:
        _security.SecKeychainItemFreeContent(None, data)
        if item:
            _core.CFRelease(item)


def save_password(service, account, password):
    _require_macos()
    password_bytes = password.encode("utf-8")
    status, _length, _data, item = _find(service, account, False)
    if status == 0:
        try:
            buffer = ctypes.create_string_buffer(password_bytes)
            result = _security.SecKeychainItemModifyAttributesAndData(
                item, None, len(password_bytes), ctypes.cast(buffer, ctypes.c_void_p))
        finally:
            _core.CFRelease(item)
    elif status == ERR_NOT_FOUND:
        service_bytes, account_bytes = _encoded(service, account)
        buffer = ctypes.create_string_buffer(password_bytes)
        result = _security.SecKeychainAddGenericPassword(
            None, len(service_bytes), service_bytes, len(account_bytes), account_bytes,
            len(password_bytes), ctypes.cast(buffer, ctypes.c_void_p), None)
    else:
        raise BridgeError(f"macOS Keychain lookup failed (OSStatus {status}).")
    if result:
        raise BridgeError(f"macOS Keychain could not save the CreateAI token (OSStatus {result}).")


def password_exists(service, account):
    status, _length, _data, item = _find(service, account, False)
    if item:
        _core.CFRelease(item)
    if status not in (0, ERR_NOT_FOUND):
        raise BridgeError(f"macOS Keychain lookup failed (OSStatus {status}).")
    return status == 0


def delete_password(service, account):
    status, _length, _data, item = _find(service, account, False)
    if status == ERR_NOT_FOUND:
        return
    if status:
        raise BridgeError(f"macOS Keychain lookup failed (OSStatus {status}).")
    try:
        result = _security.SecKeychainItemDelete(item)
    finally:
        _core.CFRelease(item)
    if result:
        raise BridgeError(f"macOS Keychain could not delete the CreateAI token (OSStatus {result}).")
