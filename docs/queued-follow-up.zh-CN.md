# Codex 队列消息编辑失败

## 症状

撤销、编辑或重新提交 queued follow-up 后，Codex Desktop 可能提示：

```text
App-server queued follow-up no longer exists
```

出现后，同一线程可能持续拒绝新的 Queue 或 Steer 提交。

## 是否与 CC Switch 或 Bridge 有关

无关。

本机日志显示：

```text
thread/queue/add     成功
thread/queue/delete  成功
thread/queue/list    成功
Composer submit      失败
```

App-server 连接没有断开，流式响应和其他 RPC 仍在正常工作。CC Switch 和
本地 Bridge 位于模型请求链路上，不处理 Desktop 的 `thread/queue/*` RPC。

## 根因

Codex Desktop 的编辑流程把 `beginEdit` 映射为队列 `remove`。

随后：

1. `thread/queue/delete` 成功。
2. 渲染层从本地 queue cache 删除该条目。
3. Composer 仍保留原始 `editPosition.messageId`。
4. 重新提交时，enqueue 使用旧 ID 查找队列项目。
5. 查不到旧 ID，便在发送 `thread/queue/add` 或 `thread/queue/update` 前抛出错误。

这是 Desktop 渲染层与 app-server 队列状态不一致，不是 provider 路由问题。

## 上游状态

相关 issue：

- https://github.com/openai/codex/issues/44781
- https://github.com/openai/codex/issues/45019
- https://github.com/openai/codex/issues/45209
- https://github.com/openai/codex/issues/45398
- https://github.com/openai/codex/issues/45430

维护者在 #44781 中确认：

> The problem is understood. We have a fix in place, and it will be included in
> the next release.

## 当前可行恢复方式

不要反复重试同一个 stale queue item，也不要修改队列数据库。

1. 保留未发送文本。
2. 切换到另一个对话，再切回当前对话。
3. 将文本作为新的 Queue/Steer 消息提交一次。
4. 如果消息已经在正文中成功出现，不要再次提交，以免重复发送。

部分用户报告切换对话后可立即恢复；也有用户需要在现有线程外新建线程继续。

## 不推荐的操作

- 不要直接删除或修改 `~/.codex` 队列数据库。
- 不要停止或重启 CC Switch 本地路由来修复该错误。
- 不要重启、解包或重新签名 Codex Desktop 应用包。
- 不要在未知 delivery 状态下盲目重试，以避免重复提交。
