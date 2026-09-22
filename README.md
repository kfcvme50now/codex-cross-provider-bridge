# Codex Cross-Provider Bridge

> [!WARNING]
> 本项目主要由 AI 生成，只在有限的本机环境中完成了针对性验证。
> 它不能保证适用于所有 Codex、CC Switch、Provider、协议和模型组合，
> 也不能保证不会引入新问题、性能变化或兼容性回归。
>
> 使用前请先阅读脚本、确认备份策略，并在可恢复环境中测试。
> 本项目不是 OpenAI、Codex 或 CC Switch 的官方组件，也不代表任何官方立场。

## 项目目标

本项目用于缓解 Codex 在 CC Switch 中切换 Provider 后出现的三类问题：

1. 历史会话可见，但无法打开。
2. 历史会话可以打开，但继续聊天时返回 HTTP 400。
3. 历史会话恢复后，远程压缩仍读取旧的 provider/model 组合而失败。

默认运行路径不会修改已保存的历史消息。`repair` 会在先备份 CC Switch SQLite
数据库后，为每个 Codex provider 模板写入一个受标记管理的空闲历史别名；它通过
两个层次处理兼容性：

- 为历史会话仍引用的 Provider ID 提供本地别名。
- 在请求离开本机前，清理无法跨 Provider 重放的 Responses 状态。

模板保护会让官方 provider 保留 `custom -> bridge`，第三方 provider 保留
`cc-switch-official -> bridge`。因此 CC Switch 后续重新投影 `config.toml` 时，不会
再次删除打开历史会话所需的别名；当前 provider 的正常路由仍由 CC Switch 管理。

对“远程压缩仍读取旧 provider”的历史会话，另有一个显式或自动迁移入口；它只在
安全条件成立时更新 rollout provider 元数据和对应的 Codex SQLite 行，并在写入前
备份完整 SQLite 和 rollout 文件。

目标是同时覆盖：

- 历史会话修复：让旧会话重新可加载、可继续。
- 新会话修复：从第一条请求开始维持可切换的稳定路由。
- 可选自动修复：后台重新应用 CC Switch 路由，并在安全条件下迁移远程压缩风险
  会话；默认关闭。

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

Codex 当前会用 Zstandard 压缩部分 Responses 请求。安装或更新后先安装唯一的
运行时依赖：

```powershell
python -m pip install -r .\requirements.txt
```

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

### 结论三：优先复用同类 Provider 状态，再安全降级

加密 reasoning 和 Provider 私有响应 ID 不是通用协议数据。跨 Provider 时，
Bridge 会优先保证可见消息、助手消息、工具调用和工具结果的可用性，并放弃无法
验证的隐藏 reasoning。

但对明确允许的 CC Switch Provider（默认是 `default` 与
`anyrouter-codex-gpt6`），Bridge 会先原样发送现有 ID、
`previous_response_id` 与 `reasoning.encrypted_content`。只有上游明确返回可移植性
错误时，才自动进行一次清理后的保守重放。因此，同一 Provider 能继续利用已有的
加密 reasoning，跨 Provider 又不会因为私有状态永久失败。Bridge 始终只修改出站
副本，不会删除 rollout 中已经保存的加密内容。

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

修复方式是只对满足安全条件的历史会话，将持久化 provider 状态迁移到当前
兼容 provider，例如 `custom`。可以按会话手动指定，也可以启用受保护的后台
自动迁移。迁移前会备份 rollout JSONL 和 `state_5.sqlite`，成功后同时更新：

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

常规 Bridge 路径不重写历史文件，而是：

1. 只读检查 Codex 历史索引中的 Provider 元数据。
2. 确保历史会话引用的 `custom` 或 `cc-switch-official` 可解析。
3. 继续旧会话时，先清理请求中的 Provider 私有续接状态。

Provider 定义使用当前活动路由作为兼容别名，因此历史线程可以继续使用当前
Provider，而不必强制修改历史数据库。

只有当远程压缩必须读取正确的持久化 provider 时，才进入独立迁移路径；该路径
会备份并重写目标会话的 provider 元数据。

### 新会话

所有新会话从第一次请求起经过 Bridge：

