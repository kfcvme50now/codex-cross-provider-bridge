# Codex Cross-Provider Bridge

> [!WARNING]
> 本项目主要由 AI 生成，只在有限的本机环境中完成了针对性验证。
> 它不能保证适用于所有 Codex、CC Switch、Provider、协议和模型组合，
> 也不能保证不会引入新问题、性能变化或兼容性回归。
>
> 使用前请先阅读脚本、确认备份策略，并在可恢复环境中测试。
> 本项目不是 OpenAI、Codex 或 CC Switch 的官方组件，也不代表任何官方立场。

## 项目目标

本项目用于缓解 Codex 在 CC Switch 中切换 Provider 后出现的两类问题：

1. 历史会话可见，但无法打开。
2. 历史会话可以打开，但继续聊天时返回 HTTP 400。

本项目不会修改已保存的历史消息、Codex SQLite 数据库或 CC Switch 的 Provider
数据库。它通过两个层次处理兼容性：

- 为历史会话仍引用的 Provider ID 提供本地别名。
- 在请求离开本机前，清理无法跨 Provider 重放的 Responses 状态。

目标是同时覆盖：

- 历史会话修复：让旧会话重新可加载、可继续。
- 新会话修复：从第一条请求开始维持可切换的稳定路由。

## 架构

```text
Codex
  |
  v
127.0.0.1:15722  Cross-Provider Bridge
  |
  v
127.0.0.1:15721  CC Switch
  |
  v
当前 Provider
```

Bridge 只处理内存中的请求副本。它不记录请求正文、Authorization、API Key 或
用户消息。状态文件中可能包含会话标题和工作目录，因此 `state/` 与备份目录不应
提交到公开仓库。

## 问题复现

### 现象一：Provider 定义悬空

切换 Provider 后，Codex 提示：

```text
ChatGPT 无法加载 config.toml，因此此对话串无法继续。
请修复 config.toml：Model provider `cc-switch-official` not found。
```

或者：

```text
Model provider `custom` not found
```

会话文件、SQLite 记录和历史内容仍然存在，但历史线程仍引用旧 Provider ID：

```text
historical thread -> model_provider = "custom"
historical thread -> model_provider = "cc-switch-official"
```

CC Switch 切换 Provider 时会重写 `config.toml`，如果旧定义没有保留，就形成：

```text
thread references model_providers.<id>
live config.toml does not define model_providers.<id>
```

这不是历史数据损坏，而是当前配置与历史元数据之间的悬空引用。

### 现象二：历史请求不可移植

即使 Provider ID 可以解析，跨 Provider 继续聊天仍可能失败。实际复现中，
第三方 Provider 产生的历史交给另一套 Responses API 后返回：

```text
400 invalid_request_error
param: input[4].id
code: invalid_value
```

历史请求可能包含：

- 原 Provider 创建的 `input[].id`
- `previous_response_id`
- `reasoning.encrypted_content`
- 不可跨 Provider 重放的 reasoning 内容
- 指向旧服务端状态的 `item_reference`
- 旧 Provider 的模型名或响应项结构

这些字段在原 Provider 内可能有效，但换到新 Provider 后，ID 前缀、服务端归属、
加密内容和重放结构都可能不再合法。

## 原因排查与结论

### 结论一：Provider 可见性与请求兼容性是两个问题

只恢复 Provider 定义，可以解决“无法打开”，但不能保证“可以继续聊天”。

只清理请求，也不能解决历史线程引用的 Provider ID 在 `config.toml` 中不存在。

因此修复必须同时覆盖：

1. 历史线程的 Provider ID 可解析。
2. 发送给新 Provider 的历史请求可移植。

### 结论二：不应直接重写历史文件

直接修改 JSONL 或 SQLite 中的 Provider ID 虽然可能让问题暂时消失，但会破坏：

- 原 Provider 下原本可继续的会话；
- 会话元数据与运行时设置的一致性；
- 回滚和审计能力。

本项目选择保留原始历史，只修改即将发出的请求副本。

### 结论三：Provider 私有 reasoning 无法保证无损迁移

加密 reasoning 和 Provider 私有响应 ID 不是通用协议数据。跨 Provider 时，
Bridge 会优先保证可见消息、助手消息、工具调用和工具结果的可用性，并放弃无法
验证的隐藏 reasoning。

这意味着：

- 用户可见对话可以继续；
- 可以保留兼容的 tool call / tool result；
- 不能保证保留完整隐藏推理；
- 不能把 Anthropic、Gemini 或其他非 Responses 协议凭空转换成 Responses。

### 现象三：返回 ChatGPT 账号不支持的第三方模型

手动执行 `/compact` 或自动压缩时，可能出现：

