# 备份与恢复

恢复不是一次覆盖操作，而是一条可审计的快照链。

## 快照时机

脚本会在以下操作前自动创建快照：

- `pre-install`
- `pre-repair`
- `pre-restore`
- `manual`

每次快照包含：

- Codex `config.toml`
- CC Switch `settings.json`
- Bridge policy/status 文件
- Windows 计划任务 XML
- SHA-256 清单和来源路径

快照使用时间戳和随机 ID，不覆盖旧快照。`index.jsonl` 只追加。

## 恢复流程

1. 读取目标快照并校验 manifest。
2. 验证快照中的 TOML。
3. 为当前状态创建 `pre-restore` 快照。
4. 停止并移除当前 Bridge 计划任务。
5. 原子恢复 `config.toml`。
6. 如快照中存在计划任务 XML，则恢复该任务。
7. 恢复失败时自动回写 `pre-restore` 快照中的配置。

默认恢复 `pre-install` 快照。其他快照可以通过 ID 指定。

```powershell
.\scripts\Restore-CodexCrossProviderState.ps1 -ListSnapshots
.\scripts\Restore-CodexCrossProviderState.ps1
.\scripts\Restore-CodexCrossProviderState.ps1 -SnapshotId "<snapshot-id>"
```

CC Switch 设置默认只备份，不覆盖恢复；需要恢复时显式传入：

```powershell
.\scripts\Restore-CodexCrossProviderState.ps1 -RestoreCcSwitchSettings
```