- 请求会被检查是否需要历史修复；
- 当前会话 ID、标题、工作目录和 Provider 会写入状态文件；
- Bridge 会记录命中的范围、是否需要修复、上游 HTTP 状态；
- 对确实含跨 Provider 状态的请求，Bridge 会用该线程数据库里当前选中的模型替换
  预压缩仍携带的旧模型名，避免把 `deepseek-flash` 一类旧模型发给当前官方 Provider；
- 如果历史状态仍被上游拒绝，会自动进行一次更保守的无 reasoning 重放。

### Provider 状态保留与请求清理规则

对 `default` 与 `anyrouter-codex-gpt6`，Bridge 首次请求保留 Provider 私有状态；
若上游明确拒绝，再对第二次请求副本执行以下清理：

- 删除 `input[].id`
- 删除 `previous_response_id`
- 删除 `reasoning.encrypted_content`
- 清空不可移植的 reasoning 文本
- 强制 `store=false`
- 必要时删除 reasoning 与 item reference

其他 Provider 或无法读取 CC Switch 当前 Provider 时，仍采用“先清理、必要时再做
更保守重放”的兼容路径。可用重复的 `--preserve-state-provider <provider-id>` 参数
自定义保留名单。

以下内容会保留：

- 用户消息
- 助手消息
- 普通函数调用
- 工具调用结果
- 兼容的内容块

### 上游停滞保护

第三方 Provider 有时会接受请求后不再返回任何数据。这种情况下 Codex 侧只会
长时间等待，上游、CC Switch 和本地都不会留下“请求失败”的记录。Bridge 因此在
转发期间记录并在必要时中止这种请求：

- 请求转发前先写入在途记录（`inFlight`），因此卡住时状态文件不再是空的；
- 支持 Codex 当前使用的 `Content-Encoding: zstd`（以及 gzip），解压后才做 JSON
  修复；未知编码返回结构化 HTTP 400，不再用会触发 Codex 连续重试的 415；
- 超过首包阈值（`--upstream-header-timeout`，默认 120 秒）没有响应头时，返回
  504 并记录 `upstream-headers-timeout`；
- 超过空闲阈值（`--upstream-idle-timeout`，默认 120 秒）没有任何响应数据时，
  向 SSE 流写入一个 `error` 事件后关闭连接，并记录 `upstream-idle-timeout`；
- 客户端提前断开时记录 `client-aborted`。

此外，Bridge 会只读检查 CC Switch 的当前 Provider 元数据：

- `meta.routing_disabled=true` 时直接返回 HTTP 424，且不发送任何上游请求；
- 默认对 `anyrouter-codex-gpt6` 启用健康门禁；CC Switch 已标记不健康时直接返回
  HTTP 424；
- AnyRouter 在健康状态过时的情况下返回 4xx/5xx、空成功响应、连接异常或流中途
  终止时，Bridge 会输出结构化错误并打开默认 60 秒的本地短路；短路期间请求继续
  返回 424，不会静默退出或不断撞击上游；
- 状态文件记录 `activeProviderId`、`errorCategory`、`retrySuppressed` 与
  `circuitOpenUntil`，不记录响应正文或凭据。

健康门禁与短路名单可用重复的 `--health-guard-provider <provider-id>` 参数配置，
短路时长可用 `--provider-circuit-seconds` 调整。OpenCode Go 当前仅在本机 CC Switch
数据库中标记为“订阅过期、禁止路由”；它的 Provider 配置与凭据仍保留，以便以后
恢复订阅后重新启用。

通用、可逆的 Provider 声明工具不会读取或删除 `settings_config`。先预览，再应用：

```powershell
python .\src\codex_ccswitch_provider_policy.py `
  --provider-id "<provider-id>" --disable --reason subscription_expired

python .\src\codex_ccswitch_provider_policy.py `
  --provider-id "<provider-id>" --disable --reason subscription_expired --apply
```

恢复订阅后只移除 Bridge 管理的禁用标记，不会自动把 Provider 设为当前项或放回
故障转移队列：

```powershell
python .\src\codex_ccswitch_provider_policy.py `
  --provider-id "<provider-id>" --enable --apply
