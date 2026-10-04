# Codex Cross-Provider Bridge

本地 Codex Responses 兼容桥，用于处理 CC Switch 切换 provider 后的历史别名、请求状态和压缩兼容问题。

当前版本：**0.3.0**。这是社区工具，验证范围和已知限制见[版本记录](CHANGELOG.md)与[验证说明](docs/validation.zh-CN.md)。

## 当前实现

- 为 `custom`、`cc-switch-official` 历史 provider 保留可解析的配置。
- 只修改发出的请求副本，保留消息正文和工具调用的 `call_id` 配对。
- 官方 Codex 使用专用 `/__codex_official__` 入口；第三方使用 `/v1`，经 CC Switch 转换协议后发送上游。
- 原生 OpenAI 状态通常保留；识别到外来状态或窄范围的 item/encrypted-content 错误时再兼容处理。
- `SessionStart`、`UserPromptSubmit`、`PreCompact` 可检查和维护路由。推荐策略不迁移历史、不自动创建分支、不发送探测请求。
- 自动配置维护线程运行在同一个 Bridge 进程中，不需要额外守护服务。
- 压缩请求使用独立的 600 秒空闲超时，普通流使用 120 秒；上游响应头默认等待 600 秒。
- 浏览器请求不进入 Codex 修复策略；带浏览器来源特征的请求不能进入官方兼容入口。
- Claude SSE 修复已停用，`/v1/messages` 原样转发。

```text
                     /__codex_official__ -> ChatGPT Codex 官方端点
Codex -> 本地 Bridge
                     /v1 -> CC Switch 本地路由 -> 当前第三方 provider
```

官方 provider 的 **CC Switch 上游模板** 保持原生 `model_provider = "openai"`；Bridge 地址只投影到 **Codex 客户端配置**。把 Bridge 地址写成官方上游模板会改变 CC Switch 的鉴权判断。

## 要求

- Python **3.11+**（使用标准库 `tomllib`）。
- `python -m pip install -r requirements.txt`。
- Windows 管理脚本需要 PowerShell **7+**；前台 Python 服务与 Python 测试可在其他平台运行。
- 已配置并登录 Codex；第三方模式需要已运行的 CC Switch 本地路由。
- 推荐让 `CODEX_EXECUTABLE` 指向与桌面应用一致版本的 CLI。否则从 PATH 查找 `codex`，不猜测安装目录或版本哈希。

## 前台启动

在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force state | Out-Null
Copy-Item config/lifecycle-policy.example.json state/lifecycle-policy.json
python src/codex_cross_provider_bridge.py
```

`state/lifecycle-policy.json` 是本机配置；已有文件时先比较和备份，不直接覆盖。示例明确开启请求兼容和自动路由维护，关闭自动探测和自动分支。

默认监听 `127.0.0.1:15722`，默认上游 `http://127.0.0.1:15721`。这些是可替换的默认值，不依赖任何特定用户名、安装目录或 provider ID。

启动后，在另一个 PowerShell 窗口安装生命周期 hooks：

```powershell
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action enable-compact-hook
```

如需为 CC Switch 的持久 provider 模板补充历史别名，先预览，再应用：

```powershell
python src/codex_ccswitch_template_guard.py
python src/codex_ccswitch_template_guard.py --apply
```

模板改动会先备份 SQLite 数据库。官方模板保持原生路由，第三方模板保留真实上游地址。完成配置后重新打开或重启 Codex，让客户端加载配置。

Windows 可双击仓库内的 `Restart-CodexBridge.cmd`，它默认开启可见前台窗口。关闭该窗口会停止服务；不需要查看日志时可以最小化。它根据自身目录寻找脚本，不含桌面绝对路径。

## Windows 登录时启动

```powershell
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action install -EnableCompactHook
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action status
```

这是可选的计划任务运行方式，使用当前用户权限。与前台方式选择其一即可。普通 `start` / `restart` 不会应用默认路由档案；`restore-route` 才是显式恢复路由的操作。

重启会检查监听端口的进程归属和正在进行的请求。先让请求完成再重启；不要在正在输出或压缩时停止服务。

## 自定义环境

