"""ed25519 device identity for Gateway v4 device pairing (earlier milestone).

The internal OpenClaw Gateway upgraded to connect protocol v4 and now *requires*
a device identity — a pure shared token no longer authenticates (the build's
``authorizeGatewayConnect`` is stubbed to return ``method:"none"``, so the
``operator && sharedAuthOk`` skip never fires). The only viable path is to
present a signed device identity; a ``mode:"backend"`` client on loopback hits
the ``localBackendSelfPairingOk`` self-pairing exemption and is accepted without
manual approval.

This module owns the ed25519 keypair: generate once, persist to disk, reuse on
every connect. The signature scheme was verified against the Gateway's
``device-identity`` / ``client`` modules and a live isolated connection probe.

Signed payload (v3, ``|``-joined):
    v3|deviceId|clientId|clientMode|role|scopes|signedAt|token|nonce|platform|deviceFamily
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# DER SPKI prefix for an ed25519 public key; the 32 raw key bytes follow it.
# Matches the Gateway's ED25519_SPKI_PREFIX (device-identity module).
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _b64url(raw: bytes) -> str:
    """base64url without padding (matches Gateway base64UrlEncode)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _raw_public_key(public_key: Ed25519PublicKey) -> bytes:
    """32 raw ed25519 public-key bytes (strip the SPKI DER prefix)."""
    spki = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if len(spki) == len(_ED25519_SPKI_PREFIX) + 32 and spki.startswith(_ED25519_SPKI_PREFIX):
        return spki[len(_ED25519_SPKI_PREFIX) :]
    return spki


@dataclass(frozen=True)
class DeviceIdentity:
    """A persisted ed25519 identity used for Gateway device pairing."""

    device_id: str
    public_key_b64url: str
    _private_key: Ed25519PrivateKey

    def sign_b64url(self, payload: str) -> str:
        """Sign a UTF-8 payload, return base64url signature (no padding)."""
        return _b64url(self._private_key.sign(payload.encode("utf-8")))

    @classmethod
    def generate(cls) -> DeviceIdentity:
        priv = Ed25519PrivateKey.generate()
        return cls._from_private_key(priv)

    @classmethod
    def _from_private_key(cls, priv: Ed25519PrivateKey) -> DeviceIdentity:
        raw_pub = _raw_public_key(priv.public_key())
        device_id = hashlib.sha256(raw_pub).hexdigest()
        return cls(
            device_id=device_id,
            public_key_b64url=_b64url(raw_pub),
            _private_key=priv,
        )

    # ── persistence ──

    @classmethod
    def load_or_create(cls, path: Path) -> DeviceIdentity:
        """Load the identity from ``path``; generate + persist if absent/invalid.

        The stored file holds the PKCS8 PEM private key (the public key and
        device_id are derived, never trusted from disk). File mode is tightened
        to ``0600`` since it is a credential.
        """
        if path.exists():
            try:
                identity = cls._load(path)
                # 复用路径也收紧权限：手工迁移 / 备份恢复 / 旧版本留下的文件可能带
                # 0644，会让明文 ed25519 私钥 world-readable（N-3）。
                _ensure_private_mode(path)
                return identity
            except Exception as exc:  # corrupt / unreadable → regenerate
                logger.warning("device identity at %s unreadable (%s); regenerating", path, exc)
        identity = cls.generate()
        identity._persist(path)
        logger.info("generated new device identity %s at %s", identity.device_id, path)
        return identity

    @classmethod
    def _load(cls, path: Path) -> DeviceIdentity:
        data = json.loads(path.read_text(encoding="utf-8"))
        pem = data["privateKeyPem"].encode("utf-8")
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("stored key is not ed25519")
        return cls._from_private_key(key)

    def _persist(self, path: Path) -> None:
        pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"deviceId": self.device_id, "privateKeyPem": pem}),
            encoding="utf-8",
        )
        _ensure_private_mode(path)


def _ensure_private_mode(path: Path) -> None:
    """best-effort 把 ``path`` 收紧到 ``0600``（凭据文件）。"""
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - 古怪文件系统上尽力而为
        pass


def build_device_auth_payload_v3(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at_ms: int,
    token: str,
    nonce: str,
    platform: str = "linux",
    device_family: str = "",
) -> str:
    """Build the v3 device-auth signing payload (``|``-joined).

    Mirrors the Gateway's ``buildDeviceAuthPayloadV3``. ``scopes`` is comma-
    joined; ``signed_at_ms`` is stringified; empty metadata stays empty string.
    """
    return "|".join(
        [
            "v3",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token or "",
            nonce,
            platform,
            device_family,
        ]
    )
