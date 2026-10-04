# 生命周期 hooks

适用于 0.3.0。早期的自动历史迁移和自动探测不属于推荐策略。

## 推荐流程

先启动 Bridge，再安装 hooks：

```powershell
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action enable-compact-hook
```

这个入口设置 `officialBridgeEnabled=true`、`portableHistoryViaBridge=true`，使用 `repair-and-continue`，关闭自动分支和探测。随后安装 `SessionStart`、`UserPromptSubmit`、`PreCompact`。

独立生命周期管理器也可以配置策略：

```powershell
./scripts/Manage-CodexLifecycleHooks.ps1 -Action set-policy -OfficialBridgeEnabled true -PortableHistoryViaBridge true -PostSwitchProbeMode disabled -CompactMode repair-and-continue -Apply
./scripts/Manage-CodexLifecycleHooks.ps1 -Action install-hooks -Apply
```

- SessionStart 和 UserPromptSubmit 检查路由、服务就绪和历史别名。
- PreCompact 在推荐策略下继续压缩，不批量改写 rollout、数据库或模型历史。
- 已加载的客户端如果需要重新加载路由，仍可能要求重新打开对话；切换 provider 后重新启动 Codex 属于正常配置加载流程。
- 路由维护先备份再写入，检查并发改动。服务不可用时保留可恢复状态。

## 自定义配置

`CODEX_HOME`、`CC_SWITCH_DB`、`CODEX_BRIDGE_URL`、`CODEX_BRIDGE_UPSTREAM_URL` 可以在安装前设置。也可以向生命周期管理器传入 `-CodexHome`、`-ConfigPath`、`-BridgeUrl`、`-CcSwitchDatabase`、`-UpstreamUrl`。

安装器生成的 wrapper 会保存所选配置，不包含 API key。移动仓库、切换 Python 解释器或更改端口后重新运行安装器，更新 wrapper。生成的绝对路径只存在本机 wrapper 中，不是仓库源码。

Codex 是否接受 hooks 还取决于本机的 hooks 支持与信任配置。检查 `hooks.json` 和一次真实提示的本地状态；仅安装成功不能证明客户端已经执行 hook。

## 停用和恢复

```powershell
./scripts/Manage-CodexLifecycleHooks.ps1 -Action uninstall-hooks -Apply
./scripts/Manage-CodexLifecycleHooks.ps1 -Action list-backups
```

安装、更新和卸载均保留本机备份。恢复指定备份需要 `restore-hooks -BackupDirectory`；策略备份通过 `list-policy-backups` 和 `restore-policy-backup` 管理。

不要提交 hooks.json、wrapper、策略状态或备份。旧的 `repair-and-stop`、迁移和分支模式只供明确的人工维护场景使用，不能作为分页历史的默认自动修复手段。
