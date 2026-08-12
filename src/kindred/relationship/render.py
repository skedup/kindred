"""Deterministic qualitative projection of the current Relationship profile."""

from kindred.relationship.models import RelationshipProfile

_ROLE_TEXT = {
    "unlabeled": "尚未在心里确认当前 user 的明确关系角色",
    "friend": "在心里将当前 user 视为朋友",
    "lover": "在心里将当前 user 视为恋人",
    "hostile": "在心里将当前 user 视为需要警惕的对抗对象",
}
_FACET_TEXT = {
    "trust": ("信任尚未形成", "信任有限", "信任较稳固", "信任很深"),
    "attachment": ("情感投入尚浅", "情感投入有限", "情感投入较深", "情感投入很深"),
    "attraction": ("吸引尚不明确", "吸引微弱", "吸引较清晰", "吸引强烈"),
    "friction": ("摩擦很少", "有些摩擦", "摩擦明显", "摩擦很强"),
}


def render_relationship_summary(profile: RelationshipProfile) -> str:
    """Describe only how ta currently sees user, without raw values or advice."""
    facets = "；".join(
        _FACET_TEXT[name][min(getattr(profile, name) // 25, 3)]
        for name in ("trust", "attachment", "attraction", "friction")
    )
    return f"【你对当前 user 的关系看法】\n你{_ROLE_TEXT[profile.declared_role]}。\n{facets}。"


__all__ = ["render_relationship_summary"]
