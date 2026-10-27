"""Import validation uses synthetic SQLCipher pages, never live WeChat data."""

from contextlib import closing
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from Crypto.Cipher import AES

from wechat_cli.commands.init import init
from wechat_cli.keys import imported


def encrypted_session(path: Path, key: bytes) -> None:
    """Move SQLite leaf cells to reserve 80 bytes, then encrypt the pages."""
    plain = path.with_suffix(".plain")
    with closing(sqlite3.connect(plain)) as connection:
        with connection:
            connection.execute("CREATE TABLE SessionTable (name TEXT)")
            connection.execute("INSERT INTO SessionTable VALUES ('synthetic')")
    source = bytearray(plain.read_bytes())
    source[20] = 80
    pages = []
    salt = bytes(range(16))
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(x ^ 0x3a for x in salt), 2, 32)
    for number in range(len(source) // 4096):
        page = bytearray(source[number * 4096:(number + 1) * 4096])
        header = 100 if number == 0 else 0
        count = int.from_bytes(page[header + 3:header + 5], "big")
        start = int.from_bytes(page[header + 5:header + 7], "big")
        if count:
            page[start - 80:4016] = page[start:4096]
            page[header + 5:header + 7] = (start - 80).to_bytes(2, "big")
            for index in range(count):
                offset = header + 8 + 2 * index
                value = int.from_bytes(page[offset:offset + 2], "big")
                page[offset:offset + 2] = (value - 80).to_bytes(2, "big")
        else:
            page[header + 5:header + 7] = (4016).to_bytes(2, "big")
        iv = bytes([number + 1]) * 16
        begin = 16 if number == 0 else 0
        encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(page[begin:4016]))
        body = (salt if number == 0 else b"") + encrypted + iv
        tag = hmac.new(mac_key, body[begin:] + (number + 1).to_bytes(4, "little"), hashlib.sha512).digest()
        pages.append(body + tag)
    path.write_bytes(b"".join(pages))
    plain.unlink()


class ImportKeysTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "db_storage"
        (self.root / "session").mkdir(parents=True)
        self.key = bytes(range(32))
        self.session = self.root / "session/session.db"
        encrypted_session(self.session, self.key)
        self.capture = self.base / "capture.json"
        self.state = self.base / "state"
        self.payload = {"keys": {"session/session.db": {"enc_key": self.key.hex()}}}
        self.write_capture()

    def write_capture(self) -> None:
        self.capture.write_text(json.dumps(self.payload), encoding="utf-8")

    def invoke(self) -> int:
        return imported.import_keys(self.capture, self.root, self.state)

    def assert_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertFalse((self.state / "all_keys.json").exists())
        self.assertFalse((self.state / "config.json").exists())

    def test_verifies_and_publishes_private_state(self) -> None:
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for name in ("all_keys.json", "config.json"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.state / "config.json").read_text())["db_dir"], str(self.root.resolve()))
        self.assertEqual(json.loads((self.state / "all_keys.json").read_text()), self.payload["keys"])

    def test_session_connection_is_closed_after_validation(self) -> None:
        connections = []
        original_connect = sqlite3.connect

        def remember_connection(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection

        with patch.object(imported.sqlite3, "connect", side_effect=remember_connection):
            imported._verify_session(self.root, self.key)

        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_invalid_key_rejected(self) -> None:
        self.payload["keys"]["session/session.db"]["enc_key"] = "x" * 64
        self.write_capture()
        self.assert_rejected()

    def test_wrong_key_rejected(self) -> None:
        self.payload["keys"]["session/session.db"]["enc_key"] = "ff" * 32
        self.write_capture()
        self.assert_rejected()

    def test_all_encrypted_databases_must_be_covered(self) -> None:
        (self.root / "contact").mkdir()
        encrypted_session(self.root / "contact/contact.db", self.key)
        self.assert_rejected()

    def test_all_captured_keys_must_match_database(self) -> None:
        self.payload["keys"]["missing.db"] = {"enc_key": self.key.hex()}
        self.write_capture()
        self.assert_rejected()

    def test_required_session_database(self) -> None:
        self.session.rename(self.root / "other.db")
        self.payload = {"keys": {"other.db": {"enc_key": self.key.hex()}}}
        self.write_capture()
        self.assert_rejected()

    def test_rejects_path_escape_and_noncanonical_names(self) -> None:
        for name in ("../outside.db", "/outside.db", "C:/outside.db", "session/../session/session.db", "session//session.db", "session\\session.db", "session/session.txt"):
            with self.subTest(name=name):
                self.payload = {"keys": {name: {"enc_key": self.key.hex()}}}
                self.write_capture()
                self.assert_rejected()

    def test_rejects_database_symlink(self) -> None:
        original = self.base / "original.db"
        self.session.rename(original)
        self.session.symlink_to(original)
        self.assert_rejected()

    def test_rejects_directory_symlink(self) -> None:
        (self.root / "alias").symlink_to(self.root / "session", target_is_directory=True)
        self.assert_rejected()

    def test_unreadable_database_subdirectory_fails_closed(self) -> None:
        def unreadable_walk(root, *, followlinks, onerror=None):
            if onerror is not None:
                onerror(PermissionError("sensitive path must not appear"))
            return [(str(root), [], [])]
        with patch.object(imported.os, "walk", side_effect=unreadable_walk):
            with self.assertRaisesRegex(RuntimeError, "无法完整读取") as caught:
                self.invoke()
        self.assertNotIn("sensitive path", str(caught.exception))
        self.assertFalse(self.state.exists())

    def test_rejects_symlinked_state_directory(self) -> None:
        destination = self.base / "outside"
        destination.mkdir()
        self.state.symlink_to(destination, target_is_directory=True)
        self.assert_rejected()

    def test_refuses_existing_config_or_keys(self) -> None:
        self.state.mkdir(mode=0o700)
        for name in ("all_keys.json", "config.json"):
            with self.subTest(name=name):
                existing = self.state / name
                existing.write_text("keep")
                with self.assertRaises(RuntimeError):
                    self.invoke()
                self.assertEqual(existing.read_text(), "keep")
                existing.unlink()

    def test_rolls_back_only_created_keys_if_config_publication_fails(self) -> None:
        original = imported._publish_json
        def fail_config(path: Path, data: dict):
            if path.name == "config.json":
                raise OSError("synthetic write failure")
            return original(path, data)
        with patch.object(imported, "_publish_json", side_effect=fail_config):
            self.assert_rejected()

    def test_invalid_payloads_are_rejected_without_echoing_values(self) -> None:
        for payload in ([], {}, {"keys": []}, {"keys": {}}, {"keys": {"session/session.db": None}}):
            with self.subTest(payload=payload):
                self.payload = payload
                self.write_capture()
                self.assert_rejected()

    def test_malformed_json_is_rejected(self) -> None:
        self.capture.write_text("not json")
        self.assert_rejected()

    def test_valid_hmac_but_invalid_sqlite_is_rejected(self) -> None:
        page = bytearray(self.session.read_bytes())
        page[100] ^= 1
        mac_key = hashlib.pbkdf2_hmac("sha512", self.key, bytes(x ^ 0x3a for x in page[:16]), 2, 32)
        page[4032:4096] = hmac.new(mac_key, page[16:4032] + b"\x01\x00\x00\x00", hashlib.sha512).digest()
        self.session.write_bytes(page)
        self.assert_rejected()

    def test_cli_import_does_not_call_memory_scanner(self) -> None:
        with patch("wechat_cli.commands.init.STATE_DIR", str(self.state)):
            with patch("wechat_cli.keys.extract_keys") as scanner:
                result = CliRunner().invoke(init, ["--db-dir", str(self.root), "--import-keys", str(self.capture)])
        self.assertEqual(result.exit_code, 0, result.output)
        scanner.assert_not_called()
        self.assertNotIn(self.key.hex(), result.output)

    def test_cli_refuses_force_with_import(self) -> None:
        result = CliRunner().invoke(init, ["--force", "--import-keys", str(self.capture)])
        self.assertNotEqual(result.exit_code, 0)

    def test_cli_import_requires_selected_database(self) -> None:
        with patch("wechat_cli.commands.init.auto_detect_db_dir", return_value=None):
            result = CliRunner().invoke(init, ["--import-keys", str(self.capture)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--db-dir", result.output)

    def test_cli_import_failure_does_not_echo_key(self) -> None:
        self.payload["keys"]["session/session.db"]["enc_key"] = "ff" * 32
        self.write_capture()
        result = CliRunner().invoke(init, ["--db-dir", str(self.root), "--import-keys", str(self.capture)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn("ff" * 32, result.output)

    def invoke_default(self, arguments: list, *, detected=None,
                       failure: Exception | None = None):
        with patch("wechat_cli.commands.init.STATE_DIR", str(self.state)), patch("wechat_cli.commands.init.CONFIG_FILE", str(self.state / "config.json")), patch("wechat_cli.commands.init.KEYS_FILE", str(self.state / "all_keys.json")), patch("wechat_cli.commands.init.auto_detect_db_dir", return_value=detected), patch("wechat_cli.keys.extract_keys", return_value={"salt": "key"}, side_effect=failure):
            return CliRunner().invoke(init, arguments)

    def test_existing_default_initialization_is_not_overwritten(self) -> None:
        self.state.mkdir()
        (self.state / "config.json").write_text("original")
        (self.state / "all_keys.json").write_text("original")
        result = self.invoke_default([])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual((self.state / "config.json").read_text(), "original")

    def test_default_initialization_autodetect_and_explicit_paths(self) -> None:
        for arguments in ([], ["--db-dir", str(self.root), "--force"]):
            with self.subTest(arguments=arguments):
                result = self.invoke_default(arguments, detected=str(self.root))
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertTrue((self.state / "config.json").exists())

    def test_default_initialization_requires_database_path(self) -> None:
        result = self.invoke_default([])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--db-dir", result.output)
        result = self.invoke_default(["--db-dir", str(self.base / "missing")])
        self.assertNotEqual(result.exit_code, 0)

    def test_default_scanner_errors_do_not_publish_config(self) -> None:
        for failure in (RuntimeError("synthetic failure"), RuntimeError("sudo denied"), ValueError("synthetic error")):
            with self.subTest(failure=failure):
                result = self.invoke_default(["--db-dir", str(self.root)], failure=failure)
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse((self.state / "config.json").exists())


class CaptureGuardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).parents[1] / "scripts/macos/capture_keys.py"
        spec = importlib.util.spec_from_file_location("capture_keys", path)
        cls.capture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.capture)

    def test_rejects_installed_applications_before_launch(self) -> None:
        for path in (Path("/Applications/WeChat.app/Contents/MacOS/WeChat"), Path.home() / "Applications/WeChat.app/Contents/MacOS/WeChat"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.capture.validate_executable(path)

    def test_output_publication_is_exclusive_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.json"
            self.capture.private_json(path, {"keys": {}})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                self.capture.private_json(path, {"keys": {"changed": {}}})
            self.assertEqual(json.loads(path.read_text()), {"keys": {}})

    def test_invalid_sizes_and_hmac_do_not_verify(self) -> None:
        self.assertFalse(self.capture.verified(b"short", b"short"))
        self.assertFalse(self.capture.verified(b"x" * 32, b"y" * 4096))

    def test_missing_executable_and_public_output_directory_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self.capture.validate_executable(Path(directory) / "missing")
            os.chmod(directory, 0o755)
            with self.assertRaises(RuntimeError):
                self.capture.private_json(Path(directory) / "keys.json", {})

    def test_symlinked_output_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "link").symlink_to(base, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                self.capture.private_json(base / "link/keys.json", {})

    def run_fake_capture(self, *, launch_failure: bool = False,
                         no_key: bool = False, intel: bool = False,
                         kill_failure: bool = False) -> tuple[int, MagicMock, Path, tempfile.TemporaryDirectory]:
        """Exercise LLDB interaction against a fake debugger and synthetic DB."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        exe = base / "WeChat"
        exe.write_text("synthetic executable, never executed")
        root = base / "db_storage"
        (root / "session").mkdir(parents=True)
        key = bytes(range(32))
        encrypted_session(root / "session/session.db", key)
        out = base / "keys.json"
        error = MagicMock()
        error.Fail.return_value = launch_failure
        error.Success.return_value = True
        debugger, target, process, breakpoint = (MagicMock() for _ in range(4))
        debugger.CreateTarget.return_value = target
        target.Launch.return_value = process
        target.BreakpointCreateByName.return_value = breakpoint
        target.GetTriple.return_value = "x86_64-apple-macos" if intel else "arm64-apple-macos"
        breakpoint.GetID.return_value = 1
        breakpoint.IsValid.return_value = True
        target.IsValid.return_value = True
        process.IsValid.return_value = True
        process.GetState.side_effect = [5, 5 if kill_failure else 10]
        process.Kill.return_value.Success.return_value = not kill_failure
        process.Continue.return_value.Fail.return_value = False
        thread = MagicMock()
        thread.GetStopReason.return_value = 3
        thread.GetStopReasonDataCount.return_value = 2
        thread.GetStopReasonDataAtIndex.return_value = 1
        registers = dict(zip(("x1", "x2", "x3", "x4", "x5", "x6"), (100, 32, 200, 16, 5, 2)))
        thread.GetFrameAtIndex.return_value.FindRegister.side_effect = lambda name: SimpleNamespace(GetValueAsUnsigned=lambda: registers[name])
        process.__iter__.return_value = [thread]
        salt = bytes(range(16))
        process.ReadMemory.side_effect = lambda address, size, err: (bytes(value ^ 0x3a for value in salt) if address == 200 else key)
        listener = debugger.GetListener.return_value
        listener.WaitForEvent.return_value = True
        states = [10] if no_key else [5]
        lldb = SimpleNamespace(
            SBDebugger=SimpleNamespace(Create=lambda: debugger, Destroy=MagicMock()),
            SBLaunchInfo=MagicMock(), SBError=lambda: error, SBEvent=MagicMock(),
            SBProcess=SimpleNamespace(EventIsProcessEvent=lambda event: True,
                                      GetStateFromEvent=lambda event: states.pop(0)),
            eStateExited=10, eStateCrashed=11, eStateDetached=12,
            eStateStopped=5, eStopReasonBreakpoint=3, eStateInvalid=0,
        )
        arguments = ["capture_keys.py", "--exe", str(exe), "--db-root", str(root), "--out", str(out)]
        events = []
        process.capture_events = events
        with patch.dict(sys.modules, {"lldb": lldb}), patch.object(sys, "argv", arguments), patch.object(self.capture, "emit", side_effect=lambda event, **kwargs: events.append(event)):
            previous_umask = os.umask(0o077)
            try:
                if intel or kill_failure:
                    with self.assertRaisesRegex(RuntimeError, "arm64" if intel else "could not close"):
                        self.capture.main()
                    result = -1
                else:
                    result = self.capture.main()
            finally:
                os.umask(previous_umask)
        return result, process, out, temporary

    def test_complete_capture_uses_only_owned_process_and_publishes_once(self) -> None:
        result, process, out, _ = self.run_fake_capture()
        self.assertEqual(result, 0)
        self.assertTrue(out.exists())
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        process.Kill.assert_called_once()

    def test_launch_failure_never_publishes(self) -> None:
        result, _, out, _ = self.run_fake_capture(launch_failure=True)
        self.assertEqual(result, 2)
        self.assertFalse(out.exists())

    def test_process_exit_before_capture_never_publishes(self) -> None:
        result, process, out, _ = self.run_fake_capture(no_key=True)
        self.assertEqual(result, 3)
        self.assertFalse(out.exists())
        process.Kill.assert_called_once()

    def test_unvalidated_intel_capture_is_rejected(self) -> None:
        _, process, out, _ = self.run_fake_capture(intel=True)
        process.Kill.assert_called_once()
        self.assertFalse(out.exists())

    def test_failed_owned_process_cleanup_is_not_reported_as_closed(self) -> None:
        result, process, _, _ = self.run_fake_capture(kill_failure=True)
        self.assertEqual(result, -1)
        self.assertIn("owned_process_cleanup_failed", process.capture_events)
        self.assertNotIn("owned_process_closed", process.capture_events)

    def test_capture_preflight_fails_closed_on_unreadable_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            executable = base / "WeChat"
            executable.write_text("synthetic, never executed")
            root = base / "db_storage"
            root.mkdir()
            def unreadable_walk(path, *, followlinks, onerror=None):
                if onerror is not None:
                    onerror(PermissionError("private path"))
                return [(str(path), [], [])]
            arguments = ["capture_keys.py", "--exe", str(executable), "--db-root", str(root), "--out", str(base / "keys.json")]
            with patch.object(self.capture.os, "walk", side_effect=unreadable_walk), patch.object(sys, "argv", arguments):
                with self.assertRaisesRegex(RuntimeError, "cannot completely read") as caught:
                    self.capture.main()
            self.assertNotIn("private path", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