```

阈值按实测留出余量：50 万 token 上下文、`reasoning.effort=high` 的正常请求，
最长静默约 12 秒，整次请求约 20 秒。被中止的请求在 Codex 侧显示
`stream disconnected before completion` 或明确的 Provider 错误，而不是无限等待。
两个阈值都是秒数，设为 `0` 表示关闭该限制。修改后需要重启 Bridge 任务：

```powershell
pwsh .\scripts\Manage-CodexCrossProviderBridge.ps1 -Action stop
pwsh .\scripts\Manage-CodexCrossProviderBridge.ps1 -Action start
```

记录只包含路径、时间、状态码和字节数，不包含请求正文、响应正文或凭据。

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
automation_task_present
automation_task_state
bridge_listening
bridge_url
config_uses_bridge
custom_provider_present
official_alias_present
runtime_scope
history_scope
runtime_armed
target_conversation_id
automation_config_repair_status
automation_history_status
automation_history_candidate_count
automation_history_applied_count
automation_post_switch_probe_status
automation_post_switch_probe_ok
automation_post_switch_probe_error_category
automation_post_switch_policy_status
automation_post_switch_policy_scope
lifecycle_hooks_installed
lifecycle_hook_backup_count
compact_repair_mode
auto_branch_enabled
session_start_mode
route_repair_mode
post_switch_probe_mode
post_switch_scope
lifecycle_status_event
lifecycle_status_result
lifecycle_status_title
lifecycle_status_cwd
last_conversation_id
last_conversation_title
last_conversation_cwd
last_conversation_provider
last_needs_repair
last_repair_status
last_upstream_status
last_request_path
last_request_outcome
last_active_provider_id
last_error_category
last_retry_suppressed
last_circuit_open_until
in_flight_request_count
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
- `modelBefore` / `modelAfter`（位于 `state/status.json`）记录跨 Provider 请求是否把
  旧模型同步为线程当前模型；普通请求不会改写模型。
- `last_request_outcome` 表示最后一次请求如何结束：

```text
completed                  上游响应正常结束
upstream-idle-timeout      上游超过空闲阈值没有任何数据
upstream-headers-timeout   上游超过首包阈值没有返回响应头
client-aborted             客户端在响应结束前断开
upstream-error             转发过程中出现传输错误
```

- `in_flight_request_count>0` 表示请求已转发但尚未结束；`in_flight_request`
  给出路径与开始时间，可用于判断请求是否卡住。

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

## 自动修复

自动修复是一个独立的、默认关闭的计划任务。它不会启动或停止 CC Switch，
也不会修改普通 ChatGPT 登录账号的 GPT 会话。

通过 `enable-automation` 启用时，默认会同时设置
`PostSwitchProbeMode=cli` 和 `PostSwitchScope=next`，即在活动路由变化后发出
一次固定内容的最小真实请求，并让下一条会话请求进入 Bridge 清理范围。不希望
产生探测请求时可显式设置为 `disabled`。

启用所有安全范围内的历史修复：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-automation `
  -AutoHistoryScope all `
  -AutoIdleSeconds 300 `
  -AutoMaxMigrationsPerRun 10
```

只修复指定历史会话：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-automation `
  -AutoHistoryScope conversation `
  -ConversationId "<conversation-id>" `
  -AutoIdleSeconds 300
```

默认会从 Bridge 最近请求状态中取得当前会话 ID，并跳过该会话；如果确实希望自动
任务处理当前会话，需要显式增加：

```powershell
-IncludeCurrentConversation
```

关闭：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action disable-automation
```

自动任务只会在以下条件同时成立时迁移历史：

```text
当前 Codex 路由是非官方 Provider
base_url 是 CC Switch 的 127.0.0.1:15721/v1
history threads.model_provider 是 openai
history model 不是 gpt-*、codex*、o1-*、o3-* 或 o4-*
会话最近至少空闲 AutoIdleSeconds
target provider 已存在于 config.toml
```

因此 `openai + gpt-*` 会话不会进入迁移候选；当前会话默认排除，`AutoIdleSeconds`
未达到的会话会留到下一轮检查。每条迁移都会创建独立的 rollout 和完整 SQLite
快照，失败时自动恢复该条会话。迁移完成后建议重新打开会话或重启 Codex，
让运行时重新读取持久化 provider。

### 切换后的真实探测

