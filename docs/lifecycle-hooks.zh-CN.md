# 生命周期钩子、真实探测与分支续接

本文说明可选的自动修复层。它建立在 Bridge 和单会话 provider 迁移之上，不替代
原有修复路径。

## 组件职责

```text
SessionStart / PreCompact 钩子
  -> 判断当前会话是否存在 provider/model 兼容风险
  -> 安全时迁移 provider 元数据
  -> 必要时阻止压缩或创建分支

Provider 切换探测器
  -> 检测活动 provider/model/base_url 变化
  -> 发出固定内容的最小真实请求
  -> 只保存成功/失败状态，不保存提示词或响应正文

分支接管
  -> 不修改原会话
  -> 复制已完成历史到新会话 ID
  -> 覆盖 provider/model
```

## 官方接口边界

Codex 官方文档当前提供：

- `PreCompact` / `PostCompact` 生命周期钩子；
- `PreCompact.matcher=manual|auto`；
- `PreCompact` 返回 `continue: false` 可在压缩前停止；
- `SessionStart`，包括 `source=compact`；
- app-server `thread/fork`，支持覆盖 `modelProvider`、`model` 和
  `lastTurnId`；
- app-server `thread/start` 的 `ephemeral` 模式；
- app-server `turn/start` 真实模型请求。

参考：

- https://developers.openai.com/codex/hooks
- https://developers.openai.com/codex/app-server
- https://developers.openai.com/codex/config-reference

重要限制：

- 钩子不能可靠修改当前 turn 已经捕获的 provider/model。
- 没有公开接口让 Desktop UI 自动切换到新分支。
- 因此默认策略是“安全时修复并继续；不确定时停止或显式分支”。

## 压缩修复模式

通过 `-CompactHookMode` 选择：

| 模式 | 行为 | 适用情况 |
| --- | --- | --- |
| `disabled` | 不处理 | 只安装 SessionStart 修复 |
| `inspect` | 只检查，不写入 | 观察风险 |
| `repair-and-continue` | 备份并修复后继续压缩 | 默认；对当前 turn 已捕获 provider 的情况是尽力而为 |
| `repair-and-stop` | 修复后停止本次压缩 | 之后重新执行 `/compact`，通常更安全 |
| `repair-and-branch` | 修复原会话并创建兼容分支 | 明确接受新会话分支 |
| `branch-only` | 原会话不改，只创建兼容分支 | 最保守的历史保留方式 |
| `block-only` | 不改写，只阻止本次压缩 | 仅诊断或完全手动处理 |

分支相关模式只有在 `-AllowAutoBranch` 时才允许运行。默认关闭。

## 安装与卸载

安装并让指定会话的 `PreCompact` 修复、停止，同时让新会话在启动时尝试修复：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action enable-compact-hook `
  -CompactHookMode repair-and-stop `
  -SessionStartMode repair `
  -PostSwitchProbeMode cli `
  -PostSwitchScope next `
  -ApplyOperation
```

安装后，Codex 可能要求在 CLI 中执行 `/hooks` 审核并信任这些用户钩子。未信任前
钩子不会运行。

