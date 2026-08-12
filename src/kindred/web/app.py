"""FastAPI app 工厂 —— 只读可视化后端。

═══ db 访问策略 ═══

可视化是**只读**的，且 ``sqlite3.Connection`` 单线程约束（见 facade 文档）。
所以这里**不**长持一个全局连接，而是每个请求 ``KindredDB.open_readonly(db_path)``
开-读-关。理由：

- **真只读**（earlier milestone review N-2）：用 ``open_readonly``（``mode=ro`` URI）而非
  ``open``——后者会 ``connect + migrate`` 触发 DDL 写库，违背「只读观察窗」。
  ``mode=ro`` 下任何写都被 SQLite 引擎拒绝，对被观察库零副作用。
- 只读 SELECT 短平快，open/close 开销可忽略（WAL 模式读不阻塞心的写）。
- 每请求独立连接 → 天然规避「跨线程共享 conn 报错」（uvicorn 多 worker/线程安全）。
- 心（daemon）持有自己的写连接，互不干扰（WAL 允许并发读 + 单写）。

═══ 配置 ═══

db 路径来自 ``KindredConfig.paths.db``（走 conf/kindred.yaml）。
``create_app`` 也接受显式 ``db_path`` 覆盖，方便测试注入临时库。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from kindred import __version__
from kindred.capability_host.artifacts import ArtifactStore, ArtifactStoreError
from kindred.config.loader import load_kindred_config
from kindred.config.schema import KindredConfig
from kindred.db import KindredDB
from kindred.db.artifacts import ArtifactCommitRow
from kindred.relationship.preflight import (
    RelationshipPreflightError,
    require_user_relationship,
)
from kindred.web.contracts import (
    ArtifactDetailResponse,
    ArtifactListItem,
    ArtifactListResponse,
    InteriorHistoryResponse,
    NowResponse,
    RelationshipView,
    StreamResponse,
)
from kindred.web.service import (
    build_interior_history,
    build_now,
    build_stream,
    decode_artifact_cursor,
    encode_artifact_cursor,
    project_committed_artifact,
    read_projected_member,
)

# /stream 分页上限：防止单请求拉爆（一次最多一屏多一点）。
_STREAM_MAX_LIMIT = 100
_STREAM_DEFAULT_LIMIT = 20
_INTERIOR_HISTORY_DEFAULT_LIMIT = 120
_INTERIOR_HISTORY_MAX_LIMIT = 500


def create_app(
    *,
    config: KindredConfig | None = None,
    db_path: Path | None = None,
    reveal_intimate: bool | None = None,
    artifact_store: ArtifactStore | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """构造只读可视化 app。

    :param config: 已解析的运行配置（CLI 注入）。None 才在这里重新 ``load_kindred_config``。
        ⚠ N-7：``kindred serve --config X`` 已在 cli 解析好配置，必须把它传进来，
        否则 app 会从 cwd 的 conf/kindred.yaml 二次读，丢掉 --config 里的 web 设置。
    :param db_path: 显式覆盖 db 路径（测试注入用）。None = 用 config.paths.db。
    :param reveal_intimate: 显式覆盖「亲密度够」标志（测试注入）。None = 用 config。
    """
    if config is None:
        config = load_kindred_config()
    explicit_db = db_path is not None
    resolved_db = db_path if db_path is not None else config.paths.db
    resolved_artifact_store = artifact_store
    if resolved_artifact_store is None and not explicit_db:
        resolved_artifact_store = ArtifactStore(config.paths.doc_dir / "artifacts")
    # 私密层明文是否返回。``web.reveal_intimate`` 语义 = 本实例「亲密度够」
    # 标志（亲密关系实例 true / 默认 false；docs/02 §401 关系深度控制）。
    # 亲密度数值源（relationship.passion）未实装，先用 config 当代理。
    intimacy_high = reveal_intimate if reveal_intimate is not None else config.web.reveal_intimate

    app = FastAPI(
        title="Kindred Life — 只读可视化",
        description="观察 ta 的生活。只读，绝不写库。",
        version=__version__,
    )
    api = APIRouter()

    def _artifact_unavailable() -> HTTPException:
        return HTTPException(status_code=503, detail="Artifact view is unavailable")

    def _read_artifacts(
        reader: Callable[[KindredDB], Any],
        missing: Any = None,
    ) -> Any:
        if not resolved_db.exists():
            return missing
        try:
            with KindredDB.open_readonly(resolved_db) as db:
                version = db.get_schema_version()
                if version is None or version < 5:
                    raise _artifact_unavailable()
                return reader(db)
        except sqlite3.OperationalError as exc:
            raise _artifact_unavailable() from exc

    def _artifact_store() -> ArtifactStore:
        if resolved_artifact_store is None:
            raise _artifact_unavailable()
        return resolved_artifact_store

    def _artifact_row(tick_id: int, ordinal: int) -> ArtifactCommitRow:
        row: ArtifactCommitRow | None = _read_artifacts(
            lambda db: db.get_committed_artifact(
                tick_id=tick_id,
                artifact_ordinal=ordinal,
            ),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return row

    @api.get("/healthz")
    def healthz() -> dict[str, object]:
        """存活探针 + db 可达性。前端启动时先打这个。"""
        exists = resolved_db.exists()
        return {
            "status": "ok",
            "db": str(resolved_db),
            "db_exists": exists,
        }

    @api.get("/now", response_model=NowResponse)
    def now(
        reveal: bool = Query(
            default=False,
            description="揭示私密层明文（右上角「揭示所有」开关）；开启即无条件显明文",
        ),
    ) -> NowResponse:
        """此刻切片：ta 现在在做什么、什么心情（叙事）+ 各维度数值（指标）。

        空库（心没跑过 tick）→ empty=True 占位响应，不报错。
        db 文件不存在 / 库未 migrate → 同样当空库处理，绝不在 GET 里建表。

        私密层明文三态：揭示开关开（?reveal=1）**OR** 亲密度够（config）→ 显明文；
        两者都不满足 → 只出俏皮拒绝语。
        """
        # 三态（用户 2026-06-15 拍板）：显明文 = 揭示开关开 **OR** 亲密度够。
        #  - ?reveal=1 无条件生效（揭示开关开 → 不论亲密度都显）
        #  - 开关关但亲密度够（config true）→ 显
        #  - 开关关且亲密度不够 → 俏皮拒绝语
        # 注：publish 陌生人实例里 ?reveal=1 仍无条件生效（N-6 陌生人绕过风险），
        # 按用户拍板下放网络层专门解决，不在 contract 层加闸。
        do_reveal = reveal or intimacy_high
        if not resolved_db.exists():
            return build_now(None, reveal_intimate=do_reveal)
        try:
            with KindredDB.open_readonly(resolved_db) as db:
                latest = db.get_state_latest()
        except sqlite3.OperationalError:
            # 库存在但未 migrate（无 state_latest 视图）/ 只读打开失败：
            # 当空库处理，绝不触发 DDL 建表（review N-2）。
            return build_now(None, reveal_intimate=do_reveal)
        return build_now(latest, reveal_intimate=do_reveal)

    @api.get("/relationship", response_model=RelationshipView)
    def relationship() -> RelationshipView:
        """Strict current user Relationship; missing/corrupt data is never neutralized."""
        if not resolved_db.exists():
            raise HTTPException(status_code=503, detail="Relationship view is unavailable")
        try:
            with KindredDB.open_readonly(resolved_db) as db:
                profile = require_user_relationship(db)
        except (RelationshipPreflightError, sqlite3.OperationalError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Relationship view is unavailable",
            ) from exc
        return RelationshipView(**profile.model_dump(exclude={"subject_key", "updated_tick_id"}))

    def _paged(
        reader: Callable[[KindredDB, int, int | None], list[dict[str, object]]],
        *,
        before: int | None,
        limit: int,
    ) -> StreamResponse:
        """/stream + /episodes 共享的分页读路径：只读打开 + 降级 + 首屏空集计算。

        ``reader(db, limit_plus_one, before)`` 负责去 db 拉（多拉一条探 has_more）；
        两个端点只是 reader 不同（全量 tick vs significance>=7），其余完全一致。

        db 不存在 / 未 migrate → empty=True，不报错、不建表（同 /now）。

        ``empty`` 语义（review N-1 修正）= **本端点首屏集合为空**（collection
        语义，非「物理空库」）：仅首屏（无 ``before``）且零行才 True。对 /stream
        是「无 tick」，对 /episodes 是「无高光」（库里可能有 tick 但都不达阈值）。
        带 ``before`` 的 stale cursor 翻到底是「页空」不是「首屏集合空」。
        """
        if not resolved_db.exists():
            return build_stream([], limit=limit, empty_on_first_page=True)
        try:
            with KindredDB.open_readonly(resolved_db) as db:
                rows = reader(db, limit + 1, before)
        except sqlite3.OperationalError:
            return build_stream([], limit=limit, empty_on_first_page=True)
        empty_on_first_page = before is None and not rows
        return build_stream(rows, limit=limit, empty_on_first_page=empty_on_first_page)

    @api.get("/stream", response_model=StreamResponse)
    def stream(
        before: int | None = Query(
            default=None,
            ge=1,
            description="cursor：只返 id 小于此值的（上一页 next_cursor）；缺省从最新开始",
        ),
        limit: int = Query(
            default=_STREAM_DEFAULT_LIMIT,
            ge=1,
            le=_STREAM_MAX_LIMIT,
            description="本页条数（1-100）",
        ),
    ) -> StreamResponse:
        """生命流：回放 ta 走过的路（全量 tick），按 id DESC cursor 分页。

        ``empty=True`` = 首屏无任何 tick（心还没跑过）。
        """
        return _paged(
            lambda db, n, b: db.get_ticks_page(limit=n, before_id=b),
            before=before,
            limit=limit,
        )

    @api.get("/episodes", response_model=StreamResponse)
    def episodes(
        before: int | None = Query(
            default=None,
            ge=1,
            description="cursor：只返 id 小于此值的（上一页 next_cursor）；缺省从最新开始",
        ),
        limit: int = Query(
            default=_STREAM_DEFAULT_LIMIT,
            ge=1,
            le=_STREAM_MAX_LIMIT,
            description="本页条数（1-100）",
        ),
    ) -> StreamResponse:
        """高光闪回：只看 significance>=7 的高光时刻（同 /stream 形状，cursor 分页）。

        ``empty=True`` = 首屏无高光（库里可能有 tick 但都不达 significance 阈值）。
        """
        return _paged(
            lambda db, n, b: db.get_episodes_page(limit=n, before_id=b),
            before=before,
            limit=limit,
        )

    @api.get("/interior/history", response_model=InteriorHistoryResponse)
    def interior_history(
        limit: int = Query(
            default=_INTERIOR_HISTORY_DEFAULT_LIMIT,
            ge=2,
            le=_INTERIOR_HISTORY_MAX_LIMIT,
            description="最近 tick 数量（2-500），响应按 tick id 正序",
        ),
    ) -> InteriorHistoryResponse:
        """Interior 趋势：只返回数值与 hover 所需的少量 tick 摘要。"""
        if not resolved_db.exists():
            return build_interior_history([])
        try:
            with KindredDB.open_readonly(resolved_db) as db:
                rows = db.get_interior_history(limit=limit)
        except sqlite3.OperationalError:
            return build_interior_history([])
        return build_interior_history(rows)

    @api.get("/artifacts", response_model=ArtifactListResponse)
    def artifacts(
        cursor: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ArtifactListResponse:
        store = _artifact_store()
        try:
            decoded = decode_artifact_cursor(cursor) if cursor is not None else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid artifact cursor") from exc
        rows: list[ArtifactCommitRow] = _read_artifacts(
            lambda db: db.list_committed_artifacts(limit=limit + 1, cursor=decoded),
            [],
        )
        visible = rows[:limit]
        items: list[ArtifactListItem] = [
            project_committed_artifact(row, store)[0] for row in visible
        ]
        next_cursor = (
            encode_artifact_cursor(
                visible[-1].tick_id,
                visible[-1].artifact_ordinal,
            )
            if len(rows) > limit and visible
            else None
        )
        return ArtifactListResponse(items=items, next_cursor=next_cursor)

    @api.get(
        "/artifacts/{tick_id}/{artifact_ordinal}",
        response_model=ArtifactDetailResponse,
    )
    def artifact_detail(tick_id: int, artifact_ordinal: int) -> ArtifactDetailResponse:
        store = _artifact_store()
        return project_committed_artifact(_artifact_row(tick_id, artifact_ordinal), store)[0]

    @api.get("/artifacts/{tick_id}/{artifact_ordinal}/members/{member_ordinal}")
    def artifact_member(
        tick_id: int,
        artifact_ordinal: int,
        member_ordinal: int,
    ) -> Response:
        store = _artifact_store()
        row = _artifact_row(tick_id, artifact_ordinal)
        projection = project_committed_artifact(row, store)
        try:
            content, media_type = read_projected_member(
                projection,
                member_ordinal=member_ordinal,
                row=row,
                store=store,
            )
        except ArtifactStoreError as exc:
            raise HTTPException(status_code=404, detail="Artifact member not found") from exc
        return Response(
            content=content,
            media_type=media_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    app.include_router(api)
    app.include_router(api, prefix="/api", include_in_schema=False)

    if static_dir is not None:
        index_path = static_dir / "index.html"
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="web-assets")
        reserved = {"api", "docs", "openapi.json", "redoc"}
        for route in api.routes:
            path = getattr(route, "path", "")
            if path:
                reserved.add(path.lstrip("/").partition("/")[0])

        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        def spa_root() -> FileResponse:
            return FileResponse(index_path)

        @app.exception_handler(404)
        async def spa_fallback(request: Request, exc: StarletteHTTPException) -> Response:
            path = request.url.path.lstrip("/")
            first_segment = path.partition("/")[0]
            if (
                request.method in {"GET", "HEAD"}
                and first_segment not in reserved
                and "text/html" in request.headers.get("accept", "")
            ):
                return FileResponse(index_path)
            return await http_exception_handler(request, exc)

    return app
