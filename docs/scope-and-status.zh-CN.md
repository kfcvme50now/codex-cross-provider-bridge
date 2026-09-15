# 作用范围与状态

Bridge 支持三种运行时范围。

## `all`

所有 Requests 请求都经过清理。适合持续跨 provider 工作，但会关闭原 provider
的隐藏 reasoning 复用。

## `next`

只对下一条 Responses 请求生效，处理后自动解除。适合临时验证或只修复一次。

## `conversation`

可以指定会话 ID；也可以不指定 ID，让 Bridge 自动锁定下一个会话。

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action repair `
  -RuntimeScope conversation `
  -ConversationId "<conversation-id>"
```

自动锁定下一个会话：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action repair `
  -RuntimeScope conversation `
  -LockNextConversation
```

Fork 会产生新的会话 ID，因此指定旧会话 ID 时，fork 后不会命中。此时可通过状态
输出取得新会话 ID，或改用 `next` / 自动锁定模式。

## 自动修复范围

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-automation `
  -AutoHistoryScope all
```

表示自动迁移所有满足安全条件的闲置历史会话。

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-automation `
  -AutoHistoryScope conversation `
  -ConversationId "<conversation-id>"
```

表示只迁移指定会话。默认跳过 Bridge 最近请求对应的当前会话；如确需处理当前
会话，可增加 `-IncludeCurrentConversation`。

`AutoHistoryScope none` 只保留自动路由修复，不迁移任何历史会话。

## 生命周期作用范围

压缩修复和会话启动修复分别配置：

```text
CompactHookMode=disabled|inspect|repair-and-continue|repair-and-stop|
                repair-and-branch|branch-only|block-only
SessionStartMode=disabled|repair|repair-and-probe
RouteRepairMode=disabled|inspect|repair
```

自动化探测和切换后的请求范围分别配置：

```text
PostSwitchProbeMode=disabled|cli|app-server
PostSwitchScope=preserve|next|all
```

`next` 表示只让下一条新会话请求进入 Bridge 清理范围；`all` 表示持续生效；
`preserve` 表示不改现有 Bridge policy。

分支模式由两个开关共同约束：

```text
CompactHookMode=branch-only / repair-and-branch
AllowAutoBranch=true
```

默认 `AllowAutoBranch=false`。未显式开启时，即使选择了分支模式也会安全地
阻止本次压缩。

## 历史范围

- `HistoryScope all`：确保 `custom` 与 `cc-switch-official` 两个历史桶都可解析。
- `HistoryScope conversation`：只审计指定会话，并补足该会话实际引用的 provider。
- `HistoryScope none`：只配置运行时 Bridge，不改变历史兼容配置。

## 状态字段

`Manage-CodexCrossProviderBridge.ps1 -Action status` 会显示：

```text
runtime_scope
runtime_armed
automation_task_present
automation_task_state
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
target_conversation_id
last_conversation_id
last_conversation_title
last_conversation_cwd
last_conversation_provider
last_needs_repair
last_repair_status
last_upstream_status
last_request_path
last_request_outcome
in_flight_request_count
conversation_history_repair_required
```

这些字段用于回答两个问题：

1. 当前命中的是哪条会话？
2. 下一次请求是否会自动执行修复？

另外三个字段用于回答“上一次请求到底怎么了”：

- `last_request_path`：最后一次请求的路径（`/v1/responses` 或
  `/v1/responses/compact`）。
- `last_request_outcome`：`completed`、`upstream-idle-timeout`、
  `upstream-headers-timeout`、`client-aborted` 或 `upstream-error`。
- `in_flight_request_count`：仍在转发中的请求数量；大于 0 时还会输出
  `in_flight_request=<path> started_at=<时间戳>`。

上游停滞阈值由 Bridge 启动参数控制（首包默认 120 秒，空闲默认 120 秒，`0`
表示关闭），详见 [`../README.md`](../README.md) 的“上游停滞保护”。
