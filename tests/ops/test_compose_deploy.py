"""Runnable without app dependencies: python -m unittest discover -s tests/ops."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops/compose"))
import deploy
import healthcheck

SHA = "a" * 40
OLD = "b" * 40
ENV = f"BOT_IMAGE=ghcr.io/jekudy/vibe-gatekeeper-bot:sha-{OLD}\nWEB_IMAGE=ghcr.io/jekudy/vibe-gatekeeper-web:sha-{OLD}\n"


class ComposeDeployTest(unittest.TestCase):
    def test_invalid_or_duplicate_pin_rejected(self):
        for text in (ENV.replace(OLD, "main"), ENV + ENV):
            with self.assertRaises(ValueError):
                deploy.image_values(text)

    def test_deploy_and_failure_rollback_only_applications(self):
        for failed in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text(ENV)
                events = []

                def compose(*args):
                    events.append(args)

                readiness = [RuntimeError("unhealthy"), None] if failed else [None]
                with (
                    patch.object(deploy, "ROOT", root),
                    patch.dict(os.environ, REPOSITORY="Jekudy/vibeshkoder"),
                    patch.object(
                        deploy.subprocess, "check_output", return_value=SHA + "\trefs/heads/main"
                    ),
                    patch.object(deploy, "compose", side_effect=compose),
                    patch.object(deploy, "wait_healthy", side_effect=readiness),
                ):
                    if failed:
                        with self.assertRaises(RuntimeError):
                            deploy.deploy(SHA)
                    else:
                        deploy.deploy(SHA)
                self.assertEqual(
                    events[:3],
                    [
                        ("pull", "bot", "web"),
                        ("stop", "bot"),
                        ("up", "-d", "--no-deps", "bot", "web"),
                    ],
                )
                if failed:
                    self.assertEqual((root / ".env").read_text(), ENV)
                    self.assertEqual(
                        events[3:], [("stop", "bot"), ("up", "-d", "--no-deps", "bot", "web")]
                    )
                else:
                    self.assertEqual((root / ".env").read_text(), ENV.replace(OLD, SHA))

    def test_stale_release_does_not_touch_environment(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(deploy, "ROOT", Path(directory)),
            patch.dict(os.environ, REPOSITORY="Jekudy/vibeshkoder"),
            patch.object(deploy.subprocess, "check_output", return_value=OLD + "\trefs/heads/main"),
            patch.object(deploy, "compose") as compose,
        ):
            deploy.deploy(SHA)
            compose.assert_not_called()

    def test_db_failure_is_red_and_payload_has_nested_data(self):
        def check(service, code):
            # Execute the actual generated code against a fake HTTP response.
            import io

            payload = (
                b'{"db":"ok"}' if "/healthz/db" in code else b'{"status":"ok","db":{"ok":true}}'
            )
            with patch("urllib.request.urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaises(SystemExit) as exited:
                    exec(code, {})
                return exited.exception.code == 0

        with patch.object(healthcheck, "container_check", side_effect=check):
            self.assertTrue(all(healthcheck.local_checks().values()))
        with patch.object(healthcheck, "container_check", side_effect=[True, False, True]):
            self.assertFalse(all(healthcheck.local_checks().values()))


if __name__ == "__main__":
    unittest.main()
