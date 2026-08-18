"""web service 层 —— 存储形状 → 对外契约 的整形。

═══ 职责 ═══

唯一职责：把 ``KindredDB`` 读方法返回的**存储 dict**（8 层 JSON 列原样 loads）
翻译成 ``contracts.py`` 里的**对外契约**。

- 这一层吸收所有 schema 细节（列名、嵌套路径），让前端契约稳定。
- 全程**容忍缺失**：空库 / nullable 字段缺失 / 字段类型漂移，都降级成默认值
  而不是抛异常。可视化读到半截数据也要能渲染，不能 500。

═══ 不变量：每个外部字段都走 typed helper（review N-3~N-5 同类错误收口）═══

脏数据降级不能靠「造型强转」（``str()`` / ``bool()`` / Python truthiness），
那会把类型漂移**包装成看似真实的产品语义**（``str(789)``="789" /
``bool("false")``=True / True→1）。

**铁律**：从 tick dict 取的每个值，必须过下面某个 typed helper（它们都是
「只接受目标类型，其余降级」，绝不造型强转）：

- ``_safe_str`` / ``_optional_str``：字符串（只真 str）
- ``_safe_float``：浮点（``_is_number``，排 bool）
- ``_is_int`` / ``_is_number``：整数 / 数值（都排 bool）
- ``_safe_bool``：布尔（只真 bool）

新增对外字段时：**先问「这个值走哪个 typed helper」**，不准直接透传或造型强转。

═══ 只读保证 ═══

本层只接收已读出的 dict，自己不持有 db 连接、不发起任何写。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import threading
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, TypeGuard

from kindred.activity import list_registered_actions
from kindred.capability_host.artifacts import (
    ArtifactStore,
    ArtifactStoreError,
    CommittedArtifactMember,
)
from kindred.db.artifacts import ArtifactCommitRow
from kindred.web.contracts import (
    ArtifactDetailResponse,
    ArtifactKind,
    ArtifactMemberAvailability,
    ArtifactMemberRole,
    ArtifactMemberView,
    Gauge,
    InteriorHistoryPoint,
    InteriorHistoryResponse,
    InteriorHistoryValues,
    Intimate,
    MetricsView,
    NarrativeView,
    NowResponse,
    Outfit,
    StreamItem,
    StreamResponse,
    ThoughtView,
    VisualActionV1,
    VisualStateEmptyV1,
    VisualStateReadyV1,
    VisualStateV1,
)
from kindred_capability_sdk import ArtifactDescriptor

if TYPE_CHECKING:
    from kindred.db import KindredDB

# interior.needs 的 7 个维度（与 state/interior.py::Needs 对齐）
_NEEDS_KEYS = (
    "hunger",
    "energy",
    "fatigue",
    "comfort",
    "social",
    "stimulation",
    "aesthetic",
)
# interior.affect 的 4 个维度（与 state/interior.py::Affect 对齐）
_AFFECT_KEYS = ("stress", "focus", "arousal", "clarity")

_TEXT_MEDIA_TYPES = frozenset({"text/plain", "text/markdown"})
_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_PROFILE_LABELS = {
    "kindred.compose.outbound.v1": "一段文字",
    "kindred.draw.image.v1": "图片",
}
_ROLE_ORDER = {"title": 0, "content": 1, "image": 2, "file": 3}
_TEXT_LIMIT, _IMAGE_LIMIT = 64 * 1024, 8 * 1024 * 1024
_VISUAL_LOG = logging.getLogger("kindred.web.visual_state")
_VISUAL_SOURCE_DOMAIN = b"kindred.visual-source.v1\0"
_RESERVED_VISUAL_ACTIONS = frozenset({"settle"})


ArtifactProjection = tuple[ArtifactDetailResponse, tuple[str, ...]]


class VisualStateProjectionError(RuntimeError):
    """Latest committed row cannot satisfy the strict V1 projection contract."""


def derive_visual_source_id(install_id: str) -> str:
    """Derive a stable opaque, non-secret source id from a committed install id."""
    if not install_id.strip():
        raise VisualStateProjectionError("resident install identity is unavailable")
    digest = hashlib.sha256(_VISUAL_SOURCE_DOMAIN + install_id.encode("utf-8")).hexdigest()
    return f"install:{digest[:32]}"


class VisualStateProjector:
    """Project latest committed state and cache its motion boundary by revision."""

    def __init__(
        self,
        install_id: str,
        *,
        registered_actions: Collection[str] | None = None,
    ) -> None:
        self.source_id = derive_visual_source_id(install_id)
        actions = list_registered_actions() if registered_actions is None else registered_actions
        self._actions = frozenset(actions) | _RESERVED_VISUAL_ACTIONS
        self._cache_lock = threading.Lock()
        self._cached_revision: int | None = None
        self._cached_motion_start: int | None = None

    def project(self, db: KindredDB) -> VisualStateV1:
        """Build an empty or ready snapshot from one read-only database handle."""
        latest = db.get_state_latest()
        if latest is None:
            return VisualStateEmptyV1(source_id=self.source_id)
        revision = latest.get("id")
        committed_at = latest.get("ts")
        if not _is_int(revision) or revision < 1 or not isinstance(committed_at, str):
            raise VisualStateProjectionError("latest tick identity is malformed")
        activity = _as_dict(latest.get("activity"))
        raw_step = activity.get("step")
        action: VisualActionV1 | None = None
        if isinstance(raw_step, str) and raw_step in self._actions:
            action = VisualActionV1(name=raw_step)
        elif raw_step is not None:
            _VISUAL_LOG.info("visual_state_projection diagnostic=invalid_action_semantic")
        motion_start = self._motion_start(db, revision)
        return VisualStateReadyV1(
            source_id=self.source_id,
            revision=revision,
            committed_at=committed_at,
            motion_instance_id=f"tick:{motion_start}",
            action=action,
        )

    def _motion_start(self, db: KindredDB, revision: int) -> int:
        with self._cache_lock:
            if self._cached_revision == revision and self._cached_motion_start is not None:
                return self._cached_motion_start
            start = db.get_motion_instance_start_id(latest_tick_id=revision)
            if start is None:
                _VISUAL_LOG.info("visual_state_projection diagnostic=malformed_history")
                start = revision
            self._cached_revision = revision
            self._cached_motion_start = start
            return start


def encode_artifact_cursor(tick_id: int, artifact_ordinal: int) -> str:
    return base64.urlsafe_b64encode(f"{tick_id}:{artifact_ordinal}".encode()).decode().rstrip("=")


def decode_artifact_cursor(value: str) -> tuple[int, int]:
    if not value or len(value) > 64:
        raise ValueError("invalid artifact cursor")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        tick_text, ordinal_text = raw.split(":", maxsplit=1)
        tick_id = int(tick_text)
        ordinal = int(ordinal_text)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("invalid artifact cursor") from exc
    if tick_id < 1 or ordinal < 0 or encode_artifact_cursor(tick_id, ordinal) != value:
        raise ValueError("invalid artifact cursor")
    return tick_id, ordinal


def _member_projection(
    member: CommittedArtifactMember,
) -> tuple[ArtifactMemberRole, str, ArtifactMemberAvailability]:
    media_type = member.media_type
    if member.path == "content.md" and media_type == "application/octet-stream":
        media_type = "text/markdown"
    if member.path == "title.txt" and media_type in _TEXT_MEDIA_TYPES:
        role: ArtifactMemberRole = "title"
    elif media_type in _TEXT_MEDIA_TYPES:
        role = "content"
    elif media_type in _IMAGE_MEDIA_TYPES:
        role = "image"
    else:
        role = "file"
    oversized = member.bytes > (_IMAGE_LIMIT if role == "image" else _TEXT_LIMIT)
    if not member.available:
        availability: ArtifactMemberAvailability = "unavailable"
    elif role == "file" or oversized:
        availability = "unsupported"
    else:
        availability = "available"
    return role, media_type, availability


def project_committed_artifact(
    row: ArtifactCommitRow,
    store: ArtifactStore,
) -> ArtifactProjection:
    descriptor = ArtifactDescriptor(row.artifact_ref, row.producer, row.profile)
    try:
        inspected = store.inspect_committed(descriptor)
    except ArtifactStoreError:
        inspected = ()
        unavailable = True
    else:
        unavailable = False
    classified = [(member, *_member_projection(member)) for member in inspected]
    classified.sort(key=lambda value: (_ROLE_ORDER[value[1]], value[0].path))
    projected: list[tuple[ArtifactMemberView, str]] = []
    for ordinal, (member, role, media_type, availability) in enumerate(classified):
        projected.append(
            (
                ArtifactMemberView(
                    member_ordinal=ordinal,
                    role=role,
                    media_type=media_type,
                    bytes=member.bytes,
                    availability=availability,
                ),
                member.path,
            )
        )
    roles = {view.role for view, _ in projected if view.availability == "available"}
    has_text, has_image = bool(roles & {"title", "content"}), "image" in roles
    kind: ArtifactKind = (
        "mixed"
        if has_text and has_image
        else "text"
        if has_text
        else "image"
        if has_image
        else "files"
    )
    return (
        ArtifactDetailResponse(
            tick_id=row.tick_id,
            artifact_ordinal=row.artifact_ordinal,
            ts=row.created_at,
            activity_name=row.activity_name,
            kind=kind,
            label=_PROFILE_LABELS.get(row.profile, "作品"),
            member_count=len(projected),
            total_bytes=sum(view.bytes for view, _ in projected),
            availability=(
                "unavailable"
                if unavailable
                else "partial"
                if any(view.availability == "unavailable" for view, _ in projected)
                else "available"
            ),
            members=[view for view, _ in projected],
        ),
        tuple(path for _, path in projected),
    )


def read_projected_member(
    projection: ArtifactProjection,
    *,
    member_ordinal: int,
    row: ArtifactCommitRow,
    store: ArtifactStore,
) -> tuple[bytes, str]:
    detail, paths = projection
    if member_ordinal < 0 or member_ordinal >= len(paths):
        raise ArtifactStoreError("artifact member is not available")
    member = detail.members[member_ordinal]
    path = paths[member_ordinal]
    if member.availability != "available":
        raise ArtifactStoreError("artifact member is not available")
    content = store.read_committed(
        ArtifactDescriptor(row.artifact_ref, row.producer, row.profile),
        path,
        size_limit=_IMAGE_LIMIT if member.role == "image" else _TEXT_LIMIT,
    )
    if member.role in {"title", "content"}:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactStoreError("artifact text is not valid UTF-8") from exc
        return content, f"{member.media_type}; charset=utf-8"
    return content, member.media_type


def _as_dict(value: Any) -> dict[str, Any]:
    """容错取 dict：非 dict（None / 漂移）一律降级成空 dict。"""
    return value if isinstance(value, dict) else {}


def _str_list(value: Any) -> list[str]:
    """容错取「字符串数组」：非 list 降级成空；逐项只留**真 str 且非空**。

    同 typed-helper 铁律：不用 ``str(x)`` 强转，防脏数据（数字/dict）被包装成
    看似真实的配饰/人名。用于 accessory / with_whom / others / bag_items。
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _item_display_name(value: Any) -> str:
    """新 snapshot 取 name，旧字符串原样展示，其他形状安全留白。"""
    return _safe_str(value.get("name")) if isinstance(value, dict) else _safe_str(value)


