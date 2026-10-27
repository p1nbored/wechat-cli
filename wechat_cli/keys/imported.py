"""Verify explicitly supplied SQLCipher 4 keys before private initialization."""

from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tempfile

from ..core.crypto import SQLITE_HDR, full_decrypt

PAGE_SIZE = 4096
KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


def _relative_database(name: object) -> PurePosixPath:
    if not isinstance(name, str) or any(character in name for character in ("\\", "\x00", ":")):
        raise RuntimeError("密钥文件包含无效数据库路径")
    path = PurePosixPath(name)
    if (path.is_absolute() or path.as_posix() != name
            or any(part in (".", "..") for part in path.parts)
            or path.suffix != ".db"):
        raise RuntimeError("密钥文件只接受规范的相对 .db 路径")
    return path


def _load_keys(capture: Path) -> dict[str, dict[str, str]]:
    if capture.is_symlink() or not capture.is_file():
        raise RuntimeError("密钥输入必须是普通文件")
    if capture.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError("密钥输入文件过大")
    try:
        with capture.open(encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, ValueError):
        raise RuntimeError("无法读取密钥 JSON 文件") from None
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, dict) or not keys:
        raise RuntimeError("密钥文件需要非空 keys 对象")
    normalized = {}
    for name, info in keys.items():
        _relative_database(name)
        key = info.get("enc_key") if isinstance(info, dict) else None
        if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
            raise RuntimeError("数据库密钥必须是 64 位十六进制字符串")
        normalized[name] = {"enc_key": key.lower()}
    return normalized


def _unreadable_directory(_error: OSError) -> None:
    raise RuntimeError("无法完整读取数据库目录；请检查读取权限") from None


def _database_pages(root: Path) -> dict[str, bytes]:
    pages = {}
    for directory, directories, files in os.walk(
        root, followlinks=False, onerror=_unreadable_directory,
    ):
        for name in directories + files:
            if (Path(directory) / name).is_symlink():
                raise RuntimeError("数据库目录中不允许符号链接")
        for name in files:
            path = Path(directory) / name
            if path.suffix != ".db":
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise RuntimeError("数据库必须是普通文件")
            with path.open("rb") as source:
                page = source.read(PAGE_SIZE)
            if page.startswith(SQLITE_HDR):
                continue
            if len(page) != PAGE_SIZE or path.stat().st_size % PAGE_SIZE:
                raise RuntimeError("加密数据库大小无效")
            pages[path.relative_to(root).as_posix()] = page
    if "session/session.db" not in pages:
        raise RuntimeError("缺少加密的 session/session.db 数据库")
    return pages


def _verify_page(key: bytes, page: bytes) -> bool:
    mac_salt = bytes(value ^ 0x3A for value in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, 32)
    actual = hmac.new(mac_key, page[16:4032] + b"\x01\x00\x00\x00", hashlib.sha512).digest()
    return hmac.compare_digest(actual, page[4032:4096])


def _verify_session(root: Path, key: bytes) -> None:
    # TemporaryDirectory is private; the decrypted database never becomes CLI state.
    with tempfile.TemporaryDirectory(prefix="wechat-key-check-") as directory:
        target = Path(directory) / "session.db"
        full_decrypt(str(root / "session/session.db"), str(target), key)
        os.chmod(target, 0o600)
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("会话数据库完整性检查失败")
            connection.execute("SELECT COUNT(*) FROM SessionTable").fetchone()


def _publish_json(path: Path, data: dict) -> os.stat_result:
    """Link a fully written private file into place without replacing any entry."""
    descriptor, temporary = tempfile.mkstemp(prefix=".import-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
            identity = os.fstat(output.fileno())
        os.link(temporary, path)
        return identity
    finally:
        os.unlink(temporary)


def _prepare_state(state: Path) -> None:
    if state.is_symlink():
        raise RuntimeError("状态目录不允许符号链接")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not state.is_dir() or stat.S_IMODE(state.stat().st_mode) != 0o700:
        raise RuntimeError("状态目录权限必须是 0700")
    if any(os.path.lexists(state / name) for name in ("all_keys.json", "config.json")):
        raise RuntimeError("已有初始化文件；密钥导入不会覆盖，请选择独立状态目录")


def import_keys(capture: Path, db_root: Path, state_dir: Path) -> int:
    """Import only a complete, HMAC-verified key set; never access process memory."""
    capture, root, state = Path(capture), Path(db_root), Path(state_dir)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("数据库根目录必须是实际目录")
    root = root.resolve()
    keys = _load_keys(capture)
    pages = _database_pages(root)
    if set(keys) != set(pages):
        raise RuntimeError("密钥集合必须完整匹配所选目录中的所有加密数据库")
    for name, page in pages.items():
        if not _verify_page(bytes.fromhex(keys[name]["enc_key"]), page):
            raise RuntimeError("数据库密钥 HMAC-SHA512 验证失败")
    try:
        _verify_session(root, bytes.fromhex(keys["session/session.db"]["enc_key"]))
    except (OSError, ValueError, sqlite3.Error):
        raise RuntimeError("会话数据库解密或结构验证失败") from None
    _prepare_state(state)
    key_path = state / "all_keys.json"
    identity = None
    try:
        identity = _publish_json(key_path, keys)
        _publish_json(state / "config.json", {"db_dir": str(root)})
    except OSError:
        if identity is not None:
            current = key_path.lstat() if os.path.lexists(key_path) else None
            if current and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                key_path.unlink()
        raise RuntimeError("无法独占创建初始化文件；原有文件不会被覆盖") from None
    return len(keys)
