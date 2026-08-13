#!/bin/sh
set -eu

VERSION=0.2.1; OPENCLAW_VERSION='OpenClaw 2026.6.10 (aa69b12)'
BASE_URL=${KINDRED_RELEASE_BASE_URL:-https://github.com/skedup/kindred/releases/download/v$VERSION}
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}; RUNTIME_ROOT="$DATA_HOME/kindred/runtime"
BIN_DIR="$HOME/.local/bin"

fail() { printf 'kindred bootstrap: %s\n' "$1" >&2; exit 2; }

sha256_file() {
  command -v shasum >/dev/null 2>&1 && LC_ALL=C shasum -a 256 "$1" | awk '{print $1}' && return
  command -v sha256sum >/dev/null 2>&1 && LC_ALL=C sha256sum "$1" | awk '{print $1}' && return
  fail 'SHA-256 tool is unavailable'
}

platform() {
  case "$(uname -s)/$(uname -m)" in
  Darwin/arm64)
    major=$(sw_vers -productVersion | awk -F. '{print $1}')
    [ "$major" -ge 14 ] || fail 'macOS 14+ is required'
    printf '%s\n' macos-arm64 ;;
  Linux/x86_64)
    release_file=${KINDRED_OS_RELEASE_FILE:-/etc/os-release}
    # shellcheck disable=SC1090
    . "$release_file"
    [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || fail 'Ubuntu 24.04 is required'
    printf '%s\n' ubuntu24-x86_64 ;;
  *) fail 'unsupported OS or CPU' ;;
  esac
}

[ "$(openclaw --version 2>/dev/null || true)" = "$OPENCLAW_VERSION" ] || fail 'unsupported OpenClaw version or build'
PLATFORM=$(platform)

for marker in "$RUNTIME_ROOT"/*/.kindred-release-version; do [ ! -f "$marker" ] || [ "$(cat "$marker")" = "$VERSION" ] || fail 'a different Kindred version is already installed'; done

TMP=$(mktemp -d "${TMPDIR:-/tmp}/kindred-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
BUNDLE="kindred-v$VERSION-$PLATFORM.tar.gz"
for asset in SHA256SUMS manifest.json "$BUNDLE"; do curl -fsSL --retry 0 "$BASE_URL/$asset" -o "$TMP/$asset" || fail 'release asset download failed'; done

expected=$(awk -v name="$BUNDLE" '$2 == name {print $1}' "$TMP/SHA256SUMS")
manifest_expected=$(awk '$2 == "manifest.json" {print $1}' "$TMP/SHA256SUMS")
[ -n "$expected" ] && [ -n "$manifest_expected" ] || fail 'checksum manifest is incomplete'
[ "$(sha256_file "$TMP/$BUNDLE")" = "$expected" ] || fail 'bundle checksum mismatch'
[ "$(sha256_file "$TMP/manifest.json")" = "$manifest_expected" ] || fail 'manifest checksum mismatch'

LC_ALL=C tar -tzf "$TMP/$BUNDLE" >"$TMP/members"
while IFS= read -r member; do case "/$member/" in */../*|//*|*\\*) fail 'bundle contains an unsafe path' ;; esac; done <"$TMP/members"
LC_ALL=C tar -tvzf "$TMP/$BUNDLE" | awk 'substr($1,1,1) !~ /^[-d]$/ { exit 1 }' || fail 'bundle contains links or special files'

mkdir -p "$RUNTIME_ROOT"; FINAL="$RUNTIME_ROOT/$VERSION"
mkdir "$TMP/stage"; LC_ALL=C tar -xzf "$TMP/$BUNDLE" -C "$TMP/stage"
[ -f "$TMP/stage/python-runtime.tar.gz" ] && [ -d "$TMP/stage/wheelhouse" ] || fail 'bundle layout is incomplete'
LC_ALL=C tar -xzf "$TMP/stage/python-runtime.tar.gz" -C "$TMP/stage"; rm "$TMP/stage/python-runtime.tar.gz"
[ -x "$TMP/stage/python/bin/python3" ] || fail 'Python runtime layout is incomplete'

cat >"$TMP/install.py" <<'PY'
import hashlib, json, pathlib, subprocess, sys
action, manifest_arg, wheelhouse_arg, bundle_arg, platform, version, no_web = sys.argv[1:]
manifest_path, wheelhouse, bundle = map(pathlib.Path, (manifest_arg, wheelhouse_arg, bundle_arg))
manifest = json.loads(manifest_path.read_text())
selected = manifest["platforms"][platform]
if (manifest.get("schema_version") != 1 or manifest["release_version"] != version
        or bundle.name != selected["bundle"]["filename"]
        or bundle.stat().st_size != selected["bundle"]["size"]
        or hashlib.sha256(bundle.read_bytes()).hexdigest() != selected["bundle"]["sha256"]):
    raise SystemExit("release metadata mismatch")
files = []
for wheel in selected["wheels"]:
    path = pathlib.Path(wheelhouse) / wheel["filename"]
    if (not path.is_file() or path.stat().st_size != wheel["size"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != wheel["sha256"]):
        raise SystemExit("wheel identity mismatch")
    if no_web != "1" or wheel["group"] != "web":
        files.append(str(path))
if action == "install":
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                    "--find-links", str(wheelhouse), *files], check=True)
PY

"$TMP/stage/python/bin/python3" "$TMP/install.py" verify "$TMP/manifest.json" "$TMP/stage/wheelhouse" "$TMP/$BUNDLE" "$PLATFORM" "$VERSION" "${KINDRED_NO_WEB:-0}"; rm -rf "$FINAL"; mv "$TMP/stage" "$FINAL"
"$FINAL/python/bin/python3" -m venv "$FINAL/venv"; "$FINAL/venv/bin/python" "$TMP/install.py" install "$TMP/manifest.json" "$FINAL/wheelhouse" "$TMP/$BUNDLE" "$PLATFORM" "$VERSION" "${KINDRED_NO_WEB:-0}"

printf '%s\n' "$VERSION" >"$FINAL/.kindred-release-version"; mkdir -p "$BIN_DIR"; ln -sfn "$FINAL/venv/bin/kindred" "$BIN_DIR/kindred"
[ -t 1 ] && [ -r /dev/tty ] && exec "$BIN_DIR/kindred" openclaw install </dev/tty >/dev/tty
printf 'Kindred runtime installed. Continue in a terminal:\n  %s openclaw install\n' "$BIN_DIR/kindred"