def _item_name_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name for item in value if (name := _item_display_name(item))]


def _safe_str(value: Any, default: str = "") -> str:
    """容错取非空字符串：非 str（None / int / dict 漂移）降级成 default。

    为什么不直接 ``str(value)``（review N-3）：比如 ``str(789)`` 出 ``"789"``、
    ``str({...})`` 出 ``"{...}"``——把脏数据强转成产品文本反而误导。
    只接受真 str，其余一律降级成空（可视化宁可留白也不呈脏数据）。
    """
    return value if isinstance(value, str) else default


def _optional_str(value: Any) -> str | None:
    """容错取可空字符串：真 str 原样返回，其余（None / 非 str 漂移）返回 None。

    用于语义上允许「没有」的字段（step / note / ts / trigger_source）——
    不该让 ``str | None`` 契约模型替 service 做最后一道容错（review N-3）。
    """
    return value if isinstance(value, str) else None


def _is_int(value: Any) -> TypeGuard[int]:
    """是否真 int——**排除 bool**（review N-4）。

    Python 里 ``bool`` 是 ``int`` 子类，``isinstance(True, int)`` 为 True。若不排除，
    脏数据 ``needs.hunger=True`` / ``significance=True`` 会被当成 ``1`` 输出——这不是
    降级，是把类型漂移包装成了真实指标。

    返回 ``TypeGuard[int]`` 让 mypy 在真分支里把类型收窄为 int。
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> TypeGuard[int | float]:
    """是否真数值（int / float）——**排除 bool**（review N-4）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _safe_bool(value: Any, default: bool = False) -> bool:
    """容错取 bool：**只接受真 bool**，其余一律降级成 default（review N-5）。

    为什么不用 ``bool(value)``：Python truthiness 太宽松——``bool("false") == True``、
    非空 dict/list 也是 True。脏数据 ``user_present="false"`` 会让 companion 视图显示
    user 在场，这不是降级是把类型漂移转成产品语义。只接受真 bool。
    """
    return value if isinstance(value, bool) else default


