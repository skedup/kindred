"""Public contract for partner-message recency and its Sense projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from kindred.graph.tick.projection.sense_prompt import _render_recent_contact
from kindred.llm.real_client import _SENSE_SYSTEM_PROMPT
from kindred.providers.recent_contact import RecentContactProvider


class _ContactDB:
    def get_watcher_cursor(self, *, session_key: str) -> tuple[int, int, int]:  # noqa: ARG002
        return (9_000, -1, 2)

    def get_latest_visible_contact_expression(
        self,
        **kwargs: Any,  # noqa: ARG002
    ) -> tuple[int, str]:
        return (9_000, "my_voice")

    def get_latest_visible_partner_expression(self, **kwargs: Any) -> int:  # noqa: ARG002
        return 1_000


def test_partner_message_age_does_not_use_newer_kindred_expression() -> None:
    provider = RecentContactProvider(cast(Any, _ContactDB()), "agent:main:test")

    context = provider.observe(now=datetime.fromtimestamp(11, tz=timezone.utc))

    assert context.recent_exchange_age_seconds == 2
    assert context.recent_actor == "kindred"
    assert context.recent_partner_message_age_seconds == 10
    assert _render_recent_contact(context.model_dump()) == (
        "【最近的联系】\n- 最近一次表达来自你，刚刚；对方最近一次发来消息，刚刚。"
    )


def test_recent_partner_message_softly_prefers_a_quiet_tick() -> None:
    assert "对方最近一次发来消息距今不超过 30 分钟" in _SENSE_SYSTEM_PROMPT
    assert "通常优先 act=false" in _SENSE_SYSTEM_PROMPT
    assert "不是冷却、发送 veto 或 Activity 隐藏规则" in _SENSE_SYSTEM_PROMPT
