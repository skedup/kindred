"""Kindred web —— ta 的生活可视化只读层。

═══ 这是什么 ═══

一个**只读**的 HTTP 视图层，把心（daemon）写进 SQLite 的 tick 数据，
整形成两类 user 各自关心的契约：

- **叙事视图**（companion user）：ta 此刻在做什么、什么心情、最近发生了什么。
  数值退到背景，主角是自然语言（note / activity.desc / location）。
- **指标视图**（engineer user）：7 维 needs + 4 维 affect + body/mood/inner_pulse
  + significance + 决策链（act_decision / act_result）。

同一行 tick 既有 ``note`` 又有 8 层 state——两类视图共享一份数据，不分库。

═══ 铁律：只读 ═══

本层**绝不**反向写库。可视化是观察 ta 的窗，不是控制 ta 的手柄。
所有路由只调 ``KindredDB`` 的读方法（get_state_latest / get_episodes /
get_recent_ticks），不碰 transaction / insert / upsert。

═══ 架构定位 ═══

::

    心(daemon) ─写─> SQLite ─读─> web 读取层(本包) ─JSON─> 前端(v1) / 桌面壳(以后)

前端只是这份 JSON 契约的一种皮。以后 Tauri 套壳成桌面组件时，数据层零改动。
"""

from kindred.web.app import create_app

__all__ = ["create_app"]