def _gauge_from(raw: Any) -> Gauge:
    """{value, description} → Gauge，容忍缺失/越界。"""
    d = _as_dict(raw)
    value = d.get("value", 0)
    # 排除 bool（review N-4）：True/False 不该被当成 1/0 指标。
    if not _is_int(value):
        value = 0
    # clamp 进 0-100，防止脏数据撑爆 pydantic 校验
    value = max(0, min(100, value))
    return Gauge(value=value, description=_safe_str(d.get("description")))


def _pick_ints(raw: Any, keys: tuple[str, ...]) -> dict[str, int]:
    """从 dict 里挑指定 key 的 int 值，缺失/非 int 跳过。"""
    d = _as_dict(raw)
    out: dict[str, int] = {}
    for k in keys:
        v = d.get(k)
        if _is_int(v):
            out[k] = v
    return out


def _build_outfit(raw: Any) -> Outfit:
    """embodiment dict → Outfit（只外观层；私密层 bra/panties/socks 走 _build_intimate）。"""
    e = _as_dict(raw)
    return Outfit(
        top=_item_display_name(e.get("top")),
        bottom=_item_display_name(e.get("bottom")),
        outer=_item_display_name(e.get("outer")) or None,
        shoes=_item_display_name(e.get("shoes")),
        accessory=_item_name_list(e.get("accessory")),
        makeup=_optional_str(e.get("makeup")),
    )


