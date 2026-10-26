"""Regression tests for safe macOS scanner failures; no live process access."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_cli.keys import scanner_macos


class ExtractKeysTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work_dir = Path(self.directory.name)
        self.db_dir = self.work_dir / "db_storage"
        self.db_dir.mkdir()
        self.output = self.work_dir / "published.json"
        self.scanner_output = self.work_dir / "all_keys.json"
        self.secret = "ab" * 32
        self.salt = "cd" * 16
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(scanner_macos, "_find_binary", return_value="/test/scanner").start()
        self.run = patch.object(scanner_macos.subprocess, "run").start()
        # Guard against the original implementation reaching the real application.
        self.resign = patch.object(
            scanner_macos, "_resign_wechat", create=True,
            return_value=(False, "test: application must remain untouched"),
        ).start()

    def invoke(self) -> dict:
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return scanner_macos.extract_keys(str(self.db_dir), str(self.output))

    def assert_secret_absent(self, error: BaseException | None = None) -> None:
        displayed = self.stdout.getvalue() + self.stderr.getvalue() + str(error or "")
        self.assertNotIn(self.secret, displayed)

    def write_scanner_output(self) -> dict:
        data = {"session/session.db": {"enc_key": self.secret, "salt": self.salt}}
        self.scanner_output.write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_access_denial_never_resigns_or_publishes_keys(self) -> None:
        self.run.return_value = subprocess.CompletedProcess(
            ["/test/scanner"], 1, "", "task_for_pid failed: 5",
        )
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.resign.assert_not_called()
        self.run.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_access_denial_does_not_display_scanner_secrets(self) -> None:
        self.run.return_value = subprocess.CompletedProcess(
            ["/test/scanner"], 1, self.secret, "task_for_pid failed: 5",
        )
        with self.assertRaises(RuntimeError) as caught:
            self.invoke()
        self.assert_secret_absent(caught.exception)

    def test_nonzero_exit_rejects_stale_keys_and_preserves_destination(self) -> None:
        original_data = self.write_scanner_output()
        self.output.write_text("existing destination", encoding="utf-8")
        self.run.return_value = subprocess.CompletedProcess(
            ["/test/scanner"], 2, "", "scanner failed",
        )
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing destination")
        self.assertEqual(json.loads(self.scanner_output.read_text()), original_data)

    def test_missing_result_does_not_include_raw_output_in_error(self) -> None:
        self.run.return_value = subprocess.CompletedProcess(
            ["/test/scanner"], 0, self.secret, self.secret,
        )
        with self.assertRaises(RuntimeError) as caught:
            self.invoke()
        self.assert_secret_absent(caught.exception)
        self.assertFalse(self.output.exists())

    def test_success_saves_keys_without_printing_scanner_output(self) -> None:
        data = self.write_scanner_output()
        self.run.return_value = subprocess.CompletedProcess(
            ["/test/scanner"], 0, self.secret, self.secret,
        )
        self.assertEqual(self.invoke(), {self.salt: self.secret})
        self.assertEqual(json.loads(self.output.read_text()), data)
        self.assertFalse(self.scanner_output.exists())
        self.assert_secret_absent()
        self.resign.assert_not_called()

    def test_timeout_does_not_publish_keys(self) -> None:
        self.run.side_effect = subprocess.TimeoutExpired("/test/scanner", 120)
        with self.assertRaisesRegex(RuntimeError, "120"):
            self.invoke()
        self.assertFalse(self.output.exists())
        self.resign.assert_not_called()

    def test_execute_permission_failure_does_not_publish_keys(self) -> None:
        self.run.side_effect = PermissionError("not executable")
        with self.assertRaisesRegex(RuntimeError, "无法执行"):
            self.invoke()
        self.assertFalse(self.output.exists())

    def test_missing_data_directory_does_not_start_scanner(self) -> None:
        self.db_dir = self.work_dir / "missing-parent" / "db_storage"
        with self.assertRaisesRegex(RuntimeError, "目录不存在"):
            self.invoke()
        self.run.assert_not_called()


class FindBinaryTests(unittest.TestCase):
    def test_unsupported_architecture_is_rejected(self) -> None:
        with patch.object(scanner_macos.platform, "machine", return_value="unknown"):
            with self.assertRaisesRegex(RuntimeError, "不支持"):
                scanner_macos._find_binary()

    def test_missing_binary_is_reported(self) -> None:
        with patch.object(scanner_macos.platform, "machine", return_value="arm64"):
            with patch.object(scanner_macos.os.path, "isfile", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "找不到"):
                    scanner_macos._find_binary()

    def test_arm64_and_intel_binary_names(self) -> None:
        for architecture in ("arm64", "x86_64"):
            with self.subTest(architecture=architecture):
                with patch.object(scanner_macos.platform, "machine", return_value=architecture):
                    with patch.object(scanner_macos.os.path, "isfile", return_value=True):
                        self.assertTrue(scanner_macos._find_binary().endswith(architecture))


if __name__ == "__main__":
    unittest.main()
