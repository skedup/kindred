"""OpenClaw Gateway JSON-RPC client (read + write over a single WS).

两条能力（迁入 adapters/openclaw 后这是读者理解本模块边界的入口）：
- **read**：``fetch_chat_history`` 拉 main-session ``chat.history``（earlier milestone）
- **write**：``send_chat`` 注入嘴 main session 让心主动 push（earlier milestone）

earlier milestone: the heart daemon pulls main-session ``chat.history`` over the OpenClaw
Gateway JSON-RPC WebSocket so real conversation (partner + my_voice) mirrors
into the ``main_session_messages`` table. The watcher then polls the table.

Why pull instead of hooks: ``message:sent`` does not fire on wecom inline
agent replies (probe: 3 received / 0 sent), so a hook cannot capture my_voice.
``chat.history`` reads OpenClaw's stored transcript, which natively contains
both user and assistant turns.

This is a trimmed adaptation of a first-party predecessor's ``daemon/gateway.py``.
The initial port kept only the **read** path (connect/auth + chat.history); the
``send_chat`` write path was restored later so the heart can proactively reach
the user (心主动 push，docs/13 §341)：``chat.send(message, sessionKey=嘴 main
session)`` 注入嘴 session 触发一轮，由嘴投递到 channel。``session_create`` 仍未恢复
（Kindred daemon 用 in-process LangGraph 跑 tick，不靠 session_send 驱动 subagent）。

Connection style: each call opens a fresh connection, authenticates, sends one
request, reads the response, and closes. No long-lived socket — the daemon
pulls infrequently (per watcher poll), so a persistent connection's upside is
low and it tends to get stuck across Gateway restarts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kindred.adapters.openclaw.device_identity import (
    DeviceIdentity,
    build_device_auth_payload_v3,
)
from kindred.config import KindredGatewayConfig

logger = logging.getLogger(__name__)

# Gateway client.id whitelist value that hits the localBackend self-pairing
# exemption (mode=backend on loopback) — see FINDINGS. ``openclaw-control-ui``
# triggers the control-ui origin check and is rejected, so we use this.
_CLIENT_ID = "gateway-client"
_CLIENT_MODE = "backend"
_ROLE = "operator"
_SCOPES = ["operator.admin"]
_PLATFORM = "linux"
_DEVICE_FAMILY = ""

# Default on-disk path for the persisted ed25519 device identity (relative to
# the life root). Overridable via the constructor / from_config.
_DEFAULT_IDENTITY_FILENAME = ".device-identity.json"


class GatewayError(Exception):
    """Non-retryable Gateway error (auth failure, protocol violation, etc.)."""


class _RetryableError(Exception):
    """Transient network error; the caller may retry with backoff."""


class GatewayClient:
    """OpenClaw Gateway WebSocket client (one-shot session per call)."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        identity: DeviceIdentity,
        connect_timeout: float = 30.0,
        recv_timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_max: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.identity = identity
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

    @classmethod
    def from_config(
        cls,
        gateway: KindredGatewayConfig,
        *,
        identity_path: Path,
        **overrides: Any,
    ) -> GatewayClient:
        """Build a client from :class:`KindredGatewayConfig`.

        Loads (or creates + persists) the ed25519 device identity from
        ``identity_path``. Raises :class:`GatewayError` when ``token`` is empty
        — the daemon entry should validate config before constructing the
        client.
        """
        if not gateway.token:
            raise GatewayError("gateway.token is empty; set KINDRED_GATEWAY_TOKEN or override")
        identity = DeviceIdentity.load_or_create(identity_path)
        return cls(
            host=gateway.host,
            port=gateway.port,
            token=gateway.token,
            identity=identity,
            **overrides,
        )

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    # ── internal: connect + authenticate ──

    @contextmanager
    def _connected_ws(self) -> Iterator[Any]:
        """Open a WebSocket, complete connect.challenge auth, auto-close.

        Auth failures raise :class:`GatewayError`; network problems raise
        :class:`_RetryableError` for the caller to decide on retry.
        """
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - dep guard
            raise GatewayError(
                "websocket module not installed. Run: pip install websocket-client"
            ) from exc

        ws = None
        try:
            try:
                ws = websocket.create_connection(self.ws_url, timeout=self.connect_timeout)
            except (OSError, websocket.WebSocketException) as exc:
                raise _RetryableError(f"connect failed: {exc}") from exc

            # connect.challenge frame carries the nonce we must sign.
            try:
                challenge = json.loads(ws.recv())
            except (OSError, websocket.WebSocketException) as exc:
                raise _RetryableError(f"awaiting challenge failed: {exc}") from exc
            except (json.JSONDecodeError, TypeError) as exc:
                raise _RetryableError(f"challenge not JSON: {exc}") from exc

            nonce = (challenge.get("payload") or {}).get("nonce")
            if not nonce:
                raise GatewayError(f"connect.challenge missing nonce: {challenge!r}")

            # Sign the v3 device-auth payload with our persisted ed25519 key.
            signed_at_ms = int(time.time() * 1000)
            payload = build_device_auth_payload_v3(
                device_id=self.identity.device_id,
                client_id=_CLIENT_ID,
                client_mode=_CLIENT_MODE,
                role=_ROLE,
                scopes=_SCOPES,
                signed_at_ms=signed_at_ms,
                token=self.token,
                nonce=nonce,
                platform=_PLATFORM,
                device_family=_DEVICE_FAMILY,
            )
            signature = self.identity.sign_b64url(payload)

            connect_req = {
                "type": "req",
                "id": str(uuid.uuid4()),
                "method": "connect",
                "params": {
                    "minProtocol": 4,
                    "maxProtocol": 4,
                    "client": {
                        "id": _CLIENT_ID,
                        "version": "1.0.0",
                        "platform": _PLATFORM,
                        "mode": _CLIENT_MODE,
                    },
                    "role": _ROLE,
                    "scopes": _SCOPES,
                    "auth": {"token": self.token},
                    "device": {
                        "id": self.identity.device_id,
                        "publicKey": self.identity.public_key_b64url,
                        "signedAt": signed_at_ms,
                        "nonce": nonce,
                        "signature": signature,
                    },
                    "caps": [],
                },
            }
            ws.send(json.dumps(connect_req))

            try:
                connect_resp = json.loads(ws.recv())
            except (OSError, websocket.WebSocketException) as exc:
                raise _RetryableError(f"awaiting connect response failed: {exc}") from exc

            if connect_resp.get("type") == "res" and not connect_resp.get("ok", True):
                msg = connect_resp.get("error", {}).get("message", "unknown")
                raise GatewayError(f"connect failed: {msg}")

            yield ws
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # pragma: no cover - best effort close
                    pass

    def _rpc_call(
        self,
        ws: Any,
        method: str,
        params: dict[str, Any],
        *,
        recv_timeout: float | None = None,
        max_event_frames: int = 20,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request on an authenticated ws, return the res.

        Skips ``event`` frames (up to ``max_event_frames``), matches the res by
        request id.
        - success res → ``{"ok": True, "payload": ...}``
        - business-error res → ``{"error": ...}``
        - timeout / unparseable / frame budget exhausted → :class:`_RetryableError`
        """
        import websocket

        req_id = str(uuid.uuid4())
        req = {"type": "req", "id": req_id, "method": method, "params": params}
        ws.send(json.dumps(req))

        ws.settimeout(recv_timeout if recv_timeout is not None else self.recv_timeout)

        for _ in range(max_event_frames):
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException as exc:
                raise _RetryableError(f"awaiting {method} response timed out: {exc}") from exc
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                raise _RetryableError(f"{method} got non-JSON frame: {exc}") from exc

            mtype = msg.get("type")
            if mtype == "event":
                continue
            if mtype == "res" and msg.get("id") == req_id:
                if msg.get("ok") is False:
                    return {"error": f"{method} request failed"}
                return {"ok": True, "payload": msg.get("payload", {}) or {}}
            # Mismatched res (shouldn't happen) → keep waiting.
        raise _RetryableError(f"{method}: no matching res within {max_event_frames} frames")

    def _retrying_rpc(
        self,
        method_name: str,
        params_factory: Callable[[], dict[str, Any]],
        *,
        result_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        recv_timeout: float | None = None,
    ) -> dict[str, Any]:
        """connect → auth → one RPC, with exponential backoff retry.

        Retries cover network jitter; business errors (``{"error": ...}``) and
        auth errors return immediately without retry.
        """
        last_err: str | None = None
        params = params_factory()
        for attempt in range(self.max_retries):
            try:
                with self._connected_ws() as ws:
                    rpc_resp = self._rpc_call(ws, method_name, params, recv_timeout=recv_timeout)
                if "error" in rpc_resp:
                    return rpc_resp
                if result_handler is not None:
                    return result_handler(rpc_resp)
                return rpc_resp
            except _RetryableError as exc:
                last_err = str(exc)
                wait = min(self.backoff_base**attempt, self.backoff_max)
                logger.warning(
                    "Gateway %s retry %d/%d failed: %s; sleeping %.1fs",
                    method_name,
                    attempt + 1,
                    self.max_retries,
                    last_err,
                    wait,
                )
                if attempt + 1 < self.max_retries:
                    time.sleep(wait)
            except GatewayError as exc:
                # Auth-class errors are not retryable.
                return {"error": str(exc)}
            except Exception as exc:  # pragma: no cover - unexpected
                logger.exception("Gateway %s unrecoverable error", method_name)
                return {"error": str(exc)}
        return {
            "error": f"Gateway {method_name} failed after {self.max_retries} retries: {last_err}"
        }

    # ── public API ──

    def probe(self) -> dict[str, Any]:
        """Check WebSocket reachability (connect.challenge stage only)."""
        try:
            import websocket
        except ImportError:  # pragma: no cover - dep guard
            return {"error": "websocket module not installed. Run: pip install websocket-client"}

        ws = None
        try:
            ws = websocket.create_connection(self.ws_url, timeout=self.connect_timeout)
            challenge = json.loads(ws.recv())
            if challenge.get("event") == "connect.challenge":
                return {"ok": True, "message": "WebSocket connection available"}
            return {"ok": True, "first_message": challenge}
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # pragma: no cover
                    pass

    def fetch_chat_history(self, session_key: str, limit: int = 30) -> dict[str, Any]:
        """Pull the most recent ``limit`` messages of ``session_key``.

        Returns:
            success → ``{"ok": True, "messages": [...], "sessionId": ..., "sessionKey": ...}``
            failure → ``{"error": ...}``

        ``messages`` is the raw chat.history array (upstream decides order).
        Exponential-backoff retry on transient network errors.
        """

        def handle(rpc_resp: dict[str, Any]) -> dict[str, Any]:
            payload = rpc_resp["payload"]
            session_info = payload.get("sessionInfo")
            session_info_key = session_info.get("key") if isinstance(session_info, dict) else None
            if payload.get("sessionKey") != session_key or session_info_key != session_key:
                return {"error": "chat.history canonical session identity mismatch"}
            return {
                "ok": True,
                "messages": payload.get("messages", []),
                "sessionId": payload.get("sessionId"),
                "sessionKey": payload.get("sessionKey"),
                "sessionInfoKey": session_info_key,
            }

        return self._retrying_rpc(
            "chat.history",
            lambda: {"sessionKey": session_key, "limit": limit},
            result_handler=handle,
        )

    def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        page_limit: int = 100,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        """Enumerate all sessions using the fixed protocol's offset pagination."""
        if not 1 <= page_limit <= 1000 or max_pages < 1:
            return {"error": "sessions.list pagination arguments are invalid"}
        sessions: list[dict[str, Any]] = []
        offset = 0

        def page_payload(response: dict[str, Any]) -> dict[str, Any]:
            payload = response.get("payload")
            return (
                payload
                if isinstance(payload, dict)
                else {"error": "sessions.list response schema is invalid"}
            )

        for _ in range(max_pages):

            def page_params(current_offset: int = offset) -> dict[str, Any]:
                params: dict[str, Any] = {"limit": page_limit, "offset": current_offset}
                if agent_id:
                    params["agentId"] = agent_id
                return params

            result = self._retrying_rpc(
                "sessions.list",
                page_params,
                result_handler=page_payload,
            )
            if "error" in result:
                return result
            page = result.get("sessions")
            has_more = result.get("hasMore")
            if not isinstance(page, list) or not isinstance(has_more, bool):
                return {"error": "sessions.list response schema is invalid"}
            for row in page:
                if not isinstance(row, dict):
                    return {"error": "sessions.list session row is invalid"}
                sessions.append(_project_session_row(row))
            if not has_more:
                return {"ok": True, "sessions": sessions, "count": len(sessions)}
            next_offset = result.get("nextOffset")
            if (
                not isinstance(next_offset, int)
                or isinstance(next_offset, bool)
                or next_offset <= offset
            ):
                return {"error": "sessions.list pagination cursor is invalid"}
            offset = next_offset
        return {"error": "sessions.list exceeded page budget"}

    def send_chat(
        self,
        session_key: str,
        message: str,
        *,
        provenance: dict[str, Any] | None = None,
        suppress_command_interpretation: bool = False,
        deliver: bool = False,
        originating_channel: str | None = None,
        originating_to: str | None = None,
        originating_account_id: str | None = None,
        originating_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Inject ``message`` into ``session_key`` and trigger one agent turn.

        This restores the **write** path that the initial port intentionally
        dropped, so the heart can push to the user. It mirrors the verified
        first-party protocol: ``chat.send`` with
        ``{message, sessionKey, idempotencyKey}``.

        ``provenance``（可选）：一个 ``systemInputProvenance`` dict
        （``{kind, sourceSessionKey?, sourceChannel?, sourceTool?}``）。复用 openclaw
        的 inter-session/announce 传输：心传 ``kind="inter_session"``，使这一轮被框成
        「别处路由来的」（嘴用自己声音重表达）而非终端用户的直接消息。要求连接持有
        ``operator.admin`` scope（``canInjectSystemProvenance``）——本 client 满足。
        ``provenance`` 被 openclaw 用来注解 agent 的 prompt，且会保留在 transcript
        消息上（穿过 ``chat.history`` 存活），让心侧据此过滤自己的 echo。

        ``suppress_command_interpretation``：传 ``True`` 让注入的文本不被当作 slash
        命令解析。

        ``deliver`` + ``originating_channel`` / ``originating_to`` /
        ``originating_account_id``（投递路由）：inter-session 注入的 turn 默认 surface =
        INTERNAL，回复**只到 web**。要把回复投到真实 channel（如个人微信），须 **同时**
        传 ``deliver=True`` 和 ``originating_channel`` + ``originating_to``（openclaw
        ``resolveChatSendOriginatingRoute``：``explicitDeliverRoute = deliver===true``；且
        ``normalizeExplicitChatSendOrigin`` 要求 channel 与 to 同时给）。``account_id``
        可选。需 ``operator.admin`` scope（本 client 满足）。

        **Async-ack semantics** (docs/13 §4): ``chat.send`` returns once the
        request is accepted and the turn *starts*; it does **not** wait for the
        agent to finish. ``status=started`` only means dispatch accepted.

        Why not reuse ``_retrying_rpc``: its retry treats a recv timeout as a
        :class:`_RetryableError` and re-sends. After the request is sent, timeout,
        disconnect, or a missing matching response leaves the side effect
        **unknown**; re-sending could trigger a duplicate turn. We therefore send
        once and return ``side_effect="unknown"``. Pre-send failures return
        ``side_effect="none"``.

        Where ``session_key`` points decides the delivery semantics: it is the
        user's main session (the mouth). The mouth's turn physically delivers
        the text to wecom — the heart decides *whether* and *what*, the mouth is
        only the delivery pipe (see earlier milestone discussion §3, "决策在心，投递经嘴管道").

        Returns:
            accepted → ``{"ok": True, ...}``
            pre-send failure → ``{"error": ..., "side_effect": "none"}``
            post-send uncertainty → ``{"error": ..., "side_effect": "unknown"}``
        """
        import websocket

        clean = (message or "").strip()
        if not clean:
            return {"error": "send_chat: message is empty"}

        params: dict[str, Any] = {
            "message": clean,
            "sessionKey": session_key,
            "idempotencyKey": str(uuid.uuid4()),
        }
        if provenance is not None:
            params["systemInputProvenance"] = provenance
        if suppress_command_interpretation:
            params["suppressCommandInterpretation"] = True
        if deliver:
            params["deliver"] = True
        if originating_channel:
            params["originatingChannel"] = originating_channel
        if originating_to:
            params["originatingTo"] = originating_to
        if originating_account_id:
            params["originatingAccountId"] = originating_account_id
        if originating_thread_id:
            params["originatingThreadId"] = originating_thread_id
        dispatch_attempted = False
        try:
            with self._connected_ws() as ws:
                req_id = str(uuid.uuid4())
                req = {"type": "req", "id": req_id, "method": "chat.send", "params": params}
                # send() 抛错时也可能已经把部分或完整 frame 写入 socket。进入调用后，
                # 结果只能视为 unknown，不能按 pre-send failure 自动重试。
                dispatch_attempted = True
                ws.send(json.dumps(req))
                ws.settimeout(self.recv_timeout)
                # Skip event frames; match res by id. A timeout/disconnect after
                # send means the side effect is unknown and must not be retried.
                for _ in range(20):
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        return {
                            "error": "chat.send outcome is unknown",
                            "side_effect": "unknown",
                        }
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if msg.get("type") == "event":
                        continue
                    if msg.get("type") == "res" and msg.get("id") == req_id:
                        if msg.get("ok") is False:
                            return {
                                "error": "chat.send request rejected",
                                "side_effect": "none",
                            }
                        payload = msg.get("payload")
                        if (
                            msg.get("ok") is True
                            and isinstance(payload, dict)
                            and payload.get("status") == "started"
                        ):
                            return {"ok": True, "payload": payload}
                        return {
                            "error": "chat.send outcome is unknown",
                            "side_effect": "unknown",
                        }
                # No matching response after send: the side effect is unknown.
                return {
                    "error": "chat.send outcome is unknown",
                    "side_effect": "unknown",
                }
        except GatewayError as exc:
            # connect / auth failure (pre-send) — not retryable, surface as error.
            return {"error": str(exc), "side_effect": "none"}
        except _RetryableError as exc:
            # Connect-stage network jitter (pre-send): nothing was sent, so no
            # duplicate-turn risk. We still don't retry here (keep send_chat's
            # "send once" contract simple); surface as error for the caller
            # (IOBridge) to degrade. The send actually going out only happens
            # after _connected_ws yields, past this stage.
            return {"error": f"send_chat connect failed: {exc}", "side_effect": "none"}
        except (OSError, websocket.WebSocketException) as exc:
            return {
                "error": (
                    "chat.send outcome is unknown"
                    if dispatch_attempted
                    else f"send_chat ws failure: {exc}"
                ),
                "side_effect": "unknown" if dispatch_attempted else "none",
            }


def _project_session_row(row: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields needed to prove wire identity and diagnose session drift."""
    origin = row.get("origin")
    delivery = row.get("deliveryContext")
    return {
        "key": row.get("key"),
        "kind": row.get("kind"),
        "chatType": row.get("chatType"),
        "updatedAt": row.get("updatedAt"),
        "verboseLevel": row.get("verboseLevel"),
        "origin": (
            {key: origin.get(key) for key in ("provider", "accountId", "to")}
            if isinstance(origin, dict)
            else None
        ),
        "deliveryContext": (
            {
                key: delivery.get(key)
                for key in ("channel", "accountId", "to", "threadId")
                if key in delivery
            }
            if isinstance(delivery, dict)
            else None
        ),
    }
