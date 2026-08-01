"""dream graph 所有终态的落点 —— ``Step 5.land`` 路由常量。

参考文档：docs/14 §2.4.2、docs/11 §6.4（闸门失败 = 整次回滚）。

**D6.8b 拓扑变更**：原本 Step 4 闸门出口是条件边（pass/warn → land；block → END
整次回滚）。但 ``should_dream`` 幂等补偿（D6.8）需要 index 覆盖**所有终态**——若
block 走 END 不写 index，``should_dream`` 会判「这天没做」→ 明天重跑 → 又 block →
死循环。所以 **block 也走 land**（写 index trace，但 land Step 0 早返不改灵魂）。

于是 Step 4 出口恒走 land——**条件边退化为普通边**（build.py 已改 ``add_edge``）。
本模块不再提供路由谓词，只保留路由目标常量 ``ROUTE_STEP5_LAND``（build / land 共享）。

block 不 apply 的安全保证现收紧在 **land Step 0**（``_is_blocked`` 显式判 verdict，
不靠 surviving 空判——fail-closed 降级的 block 可能 blocked 字段缺失使 surviving 非空）。
"""

from __future__ import annotations

from typing import Final, Literal

# 路由目标常量（build.py add_edge 目标 + land 节点名共享）
ROUTE_STEP5_LAND: Final[Literal["Step 5.land"]] = "Step 5.land"
