#!/usr/bin/env python3
"""Register this skills-only plugin using Codex's local marketplace flow."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


MARKETPLACE = "p1nbored-local"
PAYLOAD = ("plugin.json", ".codex-plugin/plugin.json", "skills", "README.md")


def select_home(argument: Optional[str]) -> Path:
    """Honor the explicit home, then CODEX_HOME, then Codex's default."""
    selected = argument or os.environ.get("CODEX_HOME")
    return (Path(selected) if selected else Path.home() / ".codex").expanduser().resolve()


def load_marketplace(path: Path) -> dict:
    if not path.exists():
        return {"name": MARKETPLACE, "plugins": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("name") != MARKETPLACE:
        raise ValueError("Unexpected marketplace identity.")
    plugins = data.get("plugins")
    if not isinstance(plugins, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str)
        for item in plugins
    ):
        raise ValueError("Marketplace plugins must be a list of named entries.")
    return data


def reject_symlinks(path: Path) -> None:
    if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
        raise ValueError("Plugin package and destination must not contain symlinks.")


def write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def replace_skills(source: Path, target: Path) -> None:
    """Stage a complete skills tree so removed instructions cannot linger."""
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".skills-") as temporary:
        staged = Path(temporary) / "updated"
        previous = Path(temporary) / "previous"
        shutil.copytree(source, staged)
        had_previous = target.exists()
        if had_previous:
            os.replace(target, previous)
        try:
            os.replace(staged, target)
        except OSError:
            if had_previous:
                os.replace(previous, target)
            raise


def copy_payload(source: Path, destination: Path) -> None:
    for relative in PAYLOAD:
        item = source / relative
        if not item.exists():
            raise FileNotFoundError("Missing plugin package file: " + relative)
        for component in (item, *item.parents):
            if component == source.parent:
                break
            if component.is_symlink():
                raise ValueError("Plugin package paths must not contain symlinks.")
        reject_symlinks(item)
    for relative in PAYLOAD:
        item, target = source / relative, destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            replace_skills(item, target)
        else:
            shutil.copy2(item, target)


def install_plugin(codex_home: Path, source: Path) -> None:
    """Copy public package files and let Codex manage registration and caching."""
    codex = shutil.which("codex")
    if codex is None:
        raise FileNotFoundError("Codex CLI is not on PATH; install it before this plugin.")
    root = codex_home / "local-marketplaces" / MARKETPLACE
    manifest = root / ".agents/plugins/marketplace.json"
    destination = root / "plugins/wechat-cli"
    for path in (root, manifest.parent, destination):
        for component in (path, *path.parents):
            if component == codex_home:
                break
            if component.is_symlink():
                raise ValueError("Marketplace paths must not contain symlinks.")
    reject_symlinks(destination)
    if manifest.is_symlink():
        raise ValueError("Marketplace manifest must not be a symlink.")
    marketplace = load_marketplace(manifest)
    copy_payload(source, destination)
    entry = {
        "name": "wechat-cli",
        "source": {"source": "local", "path": "./plugins/wechat-cli"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
        "category": "Productivity",
    }
    plugins = [item for item in marketplace["plugins"] if item["name"] != "wechat-cli"]
    write_manifest(manifest, {**marketplace, "plugins": plugins + [entry]})
    environment = {**os.environ, "CODEX_HOME": str(codex_home)}
    subprocess.run([codex, "plugin", "marketplace", "add", str(root)],
                   env=environment, check=True)
    subprocess.run([codex, "plugin", "add", "wechat-cli@p1nbored-local", "--json"],
                   env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", help="Override CODEX_HOME (default: ~/.codex)")
    arguments = parser.parse_args()
    try:
        install_plugin(select_home(arguments.codex_home), Path(__file__).resolve().parent)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Plugin installation failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
