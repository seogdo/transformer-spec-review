"""Launcher tests use temporary fake credentials; no server/API is started."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("handoff_launcher", Path(__file__).resolve().parents[1] / "scripts/run_web.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config(self, *extra):
        return launcher.configure(["--env-file", str(self.env_file), *extra])

    def test_final_launcher_defaults_to_openai_without_request(self):
        self.assertEqual(self.config().port, 8765)
        self.assertEqual(os.environ["LLM_BACKEND"], "openai")
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_env_selects_ollama_and_cli_can_override(self):
        self.env_file.write_text("LLM_BACKEND=ollama\n", encoding="utf-8")
        self.config()
        self.assertEqual(os.environ["LLM_BACKEND"], "ollama")
        self.config("--backend", "openai")
        self.assertEqual(os.environ["LLM_BACKEND"], "openai")

    def test_key_literal_and_only_allowlisted_variables(self):
        self.env_file.write_text("OPENAI_API_KEY='fake-not-a-key'\nOPENAI_MODEL=example-model\nUNRELATED=ignored\n", encoding="utf-8")
        self.config()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "fake-not-a-key")
        self.assertEqual(os.environ["OPENAI_MODEL"], "example-model")
        self.assertNotIn("UNRELATED", os.environ)

    def test_existing_environment_wins_over_file(self):
        os.environ["OPENAI_MODEL"] = "process-model"
        self.env_file.write_text("OPENAI_MODEL=file-model\n", encoding="utf-8")
        self.config()
        self.assertEqual(os.environ["OPENAI_MODEL"], "process-model")

    def test_shell_syntax_is_never_executed(self):
        sentinel = Path(self.tmp.name) / "should-not-exist"
        literal = f"$(touch {sentinel})"
        self.env_file.write_text(f"OPENAI_MODEL={literal}\n", encoding="utf-8")
        self.config()
        self.assertEqual(os.environ["OPENAI_MODEL"], literal)
        self.assertFalse(sentinel.exists())

    def test_invalid_line_does_not_expose_secret_or_partially_load(self):
        self.env_file.write_text('OPENAI_MODEL=example\nOPENAI_API_KEY="fake-private-value\n', encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            self.config()
        self.assertNotIn("fake-private-value", output.getvalue())
        self.assertNotIn("OPENAI_MODEL", os.environ)

    def test_invalid_backend_is_rejected_without_echoing_value(self):
        os.environ["LLM_BACKEND"] = "not-a-provider-secret"
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            self.config()
        self.assertNotIn("not-a-provider-secret", output.getvalue())

    def test_duplicate_key_rejected(self):
        self.env_file.write_text("OPENAI_MODEL=one\nOPENAI_MODEL=two\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            launcher.load_env_file(self.env_file)

    def test_port_validation(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.config("--port", "0")
        self.assertEqual(self.config("--port", "8766").port, 8766)