Python CLI 参数优先于环境变量；显式 PowerShell 参数同样可以覆盖默认值。Python 只展开标准用户主目录，仓库内资源通过脚本自身目录定位。

| 设置 | 用途 |
| --- | --- |
| `CODEX_HOME` / `--codex-home` | Codex 数据目录，默认用户主目录下的 `.codex` |
| `--config` / `-ConfigPath` | 客户端配置文件；单独传入 `--codex-home` 时同时指定 `--config` |
| `CC_SWITCH_HOME` | CC Switch 数据目录，默认用户主目录下的 `.cc-switch` |
| `CC_SWITCH_DB` / `--cc-switch-db` / `-CcSwitchDatabase` | CC Switch SQLite 路径，优先于 `CC_SWITCH_HOME` |
| `CODEX_BRIDGE_URL` / `--listen` / `-BridgePort` | Bridge 监听地址；环境变量采用 `http://127.0.0.1:端口/v1` |
| `CODEX_BRIDGE_UPSTREAM_URL` / `--upstream` / `-UpstreamUrl` | CC Switch 上游 URL |
| `CODEX_EXECUTABLE` | Codex CLI 可执行文件路径 |
| `CODEX_BRIDGE_BACKUP_ROOT` | Windows 管理脚本的备份根目录 |
| `--policy-file`、`--status-file` / `-BridgeStateDirectory` | 本地策略和状态目录 |

自定义端口示例：

```powershell
$env:CODEX_BRIDGE_URL = 'http://127.0.0.1:18122/v1'
$env:CODEX_BRIDGE_UPSTREAM_URL = 'http://127.0.0.1:18121'
python src/codex_cross_provider_bridge.py
```

hooks 安装器会保存所选 Bridge 地址、CC Switch 地址和数据库位置，不依赖桌面应用继承交互式终端的环境变量。自定义目录后须重新安装 hooks。

超时可以单独配置：

```powershell
python src/codex_cross_provider_bridge.py --upstream-header-timeout 600 --upstream-idle-timeout 120 --upstream-compact-idle-timeout 600
```

远程压缩仍取决于当前模型、provider 和客户端的支持能力。Bridge 延长等待并修复请求状态，不会让不支持压缩的上游自动获得支持。

## 凭证和隐私

仓库不提供 API key。官方请求使用 Codex 已有的登录信息；第三方凭证由 CC Switch 管理。额外显式路由仅通过 `--provider-route-bearer-env` 指定凭证的环境变量名。

`state/`、备份、数据库、认证文件、日志和归档均为本机数据，不提交。状态可能含会话 ID、标题和工作目录；对外分享前也需要脱敏。详见 [SECURITY.md](SECURITY.md)。

没有默认绑定的第三方 provider ID。瞬态重试与保留 provider 状态需通过 `--retry-provider`、`--preserve-state-provider` 显式指定；窄范围的官方兼容重试独立于这两个设置。

## 测试与发布

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File tests/Test-CodexBridgeConfig.ps1
python scripts/check_publication.py
```

自动测试使用临时目录和本地模拟上游，不发送收费模型请求、不使用真实凭证。原生客户端验证结果另见[验证说明](docs/validation.zh-CN.md)。

发布前还应使用 Gitleaks 检查工作树和 Git 历史：

```powershell
gitleaks dir --redact --config .gitleaks.toml .
gitleaks git --redact --config .gitleaks.toml .
```

版本号保存于 `VERSION`，版本变更写入 `CHANGELOG.md`，以同名 `vX.Y.Z` 标签定位提交。CI 执行 Python 回归测试和公开内容检查。新增版本不覆盖已有标签。

## 恢复和旧工具

[恢复说明](docs/recovery.zh-CN.md)、[生命周期 hooks](docs/lifecycle-hooks.zh-CN.md)、[作用范围](docs/scope-and-status.zh-CN.md)。

旧的历史迁移、分支交接和探测入口仍保留用于明确的人工操作，未纳入推荐自动运行流程。它们涉及历史和数据库写入；分页历史还存在字节偏移约束，不应作为处理跨 provider 压缩问题的默认方案。
