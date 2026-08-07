from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from multiagent_cli import web_launcher


class WebLauncherTests(unittest.TestCase):
    def test_existing_server_is_reused_and_opened(self) -> None:
        with (
            patch.object(web_launcher, "_ui_is_running", return_value=True),
            patch.object(web_launcher, "RunStore", return_value=Mock()),
            patch.object(web_launcher, "select_ui_workspace") as select_workspace,
            patch.object(web_launcher.webbrowser, "open") as open_browser,
            patch.object(web_launcher, "serve_ui") as serve,
        ):
            result = web_launcher.main(["--port", "9876"])

        self.assertEqual(result, 0)
        select_workspace.assert_called_once_with(
            "http://127.0.0.1:9876/",
            Path.cwd().resolve(),
        )
        open_browser.assert_called_once_with("http://127.0.0.1:9876/")
        serve.assert_not_called()

    def test_launcher_starts_quiet_server_without_a_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = Mock()
            with (
                patch.object(web_launcher, "_ui_is_running", return_value=False),
                patch.object(web_launcher, "_port_is_available", return_value=True),
                patch.object(web_launcher, "RunStore", return_value=store),
                patch.object(web_launcher, "serve_ui", return_value=0) as serve,
            ):
                result = web_launcher.main(
                    ["--workspace", str(workspace), "--no-open"]
                )

        self.assertEqual(result, 0)
        self.assertEqual(serve.call_args.kwargs["workspace"], workspace.resolve())
        self.assertIs(serve.call_args.kwargs["store"], store)
        self.assertFalse(serve.call_args.kwargs["open_browser"])
        self.assertTrue(serve.call_args.kwargs["quiet"])

    def test_default_port_uses_fallback_when_another_service_occupies_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(web_launcher, "_ui_is_running", return_value=False),
                patch.object(web_launcher, "_wait_for_ui", return_value=False),
                patch.object(
                    web_launcher,
                    "_port_is_available",
                    side_effect=[False, True],
                ),
                patch.object(web_launcher, "serve_ui", return_value=0) as serve,
            ):
                result = web_launcher.main(
                    ["--workspace", str(workspace), "--no-open"]
                )

        self.assertEqual(result, 0)
        self.assertEqual(serve.call_args.kwargs["port"], 8766)

    def test_explicit_port_is_not_silently_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(web_launcher, "_ui_is_running", return_value=False),
                patch.object(web_launcher, "_port_is_available") as port_available,
                patch.object(web_launcher, "serve_ui", return_value=0) as serve,
            ):
                result = web_launcher.main(
                    [
                        "--workspace",
                        str(workspace),
                        "--port",
                        "9988",
                        "--no-open",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(serve.call_args.kwargs["port"], 9988)
        port_available.assert_not_called()

    def test_workspace_resolution_prefers_project_then_recent_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            (project / ".git").mkdir()
            recent = root / "recent"
            recent.mkdir()
            plain = root / "plain"
            plain.mkdir()
            store = Mock()
            store.latest.return_value = {
                "workspace": str(recent),
            }

            selected_project = web_launcher._resolve_launcher_workspace(
                store,
                cwd=project,
                environ={},
            )
            selected_recent = web_launcher._resolve_launcher_workspace(
                store,
                cwd=plain,
                environ={},
            )

        self.assertEqual(selected_project, project.resolve())
        self.assertEqual(selected_recent, recent.resolve())

    def test_invalid_environment_port_falls_back_to_default(self) -> None:
        self.assertEqual(
            web_launcher._environment_port({"MULTIAGENT_UI_PORT": "invalid"}),
            web_launcher.DEFAULT_UI_PORT,
        )


if __name__ == "__main__":
    unittest.main()
