import sys
import unittest

import credstore
import credvault
import keychain
from createai import BridgeError

SERVICE = "edu.asu.createai.test-credstore"
ACCOUNT = "asu-bridge-test-account"


class DispatchTests(unittest.TestCase):
    def test_picks_the_backend_for_this_platform(self):
        expected = {"darwin": keychain, "win32": credvault}.get(sys.platform)
        self.assertIs(credstore._backend, expected)

    def test_backend_is_named_for_status_output(self):
        self.assertTrue(credstore.BACKEND)

    @unittest.skipIf(sys.platform in ("darwin", "win32"), "this platform has a backend")
    def test_unsupported_platform_explains_itself(self):
        with self.assertRaises(BridgeError) as caught:
            credstore.load_password(SERVICE, ACCOUNT)
        self.assertIn("macOS or Windows", str(caught.exception))


class BackendGuardTests(unittest.TestCase):
    """Both backends import anywhere; each refuses to act off its own platform."""

    @unittest.skipIf(sys.platform == "win32", "the guard is what we are testing")
    def test_credvault_refuses_off_windows(self):
        with self.assertRaises(BridgeError):
            credvault.load_password(SERVICE, ACCOUNT)

    @unittest.skipIf(sys.platform == "darwin", "the guard is what we are testing")
    def test_keychain_refuses_off_macos(self):
        with self.assertRaises(BridgeError):
            keychain.save_password(SERVICE, ACCOUNT, "x")

    def test_windows_target_name_joins_service_and_account(self):
        """Credential Manager keys on one target name, so both halves must reach it."""
        self.assertEqual(credvault.target_name("svc", "acct"), "svc:acct")


@unittest.skipUnless(sys.platform == "win32", "needs a real Windows Credential Manager")
class RealCredentialManagerTests(unittest.TestCase):
    """Runs only on Windows, where CI exercises advapi32 for real rather than through a mock."""

    def tearDown(self):
        try:
            credstore.delete_password(SERVICE, ACCOUNT)
        except BridgeError:
            pass

    def test_round_trip_preserves_the_token_exactly(self):
        token = "sk-asu-Ω-1234567890-中文"
        credstore.save_password(SERVICE, ACCOUNT, token)
        self.assertEqual(credstore.load_password(SERVICE, ACCOUNT), token)

    def test_exists_reports_presence_and_absence(self):
        self.assertFalse(credstore.password_exists(SERVICE, ACCOUNT))
        credstore.save_password(SERVICE, ACCOUNT, "value")
        self.assertTrue(credstore.password_exists(SERVICE, ACCOUNT))

    def test_save_overwrites_rather_than_duplicating(self):
        credstore.save_password(SERVICE, ACCOUNT, "first")
        credstore.save_password(SERVICE, ACCOUNT, "second")
        self.assertEqual(credstore.load_password(SERVICE, ACCOUNT), "second")

    def test_delete_removes_and_is_idempotent(self):
        credstore.save_password(SERVICE, ACCOUNT, "value")
        credstore.delete_password(SERVICE, ACCOUNT)
        self.assertFalse(credstore.password_exists(SERVICE, ACCOUNT))
        credstore.delete_password(SERVICE, ACCOUNT)

    def test_missing_token_raises_rather_than_returning_empty(self):
        with self.assertRaises(BridgeError):
            credstore.load_password(SERVICE, ACCOUNT)

    def test_accounts_do_not_collide(self):
        credstore.save_password(SERVICE, ACCOUNT, "mine")
        other = ACCOUNT + "-other"
        try:
            credstore.save_password(SERVICE, other, "theirs")
            self.assertEqual(credstore.load_password(SERVICE, ACCOUNT), "mine")
            self.assertEqual(credstore.load_password(SERVICE, other), "theirs")
        finally:
            try:
                credstore.delete_password(SERVICE, other)
            except BridgeError:
                pass


if __name__ == "__main__":
    unittest.main()