```text
Error running remote compact task:
The 'deepseek-flash' model is not supported when using Codex with a ChatGPT account.
```

本地历史记录中可观察到：

```text
session_meta.model_provider = "openai"
thread_settings_applied.model_provider_id = "openai"
thread_settings_applied.model = "deepseek-flash"
```

根因不是压缩提示词，也不是 Bridge 的请求清理规则，而是持久化的运行时
provider 与模型不匹配：

```text
model = deepseek-flash
provider = openai
```

远程压缩会读取这个持久化 provider，并把 `deepseek-flash` 发送到 ChatGPT
账号后端，因此被拒绝。压缩请求可能绕过自定义 provider 的 `base_url`，
所以仅给 provider 增加本地别名或清理请求内容并不足以修复。

修复方式是只在用户指定的历史会话中，将持久化 provider 状态迁移到当前
兼容 provider，例如 `custom`。迁移前会备份 rollout JSONL 和
`state_5.sqlite`，成功后同时更新：

- session meta
- `thread_settings_applied.model_provider_id`
- `state_5.sqlite.threads.model_provider`

先预览：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action migrate-history `
  -ConversationId "<conversation-id>" `
  -TargetProvider custom
```

确认后应用：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action migrate-history `
  -ConversationId "<conversation-id>" `
  -TargetProvider custom `
  -ApplyMigration
```

## 修复思路

### 历史会话

Bridge 不重写历史文件，而是：

1. 只读检查 Codex 历史索引中的 Provider 元数据。
2. 确保历史会话引用的 `custom` 或 `cc-switch-official` 可解析。
3. 继续旧会话时，先清理请求中的 Provider 私有续接状态。

Provider 定义使用当前活动路由作为兼容别名，因此历史线程可以继续使用当前
Provider，而不必强制修改历史数据库。

### 新会话

所有新会话从第一次请求起经过 Bridge：

- 请求会被检查是否需要历史修复；
- 当前会话 ID、标题、工作目录和 Provider 会写入状态文件；
- Bridge 会记录命中的范围、是否需要修复、上游 HTTP 状态；
- 如果历史状态仍被上游拒绝，会自动进行一次更保守的无 reasoning 重放。

### 请求清理规则

Bridge 会修改请求副本：

- 删除 `input[].id`
- 删除 `previous_response_id`
- 删除 `reasoning.encrypted_content`
- 清空不可移植的 reasoning 文本
- 强制 `store=false`
- 必要时删除 reasoning 与 item reference

以下内容会保留：

- 用户消息
- 助手消息
- 普通函数调用
- 工具调用结果
- 兼容的内容块

## 作用范围

运行时支持三种范围，均可通过命令行设置。

### 全部会话

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action repair `
  -RuntimeScope all `
  -HistoryScope all
```

### 仅下一条会话

第一条有效 Responses 请求命中后自动解除。

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action repair `
  -RuntimeScope next `
  -HistoryScope none
```

### 指定会话

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action repair `
  -RuntimeScope conversation `
  -ConversationId "<conversation-id>" `
  -HistoryScope conversation
```

### 自动锁定下一个会话

无需预先知道会话 ID。

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action repair `
  -RuntimeScope conversation `
  -LockNextConversation
```

说明：

- `conversation` 不是严格隔离，而是按会话 ID 做提示和范围控制。
- Fork 会产生新的会话 ID，不会自动继承旧会话的指定范围。
- Fork 场景可以使用 `next` 或自动锁定下一个会话。

## 历史修复范围

```text
HistoryScope=all
```

确保 `custom` 与 `cc-switch-official` 两个历史 Provider 桶都可解析。

```text
HistoryScope=conversation
```

只审计指定会话，并补足该会话实际引用的 Provider 定义。

```text
HistoryScope=none
```

只配置运行时 Bridge，不修改历史兼容配置。

