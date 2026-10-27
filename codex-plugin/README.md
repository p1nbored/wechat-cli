# WeChat CLI for Codex

This skills-only plugin makes the installed `wechat-cli` available to Codex
and adds **WeChat CLI** to the plugin list. It queries personal WeChat;
enterprise WeCom uses the separate `wecom-cli` plugin.

## Install on macOS

Use an existing working CLI installation when available:

```sh
wechat-cli --version
```

For a new installation from this checkout, use Python 3.10 or newer in an
isolated environment, then expose its `wechat-cli` executable on your PATH:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
export PATH="$PWD/.venv/bin:$PATH"
```

The environment must remain available to Codex after this terminal closes.
Preserve the selected checkout and environment when updating an existing local
installation. Do not replace a working installation just to install this plugin.

From the repository root, register and install the plugin (the installer itself
uses only the standard library and supports Python 3.9+):

```sh
python3 codex-plugin/install.py
codex plugin list --marketplace p1nbored-local --json
```

The `codex` command must be on PATH. The installer respects `CODEX_HOME`, or
accepts `--codex-home /path/to/codex-home`. It installs the skills package only;
it does not install the CLI or initialize the WeChat account.

Before querying, confirm both `~/.wechat-cli/config.json` and
`~/.wechat-cli/all_keys.json` exist without printing their contents. If they do
not exist, follow the repository's initialization instructions first.

If a query reports `Operation not permitted`, enable **Codex** (or the terminal
actually running the CLI) under **System Settings → Privacy & Security → Full
Disk Access**, then fully quit and reopen that application. This grants database
file access, not process memory access. The installer never re-signs WeChat or
changes system permissions. The repository's `docs/macos-initialization.md`
describes a separate manual capture/import workflow for supported macOS setups.

Verify an initialized account with `wechat-cli sessions --limit 1`. Successful
plugin installation alone does not prove that WeChat data is accessible.

## Install on Windows

Install the console tool from this fork if needed:

```powershell
uv tool install 'git+https://github.com/p1nbored/wechat-cli.git@main'
```

From the repository root, register and install the plugin:

```powershell
& ./codex-plugin/install.ps1
codex plugin list --marketplace p1nbored-local --json
```

The PowerShell installer respects `CODEX_HOME`; alternatively pass
`-CodexHome <directory>`.

## Updates and data handling

Both installers copy the manifests, skills, and README into the local
`p1nbored-local` marketplace, preserve its other plugin entries, and use
`codex plugin add` to install. Update `codex-plugin/` in the selected checkout,
then rerun the appropriate installer. Do not edit the versioned plugin cache.
Reopen the chat or restart Codex if available skills have not refreshed.

The installer does not initialize WeChat, read messages, or modify existing
WeChat configuration and keys. Log into desktop WeChat before first-time
`wechat-cli init`. Never include configuration, encryption keys, or chat data
in this package.

## Package layout

- `plugin.json`: portable Agent Plugins manifest.
- `.codex-plugin/plugin.json`: Codex compatibility manifest with skill discovery.
- `skills/wechat-cli/SKILL.md`: local CLI usage and data-handling instructions.
- `install.py`: portable standard-library installer, used on macOS.
- `install.ps1`: repeatable local marketplace registration and installation.
