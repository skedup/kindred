"""运行期 character-card 的轻量读取器。

本模块只读取已经被具体实例固化的 runtime card（默认
``life/character-card.yaml``），不处理 seed template 生成、不解释人格偏好。
当前消费面包括 ``home`` 与少量已落地的运行期 trait 投影；它们都是设置级固定
事实，不进入 8 层 state。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

CHARACTER_CARD_PLACE_SOURCE = "character_card"
RUNTIME_TRAITS_SCHEMA = "value_numeric_grade_letter"


class CharacterCardError(RuntimeError):
    """runtime character-card 结构非法。"""


@dataclass(frozen=True)
class InteriorTraitProfile:
    """Interior 派生当前真正消费的最小 trait 投影。"""

    eros: int

    @property
    def arousal_baseline(self) -> int:
        """将 eros 映射为个体 arousal 基线（10..40）。"""
        return round(10 + self.eros * 0.3)


@dataclass(frozen=True)
class CharacterCardManifest:
    """A validated character-card manifest ready to be activated at runtime."""

    path: Path
    name: str
    form: str
    home: HomeProfile | None


@dataclass(frozen=True)
class HomeProfile:
    """character-card 里的固定居住锚点。

    ``home`` 不是 ``state.location``：它只在当前位置不足、或 activity 显式需要
    ``home`` 类候选时作为设置级事实参与 prompt 组装。真正到达家仍由 act 的
    ``location_arrival`` 事件发生。``timezone`` / ``weather_location`` 先随
    runtime card 解析进来；v1 只在 home 作为地点 origin 时带上时区，天气/时钟
    的全局兜底接线留给后续 PR。
    """

    name: str = "家"
    address: str = ""
    city: str = ""
    timezone: str = ""
    weather_location: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip() or "家")
        object.__setattr__(self, "address", self.address.strip())
        object.__setattr__(self, "city", self.city.strip())
        object.__setattr__(self, "timezone", self.timezone.strip())
        object.__setattr__(self, "weather_location", self.weather_location.strip())

    def is_usable(self) -> bool:
        """是否包含足够事实可出现在地点上下文。"""
        return bool(self.address or self.city)

    @property
    def place_key(self) -> str:
        """当前 home 的稳定地点身份；地址变化会得到新 key，支持未来搬家。"""
        identity = self.address or f"{self.city}|{self.name}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        return f"character_card:home:{digest}"


def load_home_profile(path: Path) -> HomeProfile | None:
    """从 runtime character-card 读取 ``home``；文件/字段缺失返回 ``None``。

    root 允许有其他 character-card 字段；``home`` 内只消费第一版字段，未知字段
    保持前向兼容地忽略。字段类型不对时装配期 fail-fast 暴露坏卡，不静默降级。
    """
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CharacterCardError(f"invalid YAML character-card {path}: {exc}") from exc
    root = _load_yaml_mapping(path, loaded)
    if root is None:
        return None
    return _home_profile_from_root(root, path)


def load_interior_trait_profile(path: Path) -> InteriorTraitProfile:
    """严格读取运行期 Interior trait；缺失或非法时装配期失败。"""
    if not path.exists():
        raise CharacterCardError(f"character-card does not exist: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CharacterCardError(f"invalid YAML character-card {path}: {exc}") from exc
    root = _load_yaml_mapping(path, loaded)
    if root is None:
        raise CharacterCardError(f"character-card is empty: {path}")

    traits = root.get("traits")
    if not isinstance(traits, Mapping):
        raise CharacterCardError("character-card traits must be a mapping")
    traits_map = cast("Mapping[str, Any]", traits)
    if traits_map.get("schema") != RUNTIME_TRAITS_SCHEMA:
        raise CharacterCardError(f"character-card traits.schema must be {RUNTIME_TRAITS_SCHEMA!r}")

    eros = traits_map.get("eros")
    if not isinstance(eros, Mapping):
        raise CharacterCardError("character-card traits.eros must be a mapping")
    value = cast("Mapping[str, Any]", eros).get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        raise CharacterCardError("character-card traits.eros.value must be an integer")
    if not 0 <= value <= 100:
        raise CharacterCardError("character-card traits.eros.value must be within 0..100")
    return InteriorTraitProfile(eros=value)


def load_card_manifest(path: Path) -> CharacterCardManifest:
    """Load and validate the card manifest shape used by runtime activation.

    This is intentionally much lighter than a future full card installer: it
    validates ``card.yaml`` enough to expose instance-level settings such as
    ``home`` via ``paths.character_card``. It does not copy soul files or seed
    the database.
    """
    if not path.exists():
        raise CharacterCardError(f"character-card manifest does not exist: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CharacterCardError(f"invalid YAML character-card {path}: {exc}") from exc
    root = _load_yaml_mapping(path, loaded)
    if root is None:
        raise CharacterCardError(f"character-card manifest is empty: {path}")

    name = _optional_str(root, "name", default=path.parent.name).strip()
    if not name:
        raise CharacterCardError(f"character-card name must be a non-empty string: {path}")
    form = _optional_str(root, "form", default="").strip()
    if form not in {"full", "seed"}:
        raise CharacterCardError(f"character-card form must be 'full' or 'seed': {path}")

    home = _home_profile_from_root(root, path)
    if form == "full" and (home is None or not home.address.strip() or not home.city.strip()):
        raise CharacterCardError(
            f"full character-card missing usable home.address/home.city: {path}"
        )
    if form == "seed" and "home" in root and root.get("home") is not None:
        raise CharacterCardError(
            f"seed character-card must not contain home (bootstrap owns it): {path}"
        )
    return CharacterCardManifest(path=path, name=name, form=form, home=home)


def activate_character_card(
    card_dir: Path,
    *,
    target_path: Path,
    force: bool = False,
) -> CharacterCardManifest:
    """Activate ``card_dir/card.yaml`` as the runtime ``paths.character_card``.

    Activation is deliberately non-destructive: it copies only the card manifest
    so runtime-only facts (currently ``home``) become available to the graph.
    Soul files, seed state, and the database are left untouched.
    """
    source = card_dir / "card.yaml" if card_dir.is_dir() else card_dir
    manifest = load_card_manifest(source)

    if target_path.exists():
        same_file = False
        try:
            same_file = source.resolve() == target_path.resolve()
        except OSError:
            same_file = False
        if same_file:
            return manifest
        if not force and target_path.read_text(encoding="utf-8") != source.read_text(
            encoding="utf-8"
        ):
            raise CharacterCardError(
                f"runtime character-card already exists: {target_path} (use --force to replace it)"
            )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target_path)
    return manifest


def _load_yaml_mapping(path: Path, loaded: Any) -> Mapping[str, Any] | None:
    if loaded is None:
        return None
    if not isinstance(loaded, Mapping):
        raise CharacterCardError(f"character-card root must be a mapping: {path}")
    return cast("Mapping[str, Any]", loaded)


def _home_profile_from_root(root: Mapping[str, Any], path: Path) -> HomeProfile | None:
    home = root.get("home")
    if home is None:
        return None
    if not isinstance(home, Mapping):
        raise CharacterCardError(f"character-card home must be a mapping: {path}")
    home_map = cast("Mapping[str, Any]", home)
    profile = HomeProfile(
        name=_optional_str(home_map, "name", default="家"),
        address=_optional_str(home_map, "address"),
        city=_optional_str(home_map, "city"),
        timezone=_optional_str(home_map, "timezone"),
        weather_location=_optional_str(home_map, "weather_location"),
    )
    return profile if profile.is_usable() else None


def _optional_str(raw: Mapping[str, Any], key: str, *, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise CharacterCardError(f"home.{key} must be a string")
    return value


__all__ = [
    "CHARACTER_CARD_PLACE_SOURCE",
    "CharacterCardError",
    "CharacterCardManifest",
    "HomeProfile",
    "InteriorTraitProfile",
    "RUNTIME_TRAITS_SCHEMA",
    "activate_character_card",
    "load_card_manifest",
    "load_home_profile",
    "load_interior_trait_profile",
]
