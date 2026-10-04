# 作用范围与状态

适用于 0.3.0。

## 请求范围

- `all`：持续处理原生 Codex Responses 请求。
- `next`：只处理下一条匹配请求。
- `conversation`：只处理指定会话 ID，或锁定下一条匹配会话。

浏览器来源不会消耗 Codex 策略范围，也不进行兼容清理、provider 选择和重试。专用官方入口拒绝浏览器来源。

官方请求通常保留原生 reasoning；外来合成状态或窄范围的 item 状态错误触发兼容处理。第三方 provider 如需先保留自身状态，可显式传入 `--preserve-state-provider`。瞬态重试另用 `--retry-provider`，默认不绑定任何本机 provider ID。

## 推荐自动维护

同一个 Bridge 进程中的路由维护线程检查 CC Switch 写入后的稳定配置。仅维护已知官方路由与当前受管理的第三方路由，不覆盖未知外部 provider 配置。原生 CC Switch 上游模板的真实地址保持不变。

推荐 lifecycle policy：

```json
{
  "officialBridgeEnabled": true,
  "portableHistoryViaBridge": true,
  "compactRepairMode": "repair-and-continue",
  "autoBranchEnabled": false,
  "postSwitchProbeMode": "disabled",
  "postSwitchScope": "preserve"
}
```

完整示例见 `config/lifecycle-policy.example.json`。该模式不自动迁移会话或创建分支，不需要额外自动化进程。

## 状态检查

```powershell
./scripts/Manage-CodexCrossProviderBridge.ps1 -Action status
```

`/__bridge/info` 返回本机进程 ID、活动请求数和维护功能状态。status、runtime log、request-log 用于区分路由、客户端和上游失败；HTTP 200 响应头不能代替完整流完成。

状态文件可能包含会话 ID、标题和工作目录。这些本机内容不属于公开版本，不要提交，也不要直接粘贴到公开 issue。

## 旧的自动化入口

`enable-automation` 与历史迁移入口仍保留，推荐部署不启用。尤其不要对分页历史执行未经验证的批量迁移。自动维护能力由当前 lifecycle policy 决定，不能仅凭旧脚本默认值判断正在执行哪些修复。