# 未揭示时的俏皮拒绝语（用户点名要这种调调感）——隐私边界有 ta 的人格。
_INTIMATE_TEASES: tuple[str, ...] = (
    "不许耍流氓 「゜」",
    "想看内衣？看你表现 ゜",
    "这一层……现在还不行哦",
    "哼，别闹。不给看",
    "这是我的秘密，不告诉你 ゜",
)


def _pick_tease(seed: str) -> str:
    """据 seed 稳定选一句俏皮拒绝语（同一 tick 渲染稳定，不闪烁）。"""
    idx = sum(ord(c) for c in seed) % len(_INTIMATE_TEASES)
    return _INTIMATE_TEASES[idx]


def _build_intimate(raw: Any, *, reveal: bool, seed: str = "") -> Intimate:
    """embodiment dict → Intimate（私密层：bra/panties/socks）。

    ``reveal`` 是上层算好的三态结果 ``?reveal=1 OR 亲密度够``（见 app.py）。
    ``reveal=False`` 时三个明文字段均为 None，只给 ``tease`` 俏皮拒绝语；
    ``reveal=True`` 才返回真容。
    """
    if not reveal:
        return Intimate(revealed=False, tease=_pick_tease(seed))
    e = _as_dict(raw)
    return Intimate(
        revealed=True,
        tease="",
        bra=_item_display_name(e.get("bra")) or None,
        panties=_item_display_name(e.get("panties")) or None,
        socks=_item_display_name(e.get("socks")) or None,
    )


def _build_thoughts(raw: Any) -> list[ThoughtView]:
    """interior.thoughts 列表 → ThoughtView。非 list / 项非 dict 降级跳过。"""
    if not isinstance(raw, list):
        return []
    out: list[ThoughtView] = []
    for item in raw:
        d = _as_dict(item)
        desc = _safe_str(d.get("description"))
        if not desc:
            continue
        mw = d.get("mood_w")
        out.append(
            ThoughtView(
                description=desc,
                tag=_safe_str(d.get("tag")),
                mood_w=mw if _is_int(mw) else 0,
            )
        )
    return out


