# 动态路由评估

## 结论

CC Switch 3.20.3 可以在本地路由服务运行时热切换 Provider，Codex 不需要重启；
但这个切换是 Codex 应用级的全局状态，不是单请求、单会话或单 subagent 的选择器。
当前源码中的 `RequestContext::new` 调用 `ProviderRouter::select_providers`，后者读取
Codex 的 effective current provider；请求体里的 `model` 只用于模型映射和统计，
不会选择另一张 Provider 卡片。

因此：

- 手工切换 Provider：可热切换，无需重启 Codex 或 CC Switch；
- 按请求选择 Provider：当前原生实现不支持；
- 按模型改写上游模型：同一 Provider 内支持，但不能借此跨 Provider；
- 故障转移：按全局队列和熔断器选择，不是按任务语义动态选择；
- 通过每条请求前改写 `is_current` 或调用热切换模拟路由：并发时存在串线竞态，
  不应使用。

官方说明和对应源码：

- <https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.2-routing.md>
- <https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/provider_router.rs>
- <https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/proxy/handler_context.rs>

## 安全的实现方向

### 方案 A：独立前置 Router（推荐）

保持 CC Switch 负责配置管理、日志与手工热切换，让一个常驻 loopback Router 按请求
中的精确模型名选择上游：

```text
Codex / subagent
  -> Cross-Provider Bridge（历史兼容、健康门禁、短路）
  -> model router（精确 allowlist、会话固定、推理强度策略）
  -> Provider A / Provider B
```

Router 应满足：

1. 只监听 loopback，未知模型在本地失败，不能回落到任意 Provider；
2. Provider、模型和推理强度使用显式 allowlist；
3. 会话首次选路后可固定 Provider，除非用户明确切换；
4. 凭据只由 Router/CC Switch 本地进程读取，不进入命令行、日志或仓库；
5. `routing_disabled=true` 的 Provider 永远不可被自动选择；
6. 连接异常、空响应、错误状态和流停滞都进入有界短路，并返回可见错误；
7. 选择记录只保存 Provider ID、模型、原因和时间，不保存请求正文。

### 方案 B：在 CC Switch 源码内增加请求级规则

技术上可行，但应直接修改 `ProviderRouter::select_providers` 的输入，使其接收已验证的
request model / session ID / reasoning effort，并返回本次请求专属的 `Provider`，而
不是修改全局 current provider。这样可以继续复用 CC Switch 的凭据边界、协议转换、
健康统计和熔断器。

需要新增的关键约束：

- 路由规则快照必须在单次请求开始时冻结；
- 同一会话默认固定 Provider，避免 Provider 私有状态跨边界；
- failover 只能进入协议、模型和鉴权兼容的目标；
- UI 当前 Provider 与实际单请求 Provider 要分开展示；
- 日志必须记录规则命中原因，不能只显示全局 current provider。

这属于 CC Switch fork 级改动，维护和回归成本明显高于独立 Router。

### 方案 C：只对子代理选择模型

Codex 自定义 agent 可以单独指定 `model` 和 `model_reasoning_effort`，全局也有
`default_subagent_model` 与 `default_subagent_reasoning_effort`。因此，只要前置 Router
按模型名选路，就能让主任务保持 Default，而 subagent 使用另一个 Provider，无需
重启 Codex。显式 spawn 参数仍应优先于默认值。

官方配置说明：

- <https://learn.chatgpt.com/docs/agent-configuration/subagents>
- <https://learn.chatgpt.com/docs/config-file/config-reference>

## 可复用项目

### OpenCodex

<https://github.com/lidge-jun/opencodex>

- 常驻本地代理，支持 `provider/model` 和按模型模式自动匹配；
- Codex App 模型选择器、推理强度和 subagent 模型注入均有现成支持；
- 未知/错误配置可本地失败，配置支持环境变量引用；
- MIT，当前维护活跃，功能覆盖最接近本需求；
- 已知限制：其文档明确指出 native parent 调用 routed child 时，任务正文可能以后端
  加密形式到达并丢失；跨 Provider subagent 需要使用其建议的 v1 路径验证。

这是最值得先在隔离配置中验证的候选，但不应直接接管现有 CC Switch 配置或复用
OpenCode 的过期订阅。

### codex-model-router

<https://github.com/ustas-eth/codex-model-router>

- 小型 Go Responses API Router，按精确模型名 allowlist 路由；
- 支持 OpenAI 订阅与 Z.AI 的窄场景，并验证过 v1 混合 Provider subagent；
- 未知模型本地失败、loopback 监听、MIT；
- 代码和功能面较小，适合作为最小实现参考，但维护规模和 Provider 覆盖远小于
  OpenCodex。

### codex-router

<https://github.com/duolahypercho/codex-router>

- 覆盖多个 Provider、Codex 客户端面和有界重试；
- 更偏完整路由平台，安装、凭据与目录管理的引入成本更高；
- 适合需要统一管理多种客户端时评估，不适合作为本 Bridge 的小依赖直接嵌入。

### CodeRouter / claude-code-router

- <https://github.com/zephel01/CodeRouter>
- <https://github.com/musistudio/claude-code-router>

两者都有任务或模型路由能力，但前者的 Codex 接入以 Chat Completions/外部 agent
编排为主，后者核心面向 Claude Code。它们可用于架构参考，不是当前 Responses
Bridge 的首选替代。

## 本项目当前选择

本版本只实现不会引入 Provider 串线的基础能力：

- Default 与 AnyRouter GPT-6 首次保留已有 Provider 状态；
- 明确的可移植性错误才触发一次清理重放；
- 禁用或不健康 Provider 在上游调用前失败；
- AnyRouter 的异常返回、空响应、断流和超时进入可见错误与本地短路；
- OpenCode 配置保留，但订阅过期标记会阻止自动路由。

真正的模型/推理强度动态选路应放在独立 Router 或 CC Switch 的请求级 Provider
选择层，不能通过反复切换全局 Provider 状态实现。
