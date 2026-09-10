"""Public export and recursive redaction gate tests."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest


def _load_module() -> Any:
    script = Path(__file__).resolve().parents[3] / "scripts" / "public_release.py"
    spec = importlib.util.spec_from_file_location("public_release", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _policy(
    path: Path,
    *,
    paths: list[str],
    exceptions: list[dict[str, object]] | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paths": paths,
                "trees": [],
                "redaction_exceptions": exceptions or [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _ledger(
    path: Path,
    *,
    paths: list[str],
    decision: str = "approved",
    source_status: str = "known",
    rights_status: str = "cleared",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "groups": [
                    {
                        "paths": paths,
                        "source": "synthetic",
                        "source_status": source_status,
                        "original_license": "first-party",
                        "rights_basis": "owned",
                        "rights_status": rights_status,
                        "public_license": "Apache-2.0",
                        "attribution": "none",
                        "public_decision": decision,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_repo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", "public-release@example.invalid")
    _git(path, "config", "user.name", "Public Release Test")
    return path


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fixture")


def _without_central_extra(data: bytes) -> bytes:
    raw = bytearray(data)
    central = raw.index(b"PK\x01\x02")
    name_size, extra_size = struct.unpack_from("<HH", raw, central + 28)
    extra_start = central + 46 + name_size
    struct.pack_into("<H", raw, central + 30, 0)
    del raw[extra_start : extra_start + extra_size]
    eocd = raw.index(b"PK\x05\x06")
    directory_size = struct.unpack_from("<I", raw, eocd + 12)[0]
    struct.pack_into("<I", raw, eocd + 12, directory_size - extra_size)
    return bytes(raw)


def _change_local_asi_sizdev(data: bytes) -> bytes:
    raw = bytearray(data)
    name_size, extra_size = struct.unpack_from("<HH", raw, 26)
    extra_start = 30 + name_size
    field_id, payload_size = struct.unpack_from("<HH", raw, extra_start)
    assert field_id == 0x756E
    assert payload_size == extra_size - 4
    payload_start = extra_start + 4
    struct.pack_into("<I", raw, payload_start + 6, 1)
    payload = raw[payload_start : payload_start + payload_size]
    struct.pack_into("<I", raw, payload_start, zlib.crc32(payload[4:]))
    return bytes(raw)


def _prepared_repo(
    tmp_path: Path,
    *,
    selected: dict[str, str],
    ledger_paths: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    root = _git_repo(tmp_path / "repo")
    for rel, content in selected.items():
        destination = root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    policy = _policy(root / "allow.json", paths=sorted(selected))
    ledger = _ledger(root / "ledger.json", paths=ledger_paths or sorted(selected))
    _commit(root)
    return root, policy, ledger


def test_repository_allowlist_is_an_exact_path_snapshot() -> None:
    root = Path(__file__).resolve().parents[3]
    policy = json.loads((root / "distribution/public-allowlist.json").read_text())
    tracked = set(
        subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )

    assert policy["trees"] == []
    assert policy["paths"] == sorted(set(policy["paths"]))
    assert set(policy["paths"]) <= tracked


def test_export_requires_empty_target_and_uses_one_head_snapshot(tmp_path: Path) -> None:
    release = _load_module()
    root, policy, ledger = _prepared_repo(tmp_path, selected={"safe.txt": "committed\n"})
    (root / "safe.txt").write_text("api" + "_key=working-tree-secret\n", encoding="utf-8")
    target = tmp_path / "export"
    target.mkdir()

    assert release.export_clean_root(root, target, policy, ledger) == 1
    assert (target / "safe.txt").read_text(encoding="utf-8") == "committed\n"
    with pytest.raises(release.PublicReleaseError, match="export_target_not_empty"):
        release.export_clean_root(root, target, policy, ledger)


def test_export_keeps_the_initial_commit_when_head_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    root, policy, ledger = _prepared_repo(tmp_path, selected={"safe.txt": "first\n"})
    original_tree = release._git_tree

    def move_head_after_commit_is_resolved(repo: Path, commit: str) -> Any:
        (root / "safe.txt").write_text("second\n", encoding="utf-8")
        _commit(root)
        return original_tree(repo, commit)

    monkeypatch.setattr(release, "_git_tree", move_head_after_commit_is_resolved)
    target = tmp_path / "fixed-commit"
    target.mkdir()

    assert release.export_clean_root(root, target, policy, ledger) == 1
    assert (target / "safe.txt").read_text(encoding="utf-8") == "first\n"


def test_untracked_and_missing_provenance_are_rejected(tmp_path: Path) -> None:
    release = _load_module()
    root = _git_repo(tmp_path / "untracked")
    (root / "private.txt").write_text("safe\n", encoding="utf-8")
    policy = _policy(root / "allow.json", paths=["private.txt"])
    ledger = _ledger(root / "ledger.json", paths=["private.txt"])
    _git(root, "add", "allow.json", "ledger.json")
    _git(root, "commit", "-qm", "policy only")
    target = tmp_path / "export-untracked"
    target.mkdir()

    with pytest.raises(release.PublicReleaseError, match="allowlist_missing_tracked_path"):
        release.export_clean_root(root, target, policy, ledger)

    root, policy, ledger = _prepared_repo(
        tmp_path / "missing-owner",
        selected={"safe.txt": "safe\n"},
        ledger_paths=["other.txt"],
    )
    target = tmp_path / "export-owner"
    target.mkdir()
    with pytest.raises(release.PublicReleaseError, match="provenance_owner_invalid"):
        release.export_clean_root(root, target, policy, ledger)


@pytest.mark.parametrize(
    ("decision", "source_status", "rights_status"),
    [
        ("blocked", "known", "cleared"),
        ("approved", "unknown", "cleared"),
        ("approved", "known", "unclear"),
    ],
)
def test_unapproved_or_unknown_rights_are_rejected(
    tmp_path: Path,
    decision: str,
    source_status: str,
    rights_status: str,
) -> None:
    release = _load_module()
    ledger = _ledger(
        tmp_path / "ledger.json",
        paths=["safe.txt"],
        decision=decision,
        source_status=source_status,
        rights_status=rights_status,
    )
    with pytest.raises(release.PublicReleaseError, match="provenance_not_cleared"):
        release.validate_provenance(("safe.txt",), json.loads(ledger.read_text()))


def test_missing_scan_input_fails_closed(tmp_path: Path) -> None:
    release = _load_module()
    with pytest.raises(release.PublicReleaseError, match="scan_input_missing"):
        release.scan_path(tmp_path / "missing", {"schema_version": 1})


def test_digest_pinned_archive_skips_content_rules_but_keeps_structure(
    tmp_path: Path,
) -> None:
    release = _load_module()
    trusted = tmp_path / "trusted.whl"
    with zipfile.ZipFile(trusted, "w") as archive:
        sensitive = "/" + "home/fixture and " + "api" + "_key=fixture-" + "secret-value"
        archive.writestr("package/data.txt", sensitive)
    data = trusted.read_bytes()
    policy = {"trusted_archive_sha256": [hashlib.sha256(data).hexdigest()]}

    assert release._scan_snapshots({"trusted.whl": data}, policy) == ()
    assert any(
        finding.rule_id in {"private_path", "credential"}
        for finding in release._scan_snapshots({"trusted.whl": data}, {})
    )

    unsafe = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape", "safe")
    unsafe_data = unsafe.read_bytes()
    findings = release._scan_snapshots(
        {"unsafe.whl": unsafe_data},
        {"trusted_archive_sha256": [hashlib.sha256(unsafe_data).hexdigest()]},
    )
    assert any(finding.reason_code == "unsafe_archive_member" for finding in findings)

    linked = tmp_path / "linked.tar.gz"
    with tarfile.open(linked, "w:gz") as archive:
        target = tarfile.TarInfo("python/bin/python3.11")
        target.size = 0
        archive.addfile(target)
        link = tarfile.TarInfo("python/bin/python3")
        link.type = tarfile.SYMTYPE
        link.linkname = "python3.11"
        archive.addfile(link)
    linked_data = linked.read_bytes()
    assert (
        release._scan_snapshots(
            {"linked.tar.gz": linked_data},
            {"trusted_archive_sha256": [hashlib.sha256(linked_data).hexdigest()]},
        )
        == ()
    )


def test_sensitive_text_encodings_and_report_do_not_echo_secret(tmp_path: Path) -> None:
    release = _load_module()
    secret = "api" + "_key=very-secret-value-123"
    (tmp_path / "plain.txt").write_text(secret, encoding="utf-8")
    (tmp_path / "wide.txt").write_bytes(secret.encode("utf-16-le"))
    (tmp_path / "ordinary.txt").write_text(
        "api" + "_key=abcdef0123456789",
        encoding="utf-8",
    )
    (tmp_path / "known-key.txt").write_text(
        "BAIDU_MAP_" + "AK=abcdef0123456789",
        encoding="utf-8",
    )
    (tmp_path / "uppercase.txt").write_text(
        "api" + "_key=ABCDEF0123456789ABCDEF",
        encoding="utf-8",
    )
    (tmp_path / "uppercase-word.txt").write_text(
        "api" + "_key=THISISAREALPRODUCTIONTOKEN",
        encoding="utf-8",
    )
    (tmp_path / "uppercase-password.txt").write_text(
        "password=" + "ACTUALPRODUCTIONPASSWORD",
        encoding="utf-8",
    )
    (tmp_path / "underscore-secret.txt").write_text(
        "token=" + "_ACTUALPRODUCTIONTOKEN",
        encoding="utf-8",
    )
    (tmp_path / "letters.txt").write_text(
        "token=" + "abcdefghijklmnop",
        encoding="utf-8",
    )
    (tmp_path / "placeholder.txt").write_text(
        "api" + "_key=${EXAMPLE_API_KEY}",
        encoding="utf-8",
    )
    (tmp_path / "placeholder-bearer.txt").write_text(
        "Bearer " + "EXAMPLE_API_TOKEN",
        encoding="utf-8",
    )
    (tmp_path / "placeholder-human.txt").write_text(
        "api" + "_key=your-api-key-here",
        encoding="utf-8",
    )
    (tmp_path / "placeholder-code.txt").write_text(
        "\n".join(
            (
                "api" + "_key=os.environ.get",
                "token=" + "gateway.token",
                "secret=" + "{self._api_key}",
                "api" + "_key=GEMINI_API_KEY",
                "api" + "_key=$OPENAI_API_KEY",
                "token=" + "_load_token()",
            )
        ),
        encoding="utf-8",
    )

    findings = release.scan_path(tmp_path, {"schema_version": 1})

    assert {item.path for item in findings} == {
        "known-key.txt",
        "letters.txt",
        "ordinary.txt",
        "plain.txt",
        "underscore-secret.txt",
        "uppercase-password.txt",
        "uppercase-word.txt",
        "uppercase.txt",
        "wide.txt",
    }
    rendered = json.dumps([release.asdict(item) for item in findings])
    assert "very-secret-value-123" not in rendered
    assert all(set(item) == {"path", "rule_id", "reason_code"} for item in json.loads(rendered))


def test_minified_boolean_is_not_an_internal_review_id(tmp_path: Path) -> None:
    release = _load_module()
    (tmp_path / "app.js").write_text("const enabled=!0;", encoding="utf-8")
    (tmp_path / "note.txt").write_text("internal review !" + "123", encoding="utf-8")

    findings = release.scan_path(tmp_path, {"schema_version": 1})

    assert [(item.path, item.rule_id) for item in findings] == [("note.txt", "internal_review")]


def test_zip_tar_xz_and_nested_archives_are_scanned_safely(tmp_path: Path) -> None:
    release = _load_module()
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("payload.txt", "small platform marker: " + "x" + "hs")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("nested.zip", nested.getvalue())
        archive.writestr("../escape.txt", "safe")
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")

    findings = release.scan_path(outer, {"schema_version": 1})
    assert {finding.rule_id for finding in findings} == {"archive_path"}

    tar_path = tmp_path / "unsafe.tar.xz"
    with tarfile.open(tar_path, "w:xz") as archive:
        info = tarfile.TarInfo("../../escape")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"safe"))
    assert release.scan_path(tar_path, {"schema_version": 1})[0].rule_id == "archive_path"


def test_archive_metadata_is_scanned_without_exposing_member_names(tmp_path: Path) -> None:
    release = _load_module()
    archive_path = tmp_path / "metadata.zip"
    private_name = "Users/private-person/private.txt"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.comment = b"to" + b"ken=metadata-secret-value-" + b"123"
        archive.writestr(private_name, "safe")

    findings = release.scan_path(archive_path, {"schema_version": 1})
    rendered = json.dumps([release.asdict(item) for item in findings])

    assert {item.rule_id for item in findings} >= {"credential"}
    assert private_name not in rendered
    assert "metadata-secret-value" not in rendered
    assert all("member[" in item.path or item.path.endswith("!metadata") for item in findings)


def test_zip_extra_tar_owner_and_special_members_are_scanned(tmp_path: Path) -> None:
    release = _load_module()
    zip_path = tmp_path / "extra.zip"
    metadata = ("token=" + "archive-metadata-123").encode("utf-16-le")
    info = zipfile.ZipInfo("safe.txt")
    info.extra = struct.pack("<HH", 0xCAFE, len(metadata)) + metadata
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(info, "safe")

    assert "credential" in {
        item.rule_id for item in release.scan_path(zip_path, {"schema_version": 1})
    }

    tar_path = tmp_path / "owner.tar"
    with tarfile.open(tar_path, "w") as archive:
        regular = tarfile.TarInfo("safe.txt")
        regular.uname = "token=" + "owner-metadata-123"
        regular.size = 4
        archive.addfile(regular, io.BytesIO(b"safe"))
        fifo = tarfile.TarInfo("pipe")
        fifo.type = tarfile.FIFOTYPE
        archive.addfile(fifo)

    rules = {item.rule_id for item in release.scan_path(tar_path, {"schema_version": 1})}
    assert rules >= {"credential", "archive_path"}


def test_zip_security_extra_cannot_hide_path_or_symlink(tmp_path: Path) -> None:
    release = _load_module()
    archive_path = tmp_path / "security-extra.zip"
    path_info = zipfile.ZipInfo("safe-path.txt")
    encoded_name = path_info.filename.encode("cp437")
    unicode_path = b"\x01" + struct.pack("<I", zlib.crc32(encoded_name)) + b"../../escape"
    path_info.extra = struct.pack("<HH", 0x7075, len(unicode_path)) + unicode_path

    link_info = zipfile.ZipInfo("safe-link.txt")
    asi_payload = struct.pack("<HI", stat.S_IFLNK | 0o777, 0)
    asi_payload = struct.pack("<I", zlib.crc32(asi_payload)) + asi_payload
    link_info.extra = struct.pack("<HH", 0x756E, len(asi_payload)) + asi_payload
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(path_info, "safe")
        archive.writestr(link_info, "target")

    findings = release.scan_path(archive_path, {"schema_version": 1})

    assert [item.rule_id for item in findings].count("archive_path") == 2


@pytest.mark.parametrize("kind", ["path", "symlink"])
def test_zip_local_only_security_extra_is_rejected(tmp_path: Path, kind: str) -> None:
    release = _load_module()
    info = zipfile.ZipInfo("safe.txt")
    if kind == "path":
        encoded_name = info.filename.encode("cp437")
        payload = b"\x01" + struct.pack("<I", zlib.crc32(encoded_name)) + b"../../escape"
        field_id = 0x7075
    else:
        payload = struct.pack("<HI", stat.S_IFLNK | 0o777, 0)
        payload = struct.pack("<I", zlib.crc32(payload)) + payload
        field_id = 0x756E
    info.extra = struct.pack("<HH", field_id, len(payload)) + payload
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, "safe")
    archive_path = tmp_path / f"local-{kind}.zip"
    archive_path.write_bytes(_without_central_extra(buffer.getvalue()))

    findings = release.scan_path(archive_path, {"schema_version": 1})

    assert {item.rule_id for item in findings} == {"archive_path"}


def test_zip_local_and_central_asi_fields_must_match(tmp_path: Path) -> None:
    release = _load_module()
    info = zipfile.ZipInfo("safe.txt")
    payload = struct.pack("<HI", stat.S_IFREG | 0o644, 0)
    payload = struct.pack("<I", zlib.crc32(payload)) + payload
    info.extra = struct.pack("<HH", 0x756E, len(payload)) + payload
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, "safe")
    archive_path = tmp_path / "asi-fields-differ.zip"
    archive_path.write_bytes(_change_local_asi_sizdev(buffer.getvalue()))

    findings = release.scan_path(archive_path, {"schema_version": 1})

    assert {item.rule_id for item in findings} == {"archive_path"}


def test_unknown_archive_source_map_and_archive_budgets_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    disguised = tmp_path / "payload.bin"
    with zipfile.ZipFile(disguised, "w") as archive:
        archive.writestr("safe.txt", "safe")
    source_map = tmp_path / "app.js.map"
    source_map.write_text("{}", encoding="utf-8")

    findings = release.scan_path(tmp_path, {"schema_version": 1})
    assert {(item.rule_id, item.reason_code) for item in findings} >= {
        ("archive_type", "unknown_archive"),
        ("source_map", "source_map_not_allowed"),
    }

    crowded = tmp_path / "crowded.zip"
    with zipfile.ZipFile(crowded, "w") as archive:
        archive.writestr("one.txt", "one")
        archive.writestr("two.txt", "two")
    monkeypatch.setattr(release, "_MAX_MEMBERS", 1)
    assert "archive_limit" in {
        item.rule_id for item in release.scan_path(crowded, {"schema_version": 1})
    }


def test_total_scan_budget_covers_the_whole_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    monkeypatch.setattr(release, "_MAX_TOTAL_BYTES", 10)

    findings = release._scan_snapshots(
        {"a.txt": b"123456", "b.txt": b"123456"},
        {"schema_version": 1},
    )

    assert findings == (release.Finding("b.txt", "archive_limit", "archive_limit_exceeded"),)


def test_archive_count_budget_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("safe.txt", "safe")
    outer = tmp_path / "archive-count.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("one.zip", nested.getvalue())
        archive.writestr("two.zip", nested.getvalue())
    monkeypatch.setattr(release, "_MAX_ARCHIVES", 2)

    assert "archive_limit" in {
        item.rule_id for item in release.scan_path(outer, {"schema_version": 1})
    }


def test_nested_archive_budget_exhaustion_stops_the_parent(tmp_path: Path) -> None:
    release = _load_module()
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", "a" * 20_000)
    outer = tmp_path / "nested-limit.zip"
    with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested.zip", nested.getvalue())
        archive.writestr("later.txt", "token=" + "later-secret-value-123")
    release._MAX_FILE_BYTES = 1_000

    rules = {item.rule_id for item in release.scan_path(outer, {"schema_version": 1})}

    assert rules == {"archive_limit"}


def test_filesystem_budget_is_checked_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    (tmp_path / "a.txt").write_bytes(b"123456")
    (tmp_path / "b.txt").write_bytes(b"123456")
    monkeypatch.setattr(release, "_MAX_TOTAL_BYTES", 10)

    def unexpected_read(_path: Path) -> bytes:
        pytest.fail("candidate content was read before the aggregate size preflight")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)

    assert release.scan_files(
        tmp_path,
        ("a.txt", "b.txt"),
        {"schema_version": 1},
    ) == (release.Finding("b.txt", "archive_limit", "archive_limit_exceeded"),)


def test_filesystem_replacement_between_preflight_and_open_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    sample = tmp_path / "sample.txt"
    sample.write_text("safe", encoding="utf-8")
    original_open = release.os.open

    def replace_then_open(path: Path, flags: int) -> int:
        sample.write_text("changed after preflight", encoding="utf-8")
        return original_open(path, flags)

    monkeypatch.setattr(release.os, "open", replace_then_open)

    with pytest.raises(release.PublicReleaseError, match="scan_input_changed"):
        release.scan_files(tmp_path, ("sample.txt",), {"schema_version": 1})


def test_export_budget_is_checked_before_selected_blobs_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    root, policy, ledger = _prepared_repo(tmp_path, selected={"safe.txt": "12345678901"})
    tree = release._git_tree(root, release._git_commit(root))
    safe_blob = tree["safe.txt"]
    original_blob = release._git_blob
    control_size = tree["allow.json"].size + tree["ledger.json"].size
    monkeypatch.setattr(release, "_MAX_TOTAL_BYTES", control_size + safe_blob.size - 1)

    def guarded_blob(repo: Path, blob: Any) -> bytes:
        if blob == safe_blob:
            pytest.fail("selected blob was read before the aggregate size preflight")
        return original_blob(repo, blob)

    monkeypatch.setattr(release, "_git_blob", guarded_blob)
    target = tmp_path / "oversized"
    target.mkdir()

    with pytest.raises(release.PublicReleaseError, match="source_size_limit"):
        release.export_clean_root(root, target, policy, ledger)


def test_control_blobs_are_bounded_before_they_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _load_module()
    root, policy, ledger = _prepared_repo(tmp_path, selected={"safe.txt": "safe"})
    tree = release._git_tree(root, release._git_commit(root))
    monkeypatch.setattr(release, "_MAX_FILE_BYTES", tree["allow.json"].size - 1)

    def unexpected_blob(_repo: Path, _blob: Any) -> bytes:
        pytest.fail("control blob was read before its size preflight")

    monkeypatch.setattr(release, "_git_blob", unexpected_blob)
    target = tmp_path / "control-limit"
    target.mkdir()

    with pytest.raises(release.PublicReleaseError, match="control_size_limit"):
        release.export_clean_root(root, target, policy, ledger)


def test_policy_path_is_lexical_and_ignores_worktree_symlinks(tmp_path: Path) -> None:
    release = _load_module()
    root, policy, ledger = _prepared_repo(tmp_path, selected={"safe.txt": "safe"})
    policy.unlink()
    policy.symlink_to(ledger.name)
    target = tmp_path / "lexical-policy"
    target.mkdir()

    assert release.export_clean_root(root, target, policy, ledger) == 1
    assert (target / "safe.txt").read_text(encoding="utf-8") == "safe"


def test_common_ci_identity_words_are_not_sensitive_content(tmp_path: Path) -> None:
    release = _load_module()
    sample = tmp_path / "readme.txt"
    sample.write_text("Ubuntu runner setup", encoding="utf-8")

    assert release.scan_path(sample, {"schema_version": 1}) == ()


def test_redaction_exception_is_digest_pinned(tmp_path: Path) -> None:
    release = _load_module()
    source = "internal review !" + "123"
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    policy = {
        "schema_version": 1,
        "redaction_exceptions": [
            {
                "path": "sample.py",
                "rule_ids": ["internal_review"],
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
            }
        ],
    }

    assert release.scan_path(sample, policy) == ()
    sample.write_text(source + "\nchanged", encoding="utf-8")
    assert {item.rule_id for item in release.scan_path(sample, policy)} == {"internal_review"}
