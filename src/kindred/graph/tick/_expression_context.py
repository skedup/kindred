import math
from collections.abc import Mapping
from dataclasses import dataclass

from kindred.graph.tick._possession_narrative import expression_possession_fact_lines
from kindred.graph.tick._state_transition import SETTLE_STEP
from kindred.graph.tick.sense_io import weather_cache_is_current


@dataclass(frozen=True)
class ExpressionContext:
    soul_excerpt: str = ""
    scene_lines: tuple[str, ...] = ()
    possession_lines: tuple[str, ...] = ()
    activity_line: str = ""
    sense_note: str = ""
    ambience: str = ""
    thoughts: tuple[str, ...] = ()


def project_expression_context(
    state: Mapping[str, object],
    *,
    triggered_at: object,
    soul_excerpt: str,
    sense_note: object,
    weather_ttl_minutes: int,
    weather_location: str,
) -> ExpressionContext:
    now = _text(triggered_at, 32)
    location = state.get("location")
    location = location if isinstance(location, Mapping) else {}
    scene = [f"- 时间：{now}"] if now else []
    place = [_text(location.get(key), 14) for key in ("name", "type", "city")]
    if any(place):
        scene.append(f"- 地点：{' / '.join(part for part in place if part)}")
    environment = state.get("environment")
    if (
        isinstance(environment, dict)
        and now
        and weather_cache_is_current(
            environment, now, weather_location=weather_location, ttl_minutes=weather_ttl_minutes
        )
    ):
        weather = [_text(environment.get("weather"), 16)]
        weather.extend(
            metric
            for key, label, unit in (
                ("temperature", "气温", "°C"),
                ("precip_mm", "降水", "mm"),
            )
            if (metric := _metric(environment.get(key), label, unit))
        )
        if any(weather):
            scene.append(f"- 天气：{'；'.join(part for part in weather if part)}")
    return ExpressionContext(
        soul_excerpt=_text(soul_excerpt, 500),
        scene_lines=tuple(scene),
        possession_lines=expression_possession_fact_lines(state),
        activity_line=_activity_line(state.get("activity")),
        sense_note=_text(sense_note, 130),
        ambience=_text(environment.get("ambience"), 160)
        if isinstance(environment, Mapping)
        else "",
    )


def _activity_line(value: object) -> str:
    if not isinstance(value, Mapping) or not (name := _text(value.get("name"), 48)):
        return ""
    step = _text(value.get("step"), 32)
    parts = [name]
    if step and step != SETTLE_STEP:
        parts.append(f"step={step}")
    if desc := _text(value.get("desc"), 96):
        parts.append(desc)
    label = "刚结束" if step == SETTLE_STEP else "当前经历"
    return _text(f"- {label}：{'；'.join(parts)}", 160)


def _metric(value: object, label: str, unit: str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{label}{value:.3g}{unit}"
    return ""


def _text(value: object, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return f"{text[: limit - 1]}…" if len(text) > limit else text
