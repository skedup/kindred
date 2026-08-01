"""两个 graph（tick / dream）共用的底座。

- ``_common``：round-trip 桥（ActDecision / State / Thought）+ ``NodeReturn`` 类型别名
- ``_errors``：节点契约异常（``NodeContractError`` 等）

这一层不属于任何单一 graph，故从 ``tick/`` 抽出独立成 ``_shared/``，避免
``tick`` 与 ``dream`` 互相 import 对方内部文件（earlier milestone 重构）。
"""
