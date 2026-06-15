- CRM 后台操作审计日志。`text`=自然语言事件描述；`type`=事件分类（'documents'/'accounts'/'email'/…）
- 关联：`userId`→users.id（被操作的 CRM 客户）·`managerId`→managers.id（操作的后台员工，NULL=系统/客户自助行为）·`restUserId`→客户门户(rest)用户
- ⚠ **性能**：19M 行 / 4.3GB，`text` 无索引 —— **严禁裸 `WHERE text LIKE '%...%'`**（全表扫描秒级超时）。必须先用 `idx_createdAt` / `idx_userId` / `idx_type` 收窄行集，再在结果里匹配 `text`
- ⚠ **钱包地址审计**（出 U 风控常用）：钱包的创建/编辑记在 `type='documents'`，`text` 格式：
  `Created wallet #<walletId> [<address>]` / `Updated wallet #<walletId> [<address>]`（地址就在方括号里，walletId→wallets.id）
  **"Updated wallet" ≠ 地址被改**：同一 `#walletId` 的多条事件方括号地址通常一致（改的是状态/备注等其它字段）；**只有同一 walletId 跨事件方括号地址不同，才是真·改了收款地址**。判断"客户改没改提现地址"要比对方括号内容，不能只看有没有 Updated 行
