# macOS 初始化：临时副本捕获和验证导入

已实测：Apple Silicon、微信 4.2.0（270345），19/19 个加密数据库的首页
HMAC-SHA512 验证通过，三个核心数据库可以由 SQLite 读取，CLI 查询通过。
这些结果不代表 Intel、其他微信版本或其他 SQLCipher 配置已经验证。

官方签名微信可能拒绝进程内存读取，即使使用管理员身份也一样。`init` 不会
自动重新签名微信。请勿对 `/Applications/WeChat.app` 重新签名；保留应用组等
受限权限再使用临时签名，可能使微信无法启动。

此流程只适用于你自己的本地账号。步骤会暂时关闭微信并在独立临时副本中登录。
应用签名与完整磁盘访问权限需要本人审阅、手动处理；脚本不修改权限、不请求
管理员凭据、不修改官方应用，也不连接任意已运行进程。

## 1. 准备

安装 Xcode Command Line Tools（`xcode-select --install`），并确认 `xcrun lldb -P`
能够输出 LLDB Python 模块路径。捕获脚本只需系统 Python 3.9+、LLDB 和标准库；
导入功能使用已安装的 `wechat-cli` 依赖。

找到本账号的 `db_storage` 路径。通常在：

```text
~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<本人的账号目录>/db_storage
```

不要通过账号名称猜选其他人的目录。完全退出官方微信，再在仓库根目录运行：

```sh
wechat_capture_dir="$(mktemp -d "${TMPDIR:-/tmp}/wechat-capture.XXXXXX")"
ditto /Applications/WeChat.app "$wechat_capture_dir/WeChat.app"
```

这里创建的是用完即删除的调试副本。官方应用和账号数据保留原位。记录上面的临时
目录；后续只操作这个副本。

## 2. 手动准备临时副本

仅对临时副本的顶层应用签名，不使用 `--deep`：

```sh
codesign --force --sign - --options 0 "$wechat_capture_dir/WeChat.app"
codesign --verify --strict "$wechat_capture_dir/WeChat.app"
codesign -d --entitlements :- "$wechat_capture_dir/WeChat.app"
```

最后一个命令应显示没有 entitlements，或显示空的权限字典。若仍有应用组、沙盒、
团队标识等权限，停止，不启动副本，也不要修改官方应用。

打开系统设置 → 隐私与安全性 → 完整磁盘访问权限，手动添加并启用这个**临时副本**。
副本移除沙盒后需要这项权限，才能访问原账号的数据目录。用于导入的终端也需有
读取该目录的权限。权限选择和系统身份验证由本人完成；不修改 TCC 数据库。

## 3. 捕获

在同一个终端中，将下面路径替换成已确认的本账号目录：

```sh
wechat_db_root="/本账号的完整路径/db_storage"
PYTHONPATH="$(xcrun lldb -P)" /usr/bin/python3 scripts/macos/capture_keys.py \
  --exe "$wechat_capture_dir/WeChat.app/Contents/MacOS/WeChat" \
  --db-root "$wechat_db_root" \
  --out "$wechat_capture_dir/captured-keys.json" \
  --timeout 300
```

脚本启动指定的副本。需要时本人在微信窗口登录并在手机确认；不要把密码、二维码、
密钥或聊天内容交给脚本输出或上传。出现系统身份验证时，由本人在系统窗口处理。

脚本仅捕获与所选数据库盐值匹配的 PBKDF 参数，并验证密钥。输出只有数量和状态。
全部数据库验证通过时才独占创建 0600 的密钥文件；不覆盖已有输出。超时或部分
成功不会发布密钥文件。脚本结束只关闭它自己启动的进程，不按名称结束其他微信。

捕获器会拒绝 `/Applications` 和 `~/Applications` 中的可执行文件。不要绕过这个
检查或把脚本变成“复制、改权限、签名、提权”自动执行链。

## 4. 验证导入

```sh
wechat-cli init --db-dir "$wechat_db_root" \
  --import-keys "$wechat_capture_dir/captured-keys.json"
```

导入会验证所有已捕获密钥的 SQLCipher 4 首页 HMAC-SHA512，要求密钥完整覆盖所选
目录的加密 `.db` 文件，并私下解密 `session/session.db`，通过 SQLite `quick_check`
和 `SessionTable` 查询。它不读取进程内存，也不显示密钥或查询内容。

验证通过后，创建权限 0700 的 `~/.wechat-cli` 状态目录和权限 0600 的配置、密钥文件。
已有任一初始化文件时拒绝导入；不支持用 `--force` 覆盖。如果配置发布失败，只回滚
本次创建的密钥文件。请保留原有状态，先人工确认账号和路径，不要盲目删除配置。

输入格式是 `{"keys": {"session/session.db": {"enc_key": "64 位十六进制密钥"}, ...}}`。
路径必须是规范的相对 `.db` 路径，不接受绝对路径、`..` 或符号链接。
数据库正在变化时验证可能失败；先正常退出副本并确认路径，再重新捕获。

## 5. 恢复日常使用并清理

确认副本已退出。在系统设置中移除刚添加的临时副本完整磁盘访问权限，然后从
`/Applications/WeChat.app` 打开官方微信并正常登录。官方应用在此流程中不被修改。
确认 CLI 查询可用后，在 Finder 中删除记录的临时目录（包括副本和捕获文件）。
不要把密钥文件、账号目录、数据库或聊天内容提交到 GitHub。

捕获逻辑改编自 acmerfight 和贡献者的 MIT 工具：
[固定版本源码](https://gist.githubusercontent.com/acmerfight/0a01249ef72970d07a2603dbb629e80f/raw/021410a10b1a5d4139b18608eb05f0d7de2b5e36/capture_keys.py)。
完整第三方许可保存在 [scripts/macos/LICENSE](../scripts/macos/LICENSE)。
