import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    tomllib = None

import setup_codex_macos
from setup_codex_macos import config_block, port_number, remove_block, remove_top_level_key, top_level_value


class ConfigEditingTests(unittest.TestCase):
    def test_install_help_has_keychain_reuse(self):
        source = Path(setup_codex_macos.__file__).read_text()
        self.assertIn('"--use-keychain"', source)
    @unittest.skipUnless(tomllib, "TOML validation needs Python 3.11+")
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

    @unittest.skipUnless(tomllib, "TOML validation needs Python 3.11+")
    def test_no_prior_provider_restores_without_one(self):
        original = 'model = "gpt-5.6-sol"\n'
        updated = 'model_provider = "asu_autofallback"\n' + original + "\n" + config_block(41117)
        restored = remove_top_level_key(remove_block(updated), "model_provider")
        self.assertEqual(tomllib.loads(restored), tomllib.loads(original))

    def test_block_written_by_an_older_version_is_still_removed(self):
        """The marker used to name setup_macos.py. A block left by that version must still be
        recognized, or an upgrade would orphan it in the user's config.toml forever."""
        legacy = ('model = "gpt-5.6-sol"\n\n'
                  "# BEGIN ASU CODEX BRIDGE (managed by setup_macos.py)\n"
                  '[model_providers.asu_autofallback]\n'
                  'base_url = "http://127.0.0.1:41117/v1"\n'
                  "# END ASU CODEX BRIDGE\n")
        self.assertEqual(remove_block(legacy).strip(), 'model = "gpt-5.6-sol"')

    def test_current_marker_names_the_current_installer(self):
        self.assertIn("setup_codex_macos.py", setup_codex_macos.BEGIN)
        self.assertTrue(setup_codex_macos.BEGIN.startswith(setup_codex_macos.BEGIN_PREFIX))

    def test_atomic_write_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            setup_codex_macos.atomic_write(path, b"x")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_port_validation(self):
        self.assertEqual(port_number("41117"), 41117)
        with self.assertRaises(Exception):
            port_number("80")


if __name__ == "__main__":
    unittest.main()
