from pathlib import Path
import tempfile
import unittest

from huginn_leads.config import TOKEN_PLACEHOLDER, ensure_env


class ConfigTests(unittest.TestCase):
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
