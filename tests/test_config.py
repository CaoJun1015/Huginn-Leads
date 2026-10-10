from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from huginn_leads.config import TOKEN_PLACEHOLDER, ensure_env, load_settings


class ConfigTests(unittest.TestCase):
    def test_typesafe_settings_from_file_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TYPESAFE_API_KEY=file-secret\nTYPESAFE_MODEL=jev-latest\n", encoding="utf-8")
            with patch.dict("os.environ", {"HUGINN_ROOT": directory}, clear=True):
                settings = load_settings()
                self.assertEqual(settings.typesafe_api_key, "file-secret")
                self.assertEqual(settings.typesafe_model, "jev-latest")
                self.assertNotIn("file-secret", repr(settings))
                with patch.dict("os.environ", {"TYPESAFE_API_KEY": "env-secret", "TYPESAFE_MODEL": "jev-1.13.0"}):
                    self.assertEqual(load_settings().typesafe_api_key, "env-secret")
                    self.assertEqual(load_settings().typesafe_model, "jev-1.13.0")
                with patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}):
                    self.assertEqual(load_settings().typesafe_api_key, "")

    def test_ensure_env_creates_token_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text(f"ONEBOT_ACCESS_TOKEN={TOKEN_PLACEHOLDER}\n", encoding="utf-8")

            env_path = ensure_env(root)
            first = env_path.read_text(encoding="utf-8")
            ensure_env(root)

            self.assertNotIn(TOKEN_PLACEHOLDER, first)
            self.assertEqual(env_path.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