关闭：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action disable-compact-hook
```

卸载只删除本项目写入的钩子，不会删除其他用户的 `hooks.json` 条目。

## 会话启动修复

`SessionStartMode` 可选择：

| 模式 | 行为 |
| --- | --- |
| `disabled` | 不做会话启动检查 |
| `repair` | 风险成立时迁移当前会话的持久化 provider |
| `repair-and-probe` | 迁移后再发一个真实最小请求验证活动路由 |

`repair` 是默认值。`repair-and-probe` 会为每次命中的会话启动增加一次真实模型
请求，建议只用于切换后验证窗口。

## 切换后的首次请求修复

CC Switch 切换 provider 时会重写 `config.toml`，把 bridge 从请求路径里挤掉。
`UserPromptSubmit` 钩子负责在**切换后的第一次请求**上恢复路由，因此不需要常驻
后台任务：

1. 提交提示词时钩子读取 `config.toml` 的活动路由；
2. 路由已经指向 bridge 时不做任何事；
3. 路由指向 CC Switch 的 `127.0.0.1:15721/v1` 时，调用
   `Repair-Codex-CCSwitchProviderAlias.ps1`（先建 pre-repair 快照）恢复 bridge
   URL 与历史 provider 别名；
4. 修复成功或失败都会阻止本次发送（`continue: false`），Codex 显示原因，重新
   发送即可。官方 `openai`/GPT 路由不会被改写。

`RouteRepairMode` 可选择：

| 模式 | 行为 |
| --- | --- |
| `disabled` | 不做路由检查 |
| `inspect` | 只检查并记录，不写配置、不阻止发送 |
| `repair` | 检查并修复；需要时阻止本次发送（默认） |

记录位置：

```text
state/lifecycle-status.json      最近一次钩子结果（含 routeBefore/routeAfter）
state/lifecycle-events.jsonl     仅记录需要动作的事件，追加写入
```

### 安装形式

钩子命令由安装器写入 `$CODEX_HOME/codex-lifecycle-hook.cmd`（Windows）或
`.sh`（其他平台），`hooks.json` 只引用这个脚本路径。这样做的原因是：Codex 通过
shell 执行钩子命令并把整条命令再包一层引号，`cmd.exe` 会剥掉外层引号并截断最后
一个参数，多参数内联命令因此无法启动。脚本路径不受该行为影响。

信任要求：

- `hooks.json`（用户层）里的钩子需要在 Codex `/hooks` 中审核并信任后才会执行；
  CLI 场景可用 `codex exec --dangerously-bypass-hook-trust` 跳过审核。
- 如果希望钩子无需审核且不随 `config.toml` 被 CC Switch 覆写，可以把同样的
  TOML 钩子声明放进系统层 `%ProgramData%\OpenAI\Codex\config.toml`：该层属于
  managed 配置，钩子默认受信任且始终启用。写这个文件需要管理员权限，并且它
  是机器级配置，卸载只能手动删除。
- 用户层钩子只有在命令字符串不变时才保持已信任状态；更换路径或参数后需要重新
  审核。

### 已知限制

固定 `hooks.json` 或更新钩子后，用 `codex exec` 观察到的执行情况不稳定：有时
报告 `Failed`，有时报告 `Completed` 但包装脚本没有产生任何副作用（可用
`codex exec --dangerously-bypass-hook-trust "..."` 时输出的 `hook:` 行判断，或
检查 `state/lifecycle-events.jsonl` 是否出现新记录）。因此：

- 在 Desktop 中确认一次真实提示词确实触发了路由修复（发送前故意把
  `config.toml` 指回 15721 即可复现）；
- 如果钩子没有执行，回退方式是显式执行
  `-Action repair`，或改用 `enable-automation` 的每分钟路由修复。

## 切换后真实探测

后台自动修复任务会记录活动路由指纹：

```text
model_provider + model + base_url + wire_api
```

只有指纹变化时才探测。探测内容固定为：

```text
Reply with exactly: bridge-probe-ok
```

探测后端：

- `cli`：`codex exec --ephemeral ... --json`；
- `app-server`：临时 `thread/start` + `turn/start`；
- `disabled`：只检测路由变化，不发起请求。

状态只保存：

- 是否成功；
- provider/model；
- 临时探测 thread ID；
- 错误分类；
- 检查时间和路由指纹。

不会保存响应正文、Authorization、API Key 或完整上游错误。

切换后的 Bridge 范围：

- `preserve`：不修改原策略；
- `next`：下一条会话请求命中一次；
- `all`：所有新请求继续命中。

## 分支续接

手动预览：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action branch-conversation `
  -ConversationId "<conversation-id>" `
  -TargetProvider custom `
  -BranchBackend app-server
```

确认创建：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action branch-conversation `
  -ConversationId "<conversation-id>" `
  -TargetProvider custom `
  -BranchBackend app-server `
  -ApplyOperation
```

后端选择：

- `app-server`：读取最后一个已完成 turn，调用 `thread/fork(lastTurnId=...)`；
- `cli`：调用 `codex exec fork`，会发送一次续接提示并返回新 thread ID。

`app-server` 分支不会把正在执行的 turn 复制进新分支；`cli` 后端会额外发起一次
真实续接请求。两者都不会删除或覆盖原会话。

列出分支：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action list-branches
```

记录文件包含源/目标会话 ID、标题、工作目录、目标 provider/model 和时间，不保存
消息正文。

## 恢复

钩子安装前会备份到：

```text
~/.codex/backups/codex-cross-provider-hooks/<timestamp>/
```

列出备份：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action list-hook-backups
```

恢复指定备份：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action restore-hook-backup `
  -HookBackupDirectory "<backup-directory>" `
  -ApplyOperation
```

生命周期策略写入前会保留 JSON 备份：

```powershell
.\scripts\Manage-CodexCrossProviderBridge.ps1 -Action list-policy-backups
.\scripts\Manage-CodexCrossProviderBridge.ps1 `
  -Action restore-policy-backup `
  -PolicyBackupFile "<policy-backup-file>" `
  -ApplyOperation
```

单会话迁移另有完整 rollout + SQLite 备份。分支是追加操作，删除或归档分支应在
Codex UI 中显式完成。
