"""节点契约异常基类。

参考文档：

- docs/14-heart-graph.md §2 节点契约（每个节点对上游 state 的最低期望）
- ``sense_io.ColdStartError`` / ``sense_llm.SenseLlmContractError`` /
  ``act_llm.ActLlmContractError``（都继承本基类）

为什么集中
==========

各节点的契约异常子类结构 / 语义同质：都是「上游 state 缺字段或类型错时
raise，让 daemon / 集成测试可以语义化 catch」。集中到一个基类的好处：

1. ``except NodeContractError`` 可一把抓所有节点契约错——daemon 处理 graph
   内异常时不必逐个 ``except`` 不同子类
2. ``isinstance(e, NodeContractError)`` 在测试与日志聚合里更可读
3. 子类可继承基类共用的诊断行为（未来加 sentry tag / 节点名前缀 / 链路 id
   之类的钩子，改一处即可）
4. ``ColdStartError`` 是个特殊位的子类——它是「库为空」的语义化标记，daemon
   用它走 bootstrap 路径而不是真 "contract error"。但放在同一基类下，daemon
   可以 ``except ColdStartError`` 优先 + ``except NodeContractError`` 兜底

节点校验风格指南
================

节点里有两类校验对象，用不同风格：

**(一) 校验 LLM 顶层输出 → pydantic model（单一真相源）**

``sense_llm`` / ``act_llm`` 收到的 LLM 返回 dict，其顶层字段 / 类型 / 范围 /
交叉约束集中在 ``kindred.llm.schemas`` 的 ``SenseResponse`` / ``ActResponse``。
节点侧 ``_validate_sense_response`` / ``_validate_act_output_schema`` 是薄封装——
``Model.model_validate(out)`` 后把 ``ValidationError`` 包成对应的
``NodeContractError`` 子类（保 daemon ``except NodeContractError`` 兜底），不再手写
isinstance 校验。

   优点：schema 声明式、与项目其余 pydantic model 一致、可被 prompt 渲染复用为
   单一真相源；strict int / bool 直接拒 bool↔int / float 截断等「类型对语义错」。

   适用：**外部不可信输入的整包 schema 门**（LLM 返回 dict）。用 ``extra="ignore"``
   宽容真实模型夹带的额外字段，区别于 state 层 model 的 ``extra="forbid"``。

**(二) 校验上游 state 字段 → 手写 helper（两种风格）**

上游 state 是图内自产的 dict，按需取值，不建整包 model：

A. **多 helper 链式取值**（``sense_llm`` 的 ``_require_interior`` /
   ``_require_environment``）

    ::

        def _require_interior(next_state: dict[str, Any]) -> dict[str, Any]:
            interior = next_state.get("interior")
            if not isinstance(interior, dict):
                raise SenseLlmContractError(...)
            return interior

   优点：helper 名自带文档、可链式取值、错误信息可逐层附诊断上下文

   适用：**字段需要逐层取值参与后续逻辑**（例：取 interior → 取 thoughts →
   应用 thought_diff）

B. **表驱动批量校验**

    ::

        for key, typ in REQUIRED.items():
            if key not in d:
                raise SenseLlmContractError(...)

   优点：加字段只改表、错误模板统一、函数体短

   适用：**字段平铺只 raise 不取值**（节点不消费具体值，仅过门）

**默认选择**：

- LLM 顶层输出整包 → pydantic model（schemas.py）
- 上游 state 字段 ≤ 3 且需链式取值 → A 多 helper
- 上游 state 字段 > 3 或仅 raise 不取值 → B 表驱动
- 混合场景 → 拆两段：先批量过 schema 门，再 A 取关键字段

新增节点时按此指南选风格——避免风格不一致。
"""

from __future__ import annotations


class NodeContractError(RuntimeError):
    """节点契约错基类——上游 state 缺字段或类型错时 raise。

    各节点的具体子类继承本基类（``ColdStartError`` / ``SenseLlmContractError``
    / ``ActLlmContractError``），让 daemon / 集成测可以分层 catch：

        try:
            graph.invoke(initial_state)
        except ColdStartError:
            # 库为空 → bootstrap 路径
            ...
        except NodeContractError as exc:
            # 真契约错 → 拒收本 tick + 上报
            ...

    保持 ``RuntimeError`` 父类血统是为了向后兼容——历史上 ``ColdStartError``
    /``SenseLlmContractError`` / ``ActLlmContractError`` 都直接继承
    ``RuntimeError``，旧代码 ``except RuntimeError`` 仍能抓到这一族。
    """


__all__ = ["NodeContractError"]
