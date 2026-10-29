"""Locate downloaded file attachments within the current WeChat account."""

import hashlib
import os
import re
import sys
import unicodedata
from datetime import datetime


def _filename_rank(name, title):
    # macOS may store decomposed Unicode filenames. Preserve spaces in titles.
    name = unicodedata.normalize("NFC", name)
    title = unicodedata.normalize("NFC", title)
    if name == title:
        return 0
    stem, ext = os.path.splitext(title)
    pattern = re.escape(stem) + r" ?(?:\([0-9]+\)|（[0-9]+）)" + re.escape(ext)
    return 1 if re.fullmatch(pattern, name) else None


def _files_under(root):
    """Ignore missing/unreadable directories and never follow cache symlinks."""
    if os.path.islink(root):
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d)))
        for name in sorted(files):
            path = os.path.join(directory, name)
            if not os.path.islink(path) and os.path.isfile(path):
                yield path


def _matches_metadata(path, size, digest):
    try:
        if size is not None and os.path.getsize(path) != size:
            return False
        if digest:
            md5 = hashlib.md5()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    md5.update(chunk)
            if md5.hexdigest() != digest:
                return False
        return True
    except OSError:
        return False


def resolve_file_attachment(db_dir, appmsg, create_time_ts):
    """Find a file by name and optional size/MD5; abstain on ambiguous matches.

    Search all download months, then macOS account cache/temp trees. A valid
    message MD5 also permits hash-named cache files, but their bytes must match.
    No arbitrary global caches or paths supplied by message XML are traversed.
    """
    title = appmsg.findtext("title") or ""
    if not title or title in (".", "..") or any(c in title for c in ("/", "\\", "\x00")):
        return None
    digest = (appmsg.findtext("appattach/md5") or appmsg.findtext("md5") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", digest):
        digest = None
    raw_size = (appmsg.findtext("appattach/totallen") or "").strip()
    size = int(raw_size) if re.fullmatch(r"[0-9]{1,20}", raw_size) else None
    try:
        month = datetime.fromtimestamp(create_time_ts).strftime("%Y-%m") if create_time_ts else None
    except (ValueError, OverflowError, OSError, TypeError):
        month = None

    base = os.path.dirname(os.path.abspath(db_dir))
    roots = [os.path.join(base, "msg", "file")]
    if sys.platform == "darwin":
        roots.extend(os.path.join(base, part) for part in ("cache", "temp"))

    for root in roots:
        # Also reject symlinked intermediate directories such as msg/.
        if os.path.commonpath((os.path.realpath(base), os.path.realpath(root))) != os.path.realpath(base):
            continue
        candidates = []
        for path in _files_under(root):
            name = os.path.basename(path)
            rank = _filename_rank(name, title)
            if rank is None and digest:
                stem = os.path.splitext(name)[0].lower()
                if name.lower() == digest or stem == digest:
                    rank = 2
            if rank is None or not _matches_metadata(path, size, digest):
                continue
            parts = os.path.relpath(path, root).split(os.sep)[:-1]
            candidates.append(((0 if month in parts else 1, rank), path))
        if candidates:
            candidates.sort()
            best_rank, best_path = candidates[0]
            # A verified digest identifies equivalent copies. Without it, do
            # not select an arbitrary numbered duplicate or cross-month match.
            if digest or sum(rank == best_rank for rank, _ in candidates) == 1:
                return best_path
            return None
    return None