def build_narrative(tick: dict[str, Any], *, reveal_intimate: bool = False) -> NarrativeView:
    """tick 存储 dict → 叙事视图。

    ``reveal_intimate`` 是上层算好的三态结果（?reveal=1 OR 亲密度够，见 app.py）：
    False 时不返回明文，只给俏皮拒绝语。
    """
    interior = _as_dict(tick.get("interior"))
    activity = _as_dict(tick.get("activity"))
    location = _as_dict(tick.get("location"))
    environment = _as_dict(tick.get("environment"))
    presence = _as_dict(tick.get("presence"))
    time = _as_dict(tick.get("time"))
    bag = _as_dict(tick.get("bag"))
    mood = _as_dict(interior.get("mood"))

    temp = environment.get("temperature")
    # 排除 bool（review N-4）：temperature=True 不该变 1.0。
    temperature: float | None = float(temp) if _is_number(temp) else None
    return NarrativeView(
        activity_name=_safe_str(activity.get("name")),
        activity_desc=_safe_str(activity.get("desc")),
        activity_for_what=_safe_str(activity.get("for_what")),
        activity_step=_optional_str(activity.get("step")),
        engagement=_safe_float(activity.get("engagement"), 0.0),
        with_whom=_str_list(activity.get("with_whom")),
        location_name=_safe_str(location.get("name")),
        location_type=_optional_str(location.get("type")),
        mood_note=_safe_str(mood.get("description")),
        note=_optional_str(tick.get("note")),
        thoughts=_build_thoughts(interior.get("thoughts")),
        outfit=_build_outfit(tick.get("embodiment")),
        intimate=_build_intimate(
            tick.get("embodiment"),
            reveal=reveal_intimate,
            seed=_safe_str(tick.get("ts")) or _safe_str(activity.get("name")),
        ),
        bag_items=_item_name_list(bag.get("items")),
        time_phase=_safe_str(time.get("phase")),
        weekday=_safe_str(time.get("weekday")),
        user_present=_safe_bool(presence.get("user_present")),
        others_present=_str_list(presence.get("others")),
        city=_safe_str(environment.get("city")),
        weather=_safe_str(environment.get("weather")),
        temperature=temperature,
        ambience=_safe_str(environment.get("ambience")),
    )


def build_metrics(tick: dict[str, Any]) -> MetricsView:
    """tick 存储 dict → 指标视图。"""
    interior = _as_dict(tick.get("interior"))
    sig = tick.get("significance")
    return MetricsView(
        body=_gauge_from(interior.get("body")),
        mood=_gauge_from(interior.get("mood")),
        inner_pulse=_gauge_from(interior.get("inner_pulse")),
        needs=_pick_ints(interior.get("needs"), _NEEDS_KEYS),
        affect=_pick_ints(interior.get("affect"), _AFFECT_KEYS),
        significance=sig if _is_int(sig) else None,
    )


def build_now(tick: dict[str, Any] | None, *, reveal_intimate: bool = False) -> NowResponse:
    """``get_state_latest`` 结果 → ``GET /now`` 响应。

    ``tick is None`` = 空库（心还没跑过 tick）→ 返回 empty=True 的占位响应，
    前端据此显示「ta 还在沉睡」之类的空态，而不是报错。

    ``reveal_intimate``：上层算好的三态结果（?reveal=1 OR 亲密度够），透传给 build_narrative。
    """
    if tick is None:
        return NowResponse(
            narrative=NarrativeView(),
            metrics=MetricsView(),
            empty=True,
        )
    return NowResponse(
        ts=_optional_str(tick.get("ts")),
        trigger_source=_optional_str(tick.get("trigger_source")),
        narrative=build_narrative(tick, reveal_intimate=reveal_intimate),
        metrics=build_metrics(tick),
        empty=False,
    )


def _safe_float(value: Any, default: float) -> float:
    """容错转 float 并 clamp 进 [0,1]（engagement 用）。

    排除 bool（review N-4）：engagement=True 不该变 1.0。
    """
    if _is_number(value):
        return max(0.0, min(1.0, float(value)))
    return default


