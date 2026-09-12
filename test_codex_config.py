import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    tomllib = None

import codex_config
from codex_config import (config_block, managed, remove_block, remove_top_level_key,
                          top_level_value)


class ConfigEditingTests(unittest.TestCase):
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
        """The marker has twice named the installer file that wrote it. A block left by any of
        those versions must still be recognized, or an upgrade orphans it in config.toml."""
        for legacy_marker in ("# BEGIN ASU CODEX BRIDGE (managed by setup_macos.py)",
                              "# BEGIN ASU CODEX BRIDGE (managed by setup_codex_macos.py)"):
            with self.subTest(marker=legacy_marker):
                legacy = ('model = "gpt-5.6-sol"\n\n'
                          + legacy_marker + "\n"
                          '[model_providers.asu_autofallback]\n'
                          'base_url = "http://127.0.0.1:41117/v1"\n'
                          "# END ASU CODEX BRIDGE\n")
                self.assertTrue(managed(legacy))
                self.assertEqual(remove_block(legacy).strip(), 'model = "gpt-5.6-sol"')

    def test_current_marker_carries_no_filename(self):
        """Both platforms write the same marker, so it must not name one platform's installer."""
        self.assertTrue(codex_config.BEGIN.startswith(codex_config.BEGIN_PREFIX))
        self.assertNotIn(".py", codex_config.BEGIN)

    def test_block_round_trips_through_its_own_remover(self):
        text = "model = \"x\"\n\n" + config_block(41117)
        self.assertTrue(managed(text))
        self.assertEqual(remove_block(text).strip(), 'model = "x"')

    def test_incomplete_block_is_refused_rather_than_guessed(self):
        from createai import BridgeError
        with self.assertRaises(BridgeError):
            remove_block("a = 1\n" + codex_config.BEGIN + "\n[x]\n")


if __name__ == "__main__":
    unittest.main()