启用自动修复时，可以要求每次 provider/model/base_url 指纹变化后发出一条固定
内容的最小真实请求：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-automation `
  -PostSwitchProbeMode cli `
  -PostSwitchScope next
```

探测只记录成功/失败、错误分类、provider/model 和时间，不保存响应正文或凭据。
`PostSwitchScope=next` 会让下一条新会话请求进入 Bridge 清理范围；
`PostSwitchScope=all` 或 `preserve` 也可以选择。

也可以手动执行：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action probe-provider `
  -PostSwitchProbeMode cli `
  -PostSwitchScope next `
  -ApplyOperation
```

### 压缩钩子与分支续接

Codex 支持 `PreCompact` / `SessionStart` / `UserPromptSubmit` 生命周期钩子。本项目
可以将钩子安装到 `$CODEX_HOME/hooks.json`，并在风险会话压缩前：

1. 备份 rollout 与 SQLite；
2. 迁移持久化 provider/model；
3. 继续、停止或创建新分支。

以及在 `SessionStart` 和下一次提示词提交时检查活动路由：如果
`config.toml` 已经被 CC Switch 重写、bridge 被挤出请求路径，就按 `RouteRepairMode`
修复。启动阶段修复不会阻止发送；提示词阶段的兜底修复会阻止本次发送，重新发送
即可。路由已经指向 bridge 时，钩子还会检查 15722 是否实际监听；若计划任务已退出，
会先尝试启动，启动失败则阻止本次消息，避免把请求发到一个不存在的本地端口。这条
路径不需要额外的轮询式后台修复任务。

默认自动分支关闭。推荐先使用“修复后停止”，避免在当前 turn 已捕获旧 provider
的边界条件下继续发送远程压缩：

```powershell
pwsh .\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-compact-hook `
  -CompactHookMode repair-and-stop `
  -SessionStartMode repair `
  -RouteRepairMode repair `
  -AllowAutoBranch:$false `
  -ApplyOperation
```

安装后需要在 Codex `/hooks` 中审核并信任用户钩子（卸载方式见下）。

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action disable-compact-hook
```

钩子命令由安装器写入 `$CODEX_HOME/codex-lifecycle-hook.cmd`，`hooks.json` 只引用
这个脚本。Windows 上引用不含空格的 wrapper 路径时不能再给整条命令额外加引号；
新版 Codex 会把开头引号当成可执行文件名的一部分，使界面显示钩子状态但脚本未启动。
首次请求修复的行为、记录位置与信任要求见
[`docs/lifecycle-hooks.zh-CN.md`](docs/lifecycle-hooks.zh-CN.md)。

如果希望将风险会话复制为一个新的兼容 thread，而不是继续原 thread，可以显式
启用 `branch-only` 或 `repair-and-branch`。Desktop 没有公开接口自动切换到新
分支，工具会返回新的会话 ID、标题和工作目录，之后需要在 Codex UI 中打开新
会话。

详细策略、后端选择和恢复说明见
[`docs/lifecycle-hooks.zh-CN.md`](docs/lifecycle-hooks.zh-CN.md)。

## 备份与恢复

恢复不是简单覆盖，而是一条可审计、追加式的快照链。

### 自动快照

以下操作前会自动创建快照：

```text
pre-install
pre-repair
pre-migration
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

列出单会话迁移备份：

```powershell
.\scripts\Restore-CodexThreadProviderMigration.ps1 -List
```

预览把某条会话恢复到迁移前的 provider：

```powershell
.\scripts\Restore-CodexThreadProviderMigration.ps1 `
  -BackupDirectory "<migration-backup-directory>"
```

确认后执行：

```powershell
.\scripts\Restore-CodexThreadProviderMigration.ps1 `
  -BackupDirectory "<migration-backup-directory>" `
  -Apply
```

恢复操作会先检查当前 provider 是否仍等于 manifest 中的迁移目标；不匹配时拒绝
写入。写入前还会为当前状态创建新的迁移备份，因此恢复本身也可以再次反向恢复。

生命周期钩子安装/卸载前会保存独立的 `hooks.json` 快照：

```text
~/.codex/backups/codex-cross-provider-hooks/<timestamp>/
```

列出并恢复：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action list-hook-backups
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action restore-hook-backup `
  -HookBackupDirectory "<backup-directory>" `
  -ApplyOperation
```

