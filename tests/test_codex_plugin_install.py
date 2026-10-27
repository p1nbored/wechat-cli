"""Portable installer tests use temporary homes and never install real plugins."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "codex_plugin_install",
    Path(__file__).resolve().parents[1] / "codex-plugin" / "install.py",
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for relative in ("plugin.json", ".codex-plugin/plugin.json",
                         "skills/wechat-cli/SKILL.md", "README.md"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test payload", encoding="utf-8")
        (self.source / "all_keys.json").write_text("private", encoding="utf-8")
        (self.source / "install.py").write_text("installer", encoding="utf-8")
        self.home = self.root / "codex home 中文"
        self.marketplace = self.home / "local-marketplaces/p1nbored-local"
        self.manifest = self.marketplace / ".agents/plugins/marketplace.json"
        self.destination = self.marketplace / "plugins/wechat-cli"
        self.run = patch.object(installer.subprocess, "run").start()
        patch.object(installer.shutil, "which", return_value="/test/codex").start()
        self.addCleanup(patch.stopall)

    def install(self) -> None:
        installer.install_plugin(self.home, self.source)

    def test_preserves_other_entries_and_metadata_and_is_repeatable(self) -> None:
        self.manifest.parent.mkdir(parents=True)
        other = {"name": "other", "source": {"path": "./other"}}
        original = {"name": "p1nbored-local", "owner": {"name": "owner"},
                    "plugins": [other, {"name": "wechat-cli", "old": True}]}
        self.manifest.write_text(json.dumps(original), encoding="utf-8")
        self.install()
        self.install()
        actual = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(actual["owner"], original["owner"])
        self.assertEqual(actual["plugins"][0], other)
        self.assertEqual(len(actual["plugins"]), 2)
        self.assertEqual(actual["plugins"][1]["source"],
                         {"source": "local", "path": "./plugins/wechat-cli"})

    def test_only_package_payload_is_copied(self) -> None:
        self.install()
        actual = {str(path.relative_to(self.destination))
                  for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(actual, {"plugin.json", ".codex-plugin/plugin.json",
                                  "skills/wechat-cli/SKILL.md", "README.md"})

    def test_reinstall_removes_deleted_skills_and_preserves_other_plugins(self) -> None:
        old_skill = self.source / "skills/obsolete/SKILL.md"
        old_skill.parent.mkdir()
        old_skill.write_text("obsolete instructions", encoding="utf-8")
        self.install()
        other = self.destination.parent / "other/skills/untouched.md"
        other.parent.mkdir(parents=True)
        other.write_text("other plugin", encoding="utf-8")
        old_skill.unlink()
        old_skill.parent.rmdir()
        self.install()
        self.assertFalse((self.destination / "skills/obsolete").exists())
        self.assertTrue((self.destination / "skills/wechat-cli/SKILL.md").is_file())
        self.assertEqual(other.read_text(encoding="utf-8"), "other plugin")

    def test_main_uses_explicit_home_and_reports_success(self) -> None:
        with patch.object(installer.sys, "argv", ["install.py", "--codex-home", str(self.home)]):
            with patch.object(installer, "install_plugin") as install:
                self.assertEqual(installer.main(), 0)
        install.assert_called_once_with(self.home.resolve(), Path(installer.__file__).parent)

    def test_main_returns_error_and_prints_failure(self) -> None:
        errors = io.StringIO()
        with patch.object(installer.sys, "argv", ["install.py"]):
            with patch.object(installer, "install_plugin", side_effect=ValueError("invalid marketplace")):
                with contextlib.redirect_stderr(errors):
                    self.assertEqual(installer.main(), 1)
        self.assertIn("Plugin installation failed: invalid marketplace", errors.getvalue())

    def test_failed_skills_publication_restores_previous_tree(self) -> None:
        self.install()
        installed = self.destination / "skills/wechat-cli/SKILL.md"
        original = installed.read_text(encoding="utf-8")
        (self.source / "skills/wechat-cli/SKILL.md").write_text("updated", encoding="utf-8")
        replace = installer.os.replace

        def fail_publication(source: Path, target: Path) -> None:
            if Path(source).name == "updated":
                raise OSError("simulated publication failure")
            replace(source, target)

        with patch.object(installer.os, "replace", side_effect=fail_publication):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(installed.read_text(encoding="utf-8"), original)

    def test_registers_then_installs_with_selected_home(self) -> None:
        self.install()
        commands = [call.args[0] for call in self.run.call_args_list]
        self.assertEqual(commands, [
            ["/test/codex", "plugin", "marketplace", "add", str(self.marketplace)],
            ["/test/codex", "plugin", "add", "wechat-cli@p1nbored-local", "--json"],
        ])
        for call in self.run.call_args_list:
            self.assertTrue(call.kwargs["check"])
            self.assertEqual(call.kwargs["env"]["CODEX_HOME"], str(self.home))

    def test_invalid_marketplace_fails_before_mutation(self) -> None:
        for value in ([], {"name": "wrong", "plugins": []},
                      {"name": "p1nbored-local", "plugins": "bad"},
                      {"name": "p1nbored-local", "plugins": [None]}):
            with self.subTest(value=value):
                self.manifest.parent.mkdir(parents=True, exist_ok=True)
                original = json.dumps(value)
                self.manifest.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.manifest.read_text(encoding="utf-8"), original)
        self.run.assert_not_called()

    def test_registration_failure_does_not_run_plugin_add(self) -> None:
        self.run.side_effect = subprocess.CalledProcessError(3, "codex")
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertEqual(self.run.call_count, 1)

    def test_missing_codex_does_not_create_marketplace(self) -> None:
        with patch.object(installer.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                self.install()
        self.assertFalse(self.home.exists())

    def test_source_symlink_is_rejected_before_mutation(self) -> None:
        (self.source / "skills/leaked.json").symlink_to(self.source / "all_keys.json")
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.home.exists())

    def test_symlinked_source_parent_is_rejected(self) -> None:
        metadata = self.source / ".codex-plugin"
        moved = self.root / "outside_metadata"
        metadata.rename(moved)
        metadata.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.home.exists())

    def test_plugin_add_failure_is_propagated(self) -> None:
        self.run.side_effect = [None, subprocess.CalledProcessError(2, "codex")]
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertEqual(self.run.call_count, 2)

    def test_destination_symlink_is_rejected_without_writing_target(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_home_argument_overrides_environment_then_uses_default(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            self.assertEqual(installer.select_home(None), self.home.resolve())
            self.assertEqual(installer.select_home(str(self.root)), self.root.resolve())
        with patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(installer.select_home(None), (Path.home() / ".codex").resolve())


if __name__ == "__main__":
    unittest.main()
