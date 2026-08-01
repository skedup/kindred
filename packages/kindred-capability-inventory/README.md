# kindred-capability-inventory

Kindred 的 Portable Inventory 查询能力。它只提供 `list_inventory`，通过
`inventory.choice_context.v1` 读取 Host 投影的私有名册与当前穿戴快照。

Catalog、Action 写权限、selector 解析、canonical `ItemSnapshot` 和 State/T3 提交仍归
Kindred Host 所有；本 package 不访问 Kindred 数据库或 State。