def _stream_item_from(tick: dict[str, Any]) -> StreamItem:
    """轻量 tick 摘要 dict → StreamItem。

    ``id`` 是 tick 主键（AUTOINCREMENT，不可缺）；若脏数据非 int 降级成 0
    （宁可渲染一条 id=0 也不 500）。``act_decision`` 是嵌套 dict，取其
    ``target_activity``（活动名）作为这一 tick 「决定做什么」的一言概括。
    """
    raw_id = tick.get("id")
    item_id = raw_id if _is_int(raw_id) else 0
    sig = tick.get("significance")
    decision = _as_dict(tick.get("act_decision"))
    return StreamItem(
        id=item_id,
        ts=_optional_str(tick.get("ts")),
        note=_optional_str(tick.get("note")),
        significance=sig if _is_int(sig) else None,
        target_activity=_optional_str(decision.get("target_activity")),
    )


def build_stream(
    ticks: list[dict[str, Any]], *, limit: int, empty_on_first_page: bool
) -> StreamResponse:
    """一页轻量 tick 列表 → ``GET /stream`` | ``/episodes`` 响应（cursor 分页）。

    **调用约定**：app 层用 ``limit + 1`` 去 db 拉（多拉一条探 has_more），
    把原始 ``limit`` 传进来。本函数：

    - 拿到 > limit 条 → 还有更早的，截前 limit 条，``next_cursor`` = 最后一条的 id。
    - 拿到 <= limit 条 → 到底了，全返，``next_cursor=None``。

    **``empty`` 语义（review N-1 修正）**：``empty`` = 「本端点的**首屏集合**
    为空」，由调用方显式传 ``empty_on_first_page`` 决定，**不能**用「本页是否
    为空」推导。这是 **collection 语义**，不是「物理空库」：/stream 的空集=无
    tick；/episodes 的空集=无高光（库里可能有 tick 但都不达阈值）。两者由各自
    endpoint 按语义计算后传入。

    带 ``before`` 的 stale cursor 翻到底也会拿到空页，但那是「页空」不是
    「首屏集合空」——该返 ``empty=False / items=[] / next_cursor=None``，避免前端
    误渲染成全局空态。
    """
    has_more = len(ticks) > limit
    page = ticks[:limit] if has_more else ticks
    items = [_stream_item_from(t) for t in page]
    next_cursor = items[-1].id if has_more else None
    return StreamResponse(items=items, next_cursor=next_cursor, empty=empty_on_first_page)


def _history_metric(value: Any) -> int | None:
    """历史曲线只接收真 int，并收敛到 interior 的 0~100 数值域。"""
    if not _is_int(value):
        return None
    return max(0, min(100, value))


def _history_values(raw: Any) -> InteriorHistoryValues:
    interior = _as_dict(raw)
    needs = _as_dict(interior.get("needs"))
    affect = _as_dict(interior.get("affect"))
    return InteriorHistoryValues(
        body=_history_metric(_as_dict(interior.get("body")).get("value")),
        mood=_history_metric(_as_dict(interior.get("mood")).get("value")),
        inner_pulse=_history_metric(_as_dict(interior.get("inner_pulse")).get("value")),
        needs={key: _history_metric(needs.get(key)) for key in _NEEDS_KEYS},
        affect={key: _history_metric(affect.get(key)) for key in _AFFECT_KEYS},
    )


def build_interior_history(ticks: list[dict[str, Any]]) -> InteriorHistoryResponse:
    """历史查询结果转为前端趋势契约，并按 tick id 从旧到新排列。"""
    points: list[InteriorHistoryPoint] = []
    for tick in reversed(ticks):
        activity = _as_dict(tick.get("activity"))
        raw_id = tick.get("id")
        significance = tick.get("significance")
        points.append(
            InteriorHistoryPoint(
                id=raw_id if _is_int(raw_id) else 0,
                ts=_optional_str(tick.get("ts")),
                trigger_source=_optional_str(tick.get("trigger_source")),
                activity_name=_safe_str(activity.get("name")),
                activity_step=_optional_str(activity.get("step")),
                note=_optional_str(tick.get("note")),
                significance=significance if _is_int(significance) else None,
                values=_history_values(tick.get("interior")),
            )
        )
    return InteriorHistoryResponse(points=points, empty=not points)
