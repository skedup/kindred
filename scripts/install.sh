#!/bin/sh
set -eu

VERSION=0.4.0
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
[ -f "$TMP/stage/python-runtime.tar.gz" ] && [ -d "$TMP/stage/wheelhouse" ] && [ -f "$TMP/stage/services/xhs-mcp-sidecar.tar.gz" ] || fail 'bundle layout is incomplete'
LC_ALL=C tar -xzf "$TMP/stage/python-runtime.tar.gz" -C "$TMP/stage"; rm "$TMP/stage/python-runtime.tar.gz"
[ -x "$TMP/stage/python/bin/python3" ] || fail 'Python runtime layout is incomplete'

SIDECAR="$TMP/stage/services/xhs-mcp-sidecar.tar.gz"
LC_ALL=C tar -tzf "$SIDECAR" >"$TMP/sidecar-members"
sidecar_root=
while IFS= read -r member; do
  case "/$member/" in */../*|//*|*\\*) fail 'sidecar contains an unsafe path' ;; esac
  root=${member%%/*}; [ -n "$root" ] || fail 'sidecar layout is incomplete'
  [ -z "$sidecar_root" ] && sidecar_root=$root
  [ "$root" = "$sidecar_root" ] || fail 'sidecar layout is incomplete'
done <"$TMP/sidecar-members"
LC_ALL=C tar -tvzf "$SIDECAR" | awk 'substr($1,1,1) !~ /^[-d]$/ { exit 1 }' || fail 'sidecar contains links or special files'
mkdir "$TMP/stage/services/xhs-mcp"; LC_ALL=C tar -xzf "$SIDECAR" --strip-components=1 -C "$TMP/stage/services/xhs-mcp"; rm "$SIDECAR"
[ -x "$TMP/stage/services/xhs-mcp/bin/kindred-xhs" ] || fail 'sidecar layout is incomplete'

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

"$TMP/stage/python/bin/python3" "$TMP/install.py" verify "$TMP/manifest.json" "$TMP/stage/wheelhouse" "$TMP/$BUNDLE" "$PLATFORM" "$VERSION" "${KINDRED_NO_WEB:-0}"
PREVIOUS="$RUNTIME_ROOT/.$VERSION.previous"
[ ! -e "$PREVIOUS" ] || fail 'a previous interrupted installation requires operator cleanup'
[ ! -e "$FINAL" ] || mv "$FINAL" "$PREVIOUS"
if ! mv "$TMP/stage" "$FINAL"; then
  [ ! -e "$PREVIOUS" ] || mv "$PREVIOUS" "$FINAL"
  fail 'runtime publish failed'
fi

restore_previous() {
  rm -rf "$FINAL"
  [ ! -e "$PREVIOUS" ] || mv "$PREVIOUS" "$FINAL"
  fail "$1"
}

"$FINAL/python/bin/python3" -m venv "$FINAL/venv" || restore_previous 'runtime environment creation failed'
"$FINAL/venv/bin/python" "$TMP/install.py" install "$TMP/manifest.json" "$FINAL/wheelhouse" "$TMP/$BUNDLE" "$PLATFORM" "$VERSION" "${KINDRED_NO_WEB:-0}" || restore_previous 'wheel installation failed'
"$FINAL/venv/bin/python" -m kindred.runtime.materialize_assets || restore_previous 'runtime assets materialization failed'

printf '%s\n' "$VERSION" >"$FINAL/.kindred-release-version"; mkdir -p "$BIN_DIR"; ln -sfn "$FINAL/venv/bin/kindred" "$BIN_DIR/kindred"
rm -rf "$PREVIOUS"
[ -t 1 ] && [ -r /dev/tty ] && exec "$BIN_DIR/kindred" install </dev/tty >/dev/tty
printf 'Kindred runtime installed. Continue in a terminal:\n  %s install\n' "$BIN_DIR/kindred"