卸载只移除本项目管理的钩子定义，保留用户原有的其他钩子。生命周期策略文件在
写入前也会保留 JSON 备份。

列出并恢复策略备份：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action list-policy-backups
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action restore-policy-backup `
  -PolicyBackupFile "<policy-backup-file>" `
  -ApplyOperation
```

`restore` 和 `uninstall` 会先撤销自动修复计划任务，再恢复 `config.toml` 和
Bridge 配置；这不会删除快照、rollout 备份或 CC Switch 设置备份。自动任务状态
保存在本地 `state/automation-status.json`，`state/` 不会提交到公开仓库。

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
|   |-- lifecycle-hooks.zh-CN.md
|   |-- queued-follow-up.zh-CN.md
|   |-- recovery.zh-CN.md
|   `-- scope-and-status.zh-CN.md
|-- scripts/
|   |-- CodexCrossProviderBridge.Common.ps1
|   |-- Invoke-CodexBranchHandoff.ps1
|   |-- Invoke-CodexCrossProviderAutomation.ps1
|   |-- Invoke-CodexProviderProbe.ps1
|   |-- Manage-CodexCrossProviderBridge.ps1
|   |-- Manage-CodexLifecycleHooks.ps1
|   |-- Repair-Codex-CCSwitchProviderAlias.ps1
|   |-- Restore-CodexThreadProviderMigration.ps1
|   `-- Restore-CodexCrossProviderState.ps1
|-- src/
|   |-- codex_app_server_client.py
|   |-- codex_branch_handoff.py
|   |-- codex_config_guard.py
|   |-- codex_cross_provider_bridge.py
|   |-- codex_executable.py
|   |-- codex_history_audit.py
|   |-- codex_hook_manager.py
|   |-- codex_internal.py
|   |-- codex_lifecycle_control.py
|   |-- codex_lifecycle_hook.py
|   |-- codex_lifecycle_policy.py
|   |-- codex_provider_automation.py
|   |-- codex_provider_probe.py
|   `-- codex_thread_provider_migrate.py
`-- tests/
    |-- Test-CodexBridgeConfig.ps1
    |-- test_app_server_client.py
    |-- test_branch_handoff.py
    |-- test_codex_cross_provider_bridge.py
    |-- test_hook_manager.py
    |-- test_lifecycle_policy.py
    |-- test_provider_automation.py
    |-- test_provider_probe.py
    `-- test_thread_provider_migrate.py
```

## 验证

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File .\tests\Test-CodexBridgeConfig.ps1
Invoke-ScriptAnalyzer -Path .\scripts -Recurse
Invoke-ScriptAnalyzer -Path .\tests -Recurse
```

PowerShell 脚本请使用 PowerShell 7（`pwsh`）。在 Windows PowerShell 5.1 中，
如果 `PSModulePath` 先指向 PowerShell 7 的模块目录，`Get-FileHash` 等 cmdlet
可能不存在；脚本对快照哈希做了 .NET 回退，但其余 cmdlet 仍依赖宿主。

当前验证覆盖：

- 历史 Provider 别名恢复
- 新旧会话 Provider 元数据
- `all / next / conversation` 范围
- `input[].id` 清理
- encrypted reasoning 清理
- allowlist Provider 的 encrypted reasoning 首次保留与拒绝后降级
- `previous_response_id` 清理
- 保守重试
- 流式转发
- 在途请求记录
- 上游首包超时与停滞中止
- Provider 禁用/不健康预检、空响应、异常状态与本地短路
- legacy `/responses/compact` 清理
- 远程压缩 provider/model 风险检测
- 单会话 provider 状态迁移与备份
- 自动修复的 Provider/模型安全边界
- 当前会话排除、空闲阈值和迁移并发检查
- `PreCompact` / `SessionStart` 钩子决定与 JSON 输出
- 自动分支关闭、分支创建和原会话不变
- app-server `thread/fork(lastTurnId=...)` 协议
- CLI / app-server 真实探测
- provider 切换指纹与一次性探测
- `hooks.json` 合并、卸载、备份和恢复
- 备份与恢复
- 重复执行幂等性
- PowerShell 语法与静态分析

## 已知限制

