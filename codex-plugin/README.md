# WeChat CLI for Codex

This skills-only plugin makes the installed `wechat-cli` available to Codex
and adds **WeChat CLI** to the plugin list. It queries personal WeChat;
enterprise WeCom uses the separate `wecom-cli` plugin.

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

The installer copies this package into the local `p1nbored-local` marketplace,
preserves its other plugin entries, and uses `codex plugin add` to install it.
It respects `CODEX_HOME`; alternatively pass `-CodexHome <directory>`.
Run the installer again after updating the package. Reopen the chat or restart
Codex if the plugin list or available skills have not refreshed.

The installer does not initialize WeChat, read messages, or modify existing
WeChat configuration and keys. Log into desktop WeChat before first-time
`wechat-cli init`. Never include configuration, encryption keys, or chat data
in this package.

## Package layout

- `plugin.json`: portable Agent Plugins manifest.
- `.codex-plugin/plugin.json`: Codex compatibility manifest with skill discovery.
- `skills/wechat-cli/SKILL.md`: local CLI usage and data-handling instructions.
- `install.ps1`: repeatable local marketplace registration and installation.