## 运行状态

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action status
```

状态输出包含：

```text
task_present
task_state
bridge_listening
bridge_url
config_uses_bridge
custom_provider_present
official_alias_present
runtime_scope
history_scope
runtime_armed
target_conversation_id
last_conversation_id
last_conversation_title
last_conversation_cwd
last_conversation_provider
last_needs_repair
last_repair_status
last_upstream_status
conversation_history_found
conversation_history_repair_required
conversation_missing_provider_ids
```

其中：

- `runtime_armed=true` 表示 `next` 或自动锁定模式仍在等待下一次请求。
- `last_needs_repair=true` 表示请求中发现不可移植状态。
- `last_repair_status=repair-applied` 表示 Bridge 已执行清理。
- `last_repair_status=not-targeted` 表示请求不在当前范围内。
- `last_repair_status=not-needed` 表示请求不需要清理。

## 后台运行

Bridge 通过 Windows 计划任务运行，不依赖前台终端窗口。

查看任务：

```powershell
Get-ScheduledTask -TaskName "CodexCrossProviderBridge"
```

查看 Bridge 信息：

```powershell
Invoke-RestMethod http://127.0.0.1:15722/__bridge/info
```

## 备份与恢复

恢复不是简单覆盖，而是一条可审计、追加式的快照链。

### 自动快照

以下操作前会自动创建快照：

```text
pre-install
pre-repair
pre-restore
manual
```

每个快照包含：

- Codex `config.toml`
- CC Switch `settings.json`
- Bridge policy/status
- Windows 计划任务 XML
- SHA-256 文件和来源清单

快照不会覆盖旧快照，索引只追加。

### 恢复流程

1. 验证目标快照的 manifest 和 TOML。
2. 为当前状态创建 `pre-restore` 快照。
3. 停止当前 Bridge 任务。
4. 恢复快照中的 `config.toml`。
5. 如快照包含计划任务 XML，则恢复任务。
6. 失败时自动回滚到 `pre-restore` 快照。

单会话 provider 迁移另有独立备份：

```text
~/.codex/backups/codex-thread-provider-migrate/<conversation-id>-<timestamp>/
```

其中包含迁移前的 rollout JSONL、完整 `state_5.sqlite` 和 manifest。

列出快照：

```powershell
.\scripts\Restore-CodexCrossProviderState.ps1 -ListSnapshots
```

恢复到默认的 `pre-install` 状态：

```powershell
.\scripts\Restore-CodexCrossProviderState.ps1
```

恢复指定快照：

```powershell
.\scripts\Restore-CodexCrossProviderState.ps1 `
  -SnapshotId "<snapshot-id>"
```

CC Switch 设置默认只备份、不覆盖。需要恢复时显式指定：

```powershell
.\scripts\Restore-CodexCrossProviderState.ps1 `
  -RestoreCcSwitchSettings
```

## 文件结构

```text
.
|-- README.md
|-- SECURITY.md
|-- docs/
|   |-- investigation.zh-CN.md
|   |-- recovery.zh-CN.md
|   `-- scope-and-status.zh-CN.md
|-- scripts/
|   |-- CodexCrossProviderBridge.Common.ps1
|   |-- Manage-CodexCrossProviderBridge.ps1
|   |-- Repair-Codex-CCSwitchProviderAlias.ps1
|   `-- Restore-CodexCrossProviderState.ps1
|-- src/
|   |-- codex_cross_provider_bridge.py
|   |-- codex_history_audit.py
|   `-- codex_thread_provider_migrate.py
`-- tests/
    |-- Test-CodexBridgeConfig.ps1
    |-- test_codex_cross_provider_bridge.py
    `-- test_thread_provider_migrate.py
```

## 验证

```powershell
python -m unittest discover -s tests -v
.\tests\Test-CodexBridgeConfig.ps1
Invoke-ScriptAnalyzer -Path .\scripts -Recurse
Invoke-ScriptAnalyzer -Path .\tests -Recurse
```

当前验证覆盖：

- 历史 Provider 别名恢复
- 新旧会话 Provider 元数据
- `all / next / conversation` 范围
- `input[].id` 清理
- encrypted reasoning 清理
- `previous_response_id` 清理
- 保守重试
- 流式转发
- legacy `/responses/compact` 清理
- 远程压缩 provider/model 风险检测
- 单会话 provider 状态迁移与备份
- 备份与恢复
- 重复执行幂等性
- PowerShell 语法与静态分析

## 已知限制

- CC Switch 切换 Provider 后可能再次覆写 `config.toml`，需要重新执行 `repair`。
- 历史线程如果仍以 `openai` provider 搭配第三方模型运行，需要执行一次单会话
  provider 状态迁移。
- 历史会话修复不等于跨 Provider 的隐藏 reasoning 无损迁移。
- 不同 Provider 的协议、模型、工具 schema 和上下文窗口仍可能不兼容。
- 当前实现只在有限环境中验证，不能替代完整的跨平台和全 Provider 测试。
- 不建议将 Bridge 监听地址暴露到局域网或公网。

## 相关社区问题

- `farion1231/cc-switch#5398`
- `farion1231/cc-switch#5922`
- `farion1231/cc-switch#5974`
- `farion1231/cc-switch#6340`
- `farion1231/cc-switch#6503`
- `farion1231/cc-switch#6658`
- `farion1231/cc-switch#7257`
- `farion1231/cc-switch#5536`
- `farion1231/cc-switch#4725`
- `farion1231/cc-switch#6156`
- `openai/codex#38930`
- `openai/codex#42313`
- `openai/codex#37010`
