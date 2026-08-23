"""Portable validation for the published VisualStateV1 contract capsule."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts" / "visual-state"
SCHEMA_PATH = CONTRACT_ROOT / "v1.schema.json"
DIGEST_PATH = CONTRACT_ROOT / "v1.schema.sha256"
FIXTURES_ROOT = CONTRACT_ROOT / "fixtures"

FROZEN_V1_SCHEMA_SHA256 = "4a156e48f7249357e4ad10c02591626c9516bd67a147c58d2a5c2b54f8279db2"
FROZEN_V1_DIRECTORY_SHA256 = "da6995648bcd91e4e6f41211c58d102f0d9afb68bcfd7f9131604143bb99cb32"

VALID_FIXTURES = (
    "valid-empty.json",
    "valid-ready-action.json",
    "valid-ready-no-action.json",
)
INVALID_FIXTURES = (
    "invalid-action-control-character.json",
    "invalid-action-name.json",
    "invalid-control-character.json",
    "invalid-extra-field.json",
    "invalid-missing-action.json",
    "invalid-motion-control-character.json",
    "invalid-revision-type.json",
    "invalid-schema-version.json",
    "invalid-unsafe-revision.json",
)


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _expected_digest() -> str:
    digest, separator, filename = DIGEST_PATH.read_text(encoding="utf-8").strip().partition("  ")
    assert separator == "  "
    assert filename == SCHEMA_PATH.name
    assert len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)
    return digest


def _directory_digest() -> str:
    paths = sorted(path for path in CONTRACT_ROOT.rglob("*") if path.is_file())
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"contracts/visual-state/{path.relative_to(CONTRACT_ROOT).as_posix()}\n"
        for path in paths
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


def test_visual_state_schema_capsule() -> None:
    schema_bytes = SCHEMA_PATH.read_bytes()
    schema_digest = hashlib.sha256(schema_bytes).hexdigest()
    assert schema_digest == _expected_digest()
    assert schema_digest == FROZEN_V1_SCHEMA_SHA256
    assert _directory_digest() == FROZEN_V1_DIRECTORY_SHA256

    schema = _json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    fixture_names = {path.name for path in FIXTURES_ROOT.glob("*.json")}
    assert fixture_names == set(VALID_FIXTURES) | set(INVALID_FIXTURES)

    for filename in VALID_FIXTURES:
        assert list(validator.iter_errors(_json(FIXTURES_ROOT / filename))) == [], filename

    for filename in INVALID_FIXTURES:
        assert list(validator.iter_errors(_json(FIXTURES_ROOT / filename))), filename


if __name__ == "__main__":
    test_visual_state_schema_capsule()
    print("VisualStateV1 contract capsule: ok")