- `repair` 会把历史别名写入现有 CC Switch provider 模板；之后新增的 provider 仍需
  再执行一次 `repair`。若生命周期钩子未受信任，活动第三方路由被重新投影后仍可能
  暂时绕过 Bridge。
- 历史线程如果仍以 `openai` provider 搭配第三方模型运行，可执行单会话迁移或
  启用自动修复；自动修复默认跳过当前会话并等待空闲阈值。
- 历史会话修复不等于跨 Provider 的隐藏 reasoning 无损迁移。
- 自动迁移会短暂改写 rollout 文件与 `state_5.sqlite`；脚本会先做完整备份并在
  失败时回滚，但仍建议先关闭正在写入该会话的 Codex 任务。
- `PreCompact` 无法可靠改变当前 turn 已经捕获的 provider/model。默认
  `repair-and-continue` 是尽力而为；需要严格保证时可改用 `repair-and-stop`。
- 自动分支默认关闭。即使创建了新分支，Codex Desktop 也没有公开接口让 UI
  自动切换过去，需要按输出的新会话 ID/标题手动打开。
- 用户级 `hooks.json` 需要先在 Codex `/hooks` 中审核并信任，否则钩子不会执行。
- 真实探测会在每次活动路由指纹变化后发送一条固定内容的最小模型请求；不使用
  时应设置 `PostSwitchProbeMode=disabled`。
- 不同 Provider 的协议、模型、工具 schema 和上下文窗口仍可能不兼容。
- 当前实现只在有限环境中验证，不能替代完整的跨平台和全 Provider 测试。
- 不建议将 Bridge 监听地址暴露到局域网或公网。

## 动态路由评估

CC Switch 3.20.3 支持路由服务运行期间热切换当前 Provider，无需重启 Codex；但其
Provider 选择仍是“每个应用一个全局当前 Provider”，不是按每条请求中的模型、
推理强度或会话选择 Provider。并发请求前反复修改全局当前 Provider 会产生串线
竞态，因此本项目不采用这种方式模拟动态路由。

可行的请求级路由、按会话固定路由、仅对子代理切换模型的方案，以及现有项目对比
见 [`docs/dynamic-routing.zh-CN.md`](docs/dynamic-routing.zh-CN.md)。

## 非 Provider 类问题

Codex Desktop 的以下错误与 CC Switch、Bridge 均无关：

```text
App-server queued follow-up no longer exists
```

这是 Desktop 队列编辑状态机问题。上游维护者已经确认修复会进入下一版本。
详细分析、本机日志特征与恢复方式见：

- `docs/queued-follow-up.zh-CN.md`
- https://github.com/openai/codex/issues/44781

## 相关社区问题

截至本项目当前验证版本，CC Switch 侧仍没有已合并的完整通用修复：

- `#4030` 仍在跟踪 Chat/Responses 远程压缩协议转换失败。
- `#5398` 仍在跟踪切换 Provider 后删除历史 provider 定义的问题。
- `#5974` 仍在跟踪统一历史在本地路由/官方 provider 分桶下的不一致。
- PR `#5536` 提供了 Chat-backed 远程压缩转换方向，但状态仍为打开且未合并。
- PR `#7147` 提供了真实最小模型请求验证器方向，但状态仍为打开。
- PR `#5735` 提供了统一历史/provider takeover 状态同步方向，但状态仍为打开。
- CC Switch 当前 `stream_check` 主要检查端点可达性，不等价于真实模型请求。

本项目通过本地 Bridge 与受保护的 provider 元数据迁移覆盖上述问题，不依赖 CC
Switch 内部改动。

- `farion1231/cc-switch#4030`
- `farion1231/cc-switch#5398`
- `farion1231/cc-switch#5922`
- `farion1231/cc-switch#5974`
- `farion1231/cc-switch#6340`
- `farion1231/cc-switch#6503`
- `farion1231/cc-switch#6658`
- `farion1231/cc-switch#7257`
- `farion1231/cc-switch#5536`
- `farion1231/cc-switch#5735`
- `farion1231/cc-switch#7147`
- `farion1231/cc-switch#4725`
- `farion1231/cc-switch#6156`
- `openai/codex#38930`
- `openai/codex#42313`
- `openai/codex#37010`
