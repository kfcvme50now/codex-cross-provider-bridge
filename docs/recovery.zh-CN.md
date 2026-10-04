# 恢复和升级

适用于 0.3.0。升级源码和切换实际运行的 Bridge 是两个步骤。

## 升级

1. 记录当前版本、监听端口和所选 provider，保留本机配置、数据库与 hooks 的恢复副本。
2. 在独立目录获取新版本、安装依赖并执行测试。
3. 等待活动请求和压缩完成。
4. 停止旧 Bridge，在新目录以相同的监听地址、上游地址和本机状态目录启动。
5. 重新安装 hooks，让 wrapper 指向新的仓库和解释器。
6. 重新打开或重启 Codex，验证真实回复、继续对话和压缩。

不要将运行中的 state、auth.json 或数据库复制进公开仓库。保留恢复副本，不用重新生成凭证。

## 管理入口

```powershell
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action backup
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action list-snapshots
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action status
```

恢复本机管理器的快照使用 `-Action restore -SnapshotId`。先检查快照对应的配置文件和服务任务，避免把另一实例的状态恢复到当前实例。

`restart` 不会自动切回默认 provider。只有明确调用 `restore-route` 才会应用 `config/codex-route-default.json`；示例档案的空 baseUrl 根据本次 BridgePort 生成官方入口。

## 服务无法连接

- 检查当前配置指向的地址是否监听，区分端口拒绝连接、TLS/系统代理、上游鉴权和上游模型错误。
- 前台窗口关闭会停止服务；需要保留服务时将窗口最小化。
- 不要杀死不属于本项目的端口进程；管理器会检查进程归属并拒绝干扰活动请求。
- 调整了端口或数据目录后，客户端配置和 hook wrapper 都需要对应更新。

## 历史和压缩

推荐策略修改请求副本与 live config，不迁移历史。分页历史的 byte offset 和原始 JSONL 存在对应关系，不能直接重序列化所有行来修复一个 provider 字段。

旧的迁移、消息 ID 修复、分支工具仅保留为显式维护入口。需要使用时先检查计划、备份完整受影响数据，验证客户端版本的历史结构。恢复既有备份之前也应保存当前可用状态。

不能把模型目录、HTTP 200 响应头或开始输出当作请求完成。以原生客户端 completed 事件、压缩记录和最终流完成为验证依据。
