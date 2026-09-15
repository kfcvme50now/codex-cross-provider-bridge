# 问题调查记录

## 症状

切换 provider 后，Codex 可能出现两类不同问题：

1. 历史线程可见但无法打开，提示 provider 未定义。
2. 历史线程可以打开，但继续发送消息时上游返回 400。

这两类问题不能混为一谈。

## 第一类：provider 定义悬空

会话元数据仍保存旧 provider ID，例如：

- `custom`
- `cc-switch-official`

CC Switch 切换 provider 时可能整份覆写 `config.toml`，删除仍被历史线程引用的
provider 定义。结果是：

```text
historical thread -> model_provider = <missing-id>
live config.toml  -> model_providers.<missing-id> absent
```

修复方式是在不修改历史文件的前提下，为当前活动 provider 建立兼容别名，使两个
历史 provider ID 都能解析到同一条合法路由。

## 第二类：历史请求不可移植

Responses 请求不只包含可见消息，还可能包含：

- provider 私有 item ID
- `previous_response_id`
- `reasoning.encrypted_content`
- 不可跨 provider 重放的 reasoning 内容
- `item_reference`

复现时，第三方历史交给另一个 Responses 端点后返回：

```text
400 invalid_request_error
param: input[4].id
code: invalid_value
```

根因是旧历史中的 ID 由原 provider 创建，新 provider 不接受其格式或服务端归属。

## 第三类：远程压缩使用旧 provider

压缩失败时可能出现：

```text
Error running remote compact task:
The 'deepseek-flash' model is not supported when using Codex with a ChatGPT account.
```

历史记录中的持久化运行时设置为：

```text
model_provider_id = "openai"
model = "deepseek-flash"
```

远程压缩读取的是持久化 provider，而不是当前 `config.toml` 中的 provider。
请求因此被发往 ChatGPT 后端，第三方模型被拒绝。

该问题不能仅靠请求清理解决，需要为指定会话迁移持久化 provider 状态：

- rollout `session_meta`
- rollout `thread_settings_applied.model_provider_id`
- `state_5.sqlite.threads.model_provider`

迁移工具默认只预览，使用 `-ApplyMigration` 后才写入，并在写入前备份 rollout
和完整 SQLite 数据库。

## 第四类：请求已转发，但上游没有任何响应

本地日志中可观察到：

```text
Codex:     POST /v1/responses -> 200 OK (text/event-stream) 之后没有任何流事件
CC Switch: [Codex] >>> 请求目标: <上游地址>，但没有对应的用量记录
上游:      没有对应的请求或计费记录
```

手动 `/compact` 与普通对话现象一致：界面一直等待，直到用户中止。这类失败不是
400/500 这类可分类错误，而是上游接受了请求之后不再返回数据。CC Switch 已经把
`200` 和 SSE 响应头发给了 Codex，因此 Codex 只能等待；本地各层都不会记录这次
失败，只有“什么都没有发生”这一现象。

判定方式：

1. Bridge 状态中的 `last_request_outcome`（`upstream-idle-timeout` /
   `upstream-headers-timeout`）与 `in_flight_request_count`；
2. CC Switch 日志中 `[Codex] >>> 请求目标` 的行数多于 `proxy_request_logs`
   的行数；
3. Codex app-server 日志中 `Request completed` 之后没有后续流事件。

处置：Bridge 的停滞阈值会中止这类请求并写入状态记录；之后直接重试，或切换到
其他 Provider。上下文窗口与自动压缩阈值来自 CC Switch 的模型目录
（`model_catalog_json`），例如 1,000,000 上下文配合
`auto_compact_token_limit = 500000` 意味着大约 52% 才会触发自动压缩——阈值附近
出现失败时，先确认是否已经跨过该阈值，而不是假设压缩一定已经发生。

## 自动修复的安全边界

后台自动修复只在以下条件全部成立时工作：

- 当前活动路由指向 CC Switch 的 `127.0.0.1:15721/v1`；
- 当前 provider 不是官方 OpenAI/GPT 路由；
- 历史线程 provider 为 `openai`，但模型不是官方 `gpt-*` / `codex*` /
  `o1-*` / `o3-*` / `o4-*`；
- target provider 已存在于 `config.toml`；
- 会话达到空闲阈值，且默认不是 Bridge 最近记录到的当前会话。

正常 ChatGPT 登录后的 `openai + gpt-*` 会话不会进入候选。每条迁移仍然单独备份
rollout 和完整 SQLite，失败自动回滚。

## 生命周期钩子能做什么、不能做什么

官方文档确认：

- `PreCompact` 可匹配 `manual` / `auto`；
- `PreCompact` 返回 `continue: false` 可以阻止压缩；
- `SessionStart` 会在 `startup`、`resume`、`clear`、`compact` 时触发；
- app-server `thread/fork` 支持 `lastTurnId`、`modelProvider`、`model` 和
  `ephemeral`。

官方文档：

- https://developers.openai.com/codex/hooks
- https://developers.openai.com/codex/app-server

但是，钩子无法可靠改变当前 turn 已经捕获的 provider/model。因此自动修复不能
假设“改完持久化配置，当前压缩就一定改用新 provider”。更安全的策略是：

1. 风险会话先停止压缩或创建分支；
2. 修复持久化 provider 元数据；
3. 下一次请求或重新执行压缩时继续；
4. 需要使用真实请求确认时，再执行固定内容的最小探测。

## 修复层

### 历史会话

历史处理不直接修改 JSONL 或 SQLite：

1. 读取历史索引，确认目标会话使用的 provider ID。
2. 保证该 provider ID 在 `config.toml` 中有兼容定义。
3. 继续会话时由 Bridge 清理请求副本中的不可移植状态。

### 新会话

新会话从第一次请求起经过 Bridge。Bridge 会记录：

- 会话 ID
- 会话标题
- 工作目录
- provider
- 是否需要修复
- 修复是否命中
- 上游 HTTP 状态

## 相关社区讨论

- `farion1231/cc-switch#5398`
- `farion1231/cc-switch#5922`
- `farion1231/cc-switch#5974`
- `farion1231/cc-switch#6340`
- `farion1231/cc-switch#6503`
- `farion1231/cc-switch#6658`
- `farion1231/cc-switch#7257`
- `farion1231/cc-switch#5536`（远程压缩协议转换 PR，仍打开）
- `farion1231/cc-switch#5735`（统一历史/provider takeover PR，仍打开）
- `farion1231/cc-switch#7147`（真实最小模型请求验证器 PR，仍打开）
