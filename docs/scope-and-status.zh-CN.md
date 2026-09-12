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

## 历史范围

- `HistoryScope all`：确保 `custom` 与 `cc-switch-official` 两个历史桶都可解析。
- `HistoryScope conversation`：只审计指定会话，并补足该会话实际引用的 provider。
- `HistoryScope none`：只配置运行时 Bridge，不改变历史兼容配置。

## 状态字段

`Manage-CodexCrossProviderBridge.ps1 -Action status` 会显示：

```text
runtime_scope
runtime_armed
target_conversation_id
last_conversation_id
last_conversation_title
last_conversation_cwd
last_conversation_provider
last_needs_repair
last_repair_status
last_upstream_status
conversation_history_repair_required
```

这些字段用于回答两个问题：

1. 当前命中的是哪条会话？
2. 下一次请求是否会自动执行修复？
