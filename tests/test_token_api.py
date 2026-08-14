from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multiagent_cli.token_api import (
    TokenAPICredentials,
    known_incompatible_reason,
    public_model_catalog,
)


class TokenAPICredentialsTests(unittest.TestCase):
    def test_private_file_round_trip_never_exposes_full_key_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            credentials = TokenAPICredentials(root, environ={})
            credentials.save("sk-company-secret-1234")

            status = credentials.status()
            content = credentials.path.read_text(encoding="utf-8")

            self.assertEqual(credentials.load(), "sk-company-secret-1234")
            self.assertTrue(status["configured"])
            self.assertEqual(status["masked"], "••••1234")
            self.assertNotIn("sk-company-secret-1234", json.dumps(status))
            self.assertIn("sk-company-secret-1234", content)
            if sys.platform != "win32":
                self.assertEqual(credentials.directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(credentials.path.stat().st_mode & 0o777, 0o600)

    def test_environment_takes_precedence_over_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            saved = TokenAPICredentials(directory, environ={})
            saved.save("saved-secret-1234")
            credentials = TokenAPICredentials(
                directory,
                environ={"MULTIAGENT_TOKEN_API_KEY": "env-secret-9876"},
            )

            self.assertEqual(credentials.load(), "env-secret-9876")
            self.assertEqual(credentials.status()["source"], "MULTIAGENT_TOKEN_API_KEY")

    def test_status_loads_private_credentials_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credentials = TokenAPICredentials(directory, environ={})
            credentials.save("saved-secret-1234")
            with patch.object(credentials, "load", wraps=credentials.load) as load:
                status = credentials.status()

        self.assertEqual(load.call_count, 1)
        self.assertEqual(status["source"], "private_file")


class TokenAPIModelCatalogTests(unittest.TestCase):
    def test_catalog_contains_only_documented_compatible_models(self) -> None:
        catalog = public_model_catalog()
        claude = {model["id"] for model in catalog["claude"]}
        codex = {model["id"] for model in catalog["codex"]}

        self.assertIn("claude-opus-5[1M]", claude)
        self.assertIn("openai-gpt-5.6-sol", claude)
        self.assertIn("gemini-3.5-flash", claude)
        self.assertIn("glm-5.2[1M]", claude)
        self.assertIn("qwen3.8-max", claude)
        self.assertIn("gpt-5.6-sol", codex)
        self.assertIn("gpt-5.3-codex-spark", codex)
        self.assertNotIn("unavailable", catalog)
        self.assertNotIn("limitations", catalog)

    def test_known_cross_protocol_names_are_rejected_with_reason(self) -> None:
        self.assertIn(
            "Claude Code",
            known_incompatible_reason("codex", "gemini-3.5-flash"),
        )
        self.assertIn(
            "网关别名",
            known_incompatible_reason("claude", "gpt-5.6-sol"),
        )


if __name__ == "__main__":
    unittest.main()
