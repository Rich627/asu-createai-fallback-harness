import tempfile
import tomllib
import unittest
from pathlib import Path

import setup_macos
from setup_macos import config_block, port_number, remove_block, remove_top_level_key, top_level_value


class ConfigEditingTests(unittest.TestCase):
    def test_install_help_has_keychain_reuse(self):
        source = Path(setup_macos.__file__).read_text()
        self.assertIn('"--use-keychain"', source)
    def test_managed_provider_is_valid_toml_with_existing_tables(self):
        original = '''model = "gpt-5.6-sol"
model_provider = "old"

[features]
apps = true
'''
        prior = top_level_value(original, "model_provider")
        remainder = remove_top_level_key(original, "model_provider").lstrip()
        updated = 'model_provider = "asu_autofallback"\n' + remainder.rstrip() + "\n\n" + config_block(41117)
        parsed = tomllib.loads(updated)
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["model_provider"], "asu_autofallback")
        self.assertTrue(parsed["features"]["apps"])
        self.assertEqual(parsed["model_providers"]["asu_autofallback"]["base_url"],
                         "http://127.0.0.1:41117/v1")
        restored = remove_top_level_key(remove_block(updated), "model_provider")
        restored = prior + "\n" + restored.lstrip()
        self.assertEqual(tomllib.loads(restored), tomllib.loads(original))

    def test_no_prior_provider_restores_without_one(self):
        original = 'model = "gpt-5.6-sol"\n'
        updated = 'model_provider = "asu_autofallback"\n' + original + "\n" + config_block(41117)
        restored = remove_top_level_key(remove_block(updated), "model_provider")
        self.assertEqual(tomllib.loads(restored), tomllib.loads(original))

    def test_atomic_write_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            setup_macos.atomic_write(path, b"x")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_port_validation(self):
        self.assertEqual(port_number("41117"), 41117)
        with self.assertRaises(Exception):
            port_number("80")


if __name__ == "__main__":
    unittest.main()
