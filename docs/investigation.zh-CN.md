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
