"""Attachment lookup regressions using synthetic account trees only."""

import hashlib
import tempfile
import unicodedata
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wechat_cli.core import attachments, messages


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / "db_storage"
        self.db.mkdir()
        self.ts = datetime(2026, 9, 15).timestamp()
        self.platform = patch.object(attachments.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def file(self, relative, data=b"attachment"):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def appmsg(self, title="report.pdf", size=None, digest=None):
        node = ET.Element("appmsg")
        ET.SubElement(node, "type").text = "6"
        ET.SubElement(node, "title").text = title
        attach = ET.SubElement(node, "appattach")
        if size is not None:
            ET.SubElement(attach, "totallen").text = str(size)
        if digest is not None:
            ET.SubElement(attach, "md5").text = digest
        return node

    def resolve(self, **kwargs):
        return attachments.resolve_file_attachment(str(self.db), self.appmsg(**kwargs), self.ts)

    def test_exact_same_month(self):
        path = self.file("msg/file/2026-09/report.pdf")
        self.assertEqual(self.resolve(), path)

    def test_cross_month_in_both_directions(self):
        for month in ("2026-08", "2026-10"):
            with self.subTest(month=month):
                path = self.file(f"msg/file/{month}/report.pdf")
                self.assertEqual(self.resolve(), path)
                Path(path).unlink()

    def test_numbered_duplicates_in_other_month(self):
        for name in ("report(1).pdf", "report (2).pdf", "report（3）.pdf"):
            with self.subTest(name=name):
                path = self.file(f"msg/file/2026-10/{name}")
                self.assertEqual(self.resolve(), path)
                Path(path).unlink()

    def test_same_month_and_exact_name_preference(self):
        self.file("msg/file/2026-10/report.pdf")
        self.file("msg/file/2026-09/report(1).pdf")
        preferred = self.file("msg/file/2026-09/report.pdf")
        self.assertEqual(self.resolve(), preferred)

    def test_ambiguous_duplicates_are_not_guessed(self):
        self.file("msg/file/2026-10/report(1).pdf")
        self.file("msg/file/2026-10/report(2).pdf")
        self.assertIsNone(self.resolve())

    def test_ambiguous_months_are_not_guessed(self):
        self.file("msg/file/2026-08/report.pdf")
        self.file("msg/file/2026-10/report.pdf")
        self.assertIsNone(self.resolve())

    def test_size_selects_duplicate_over_wrong_exact_name(self):
        self.file("msg/file/2026-09/report.pdf", b"wrong")
        correct = self.file("msg/file/2026-09/report(1).pdf", b"correct")
        self.assertEqual(self.resolve(size=7), correct)
        self.assertIsNone(self.resolve(size=999))

    def test_md5_disambiguates_same_size_duplicates(self):
        self.file("msg/file/2026-09/report.pdf", b"wrong")
        correct = self.file("msg/file/2026-10/report(1).pdf", b"right")
        self.assertEqual(self.resolve(size=5, digest=hashlib.md5(b"right").hexdigest()), correct)

    def test_mac_cache_and_temp_without_msg_directory(self):
        for root in ("cache", "temp"):
            with self.subTest(root=root):
                path = self.file(f"{root}/nested/2026-10/Files/report (1).pdf")
                self.assertEqual(self.resolve(), path)
                Path(path).unlink()

    def test_hash_named_cache_requires_verified_digest(self):
        digest = hashlib.md5(b"right").hexdigest()
        wrong = self.file(f"cache/nested/{digest}", b"wrong")
        self.assertIsNone(self.resolve(digest=digest))
        Path(wrong).unlink()
        for name in (digest, digest + ".pdf"):
            path = self.file(f"temp/nested/{name}", b"right")
            self.assertEqual(self.resolve(digest=digest.upper()), path)
            self.assertIsNone(self.resolve())
            Path(path).unlink()

    def test_non_mac_downloads_still_support_cross_month_duplicates(self):
        self.file("cache/report.pdf")
        with patch.object(attachments.sys, "platform", "win32"):
            self.assertIsNone(self.resolve())
            path = self.file("msg/file/2026-10/report(1).pdf")
            self.assertEqual(self.resolve(), path)

    def test_spaces_and_unicode_are_preserved_for_lookup(self):
        title = "résumé  final.pdf"
        path = self.file("msg/file/2026-10/" + unicodedata.normalize("NFD", title))
        self.assertEqual(self.resolve(title=title), path)

    def test_substrings_directories_and_partial_downloads_do_not_match(self):
        for name in ("report.pdf.tmp", "report.pdf.backup", "old-report.pdf", "report.pd"):
            self.file("msg/file/2026-09/" + name)
        (self.base / "msg/file/2026-09/report.pdf").mkdir()
        self.assertIsNone(self.resolve())

    def test_message_paths_and_symlinks_do_not_escape_account(self):
        outside = self.file("outside/report.pdf")
        folder = self.base / "msg/file/2026-09"
        folder.mkdir(parents=True)
        (folder / "report.pdf").symlink_to(outside)
        (self.base / "cache").symlink_to(self.base / "outside", target_is_directory=True)
        self.assertIsNone(self.resolve())
        for title in ("../outside/report.pdf", outside, "..\\outside\\report.pdf"):
            self.assertIsNone(self.resolve(title=title))

    def test_unreadable_candidate_does_not_hide_other_match(self):
        self.file("msg/file/2026-09/report.pdf")
        correct = self.file("cache/report.pdf")
        real_size = attachments.os.path.getsize
        def getsize(path):
            if "/msg/" in path:
                raise PermissionError("unreadable")
            return real_size(path)
        with patch.object(attachments.os.path, "getsize", side_effect=getsize):
            self.assertEqual(self.resolve(size=10), correct)

    def test_unknown_timestamp_still_searches_downloads(self):
        correct = self.file("msg/file/2026-10/report.pdf")
        for timestamp in (0, None, 10**30):
            self.assertEqual(attachments.resolve_file_attachment(str(self.db), self.appmsg(), timestamp), correct)

    def test_formatted_history_and_media_api_use_same_lookup(self):
        path = self.file("temp/nested/report(1).pdf")
        xml = "<msg>" + ET.tostring(self.appmsg(), encoding="unicode") + "</msg>"
        self.assertEqual(messages._resolve_media_path(str(self.db), xml, 49, self.ts), (path, True))
        _, text = messages._format_message_text(
            1, 49, xml, False, "test", "Test", {}, lambda *args: "Test",
            db_dir=str(self.db), create_time_ts=self.ts, resolve_media=True,
        )
        self.assertEqual(text, "[文件] report.pdf\n  " + path)
        _, text = messages._format_message_text(
            1, 49, xml, False, "test", "Test", {}, lambda *args: "Test",
            db_dir=str(self.db), create_time_ts=self.ts, resolve_media=False,
        )
        self.assertEqual(text, "[文件] report.pdf")


if __name__ == "__main__":
    unittest.main()
