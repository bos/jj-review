#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tools/install-jj-release.sh <version> [install-dir]

Download a released jj binary for the current platform into an isolated path
without building from source. Prints the directory containing the installed
`jj` binary on stdout.

Examples:
  tools/install-jj-release.sh v0.39.0
  PATH="$(tools/install-jj-release.sh v0.28.2):$PATH" ./check.py
  tools/install-jj-release.sh 0.39.0 .tmp/jj/v0.39.0
EOF
}

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as file:
    for chunk in iter(lambda: file.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

expected_sha256() {
  case "$1/$2" in
    v0.39.0/aarch64-apple-darwin)
      printf '%s\n' "525ee96fd1eda1be925b3827a964c58a9f14bc2bae411bd7d8422fe1af40ea19"
      ;;
    v0.39.0/x86_64-apple-darwin)
      printf '%s\n' "cdf0eb6f457165bfe5edc3afc16a5d10b3ea89cd682ebe333dabdec626373104"
      ;;
    v0.39.0/aarch64-unknown-linux-musl)
      printf '%s\n' "15bbb0199adf57929d1e3cd90ae0b47356858cbe374814769815a1fb87d5ad1d"
      ;;
    v0.39.0/x86_64-unknown-linux-musl)
      printf '%s\n' "8da8d96e9c8696c21ad47847a63d533e249acb0449d9af0f0562b5ea7b024f04"
      ;;
    v0.40.0/aarch64-apple-darwin)
      printf '%s\n' "8a1d713103bb968c771617c9b2c48b0b5982193090ee74dec935bff710af2082"
      ;;
    v0.40.0/x86_64-apple-darwin)
      printf '%s\n' "ce62cf26e3c6c72a295f5917056e33cfa972874f882a2d15b5a3687b3ddce1e5"
      ;;
    v0.40.0/aarch64-unknown-linux-musl)
      printf '%s\n' "b26f24ff7a34838fbafe8788e6a94a9cdcf51601ef8c9af8fab4fa22c06ddbee"
      ;;
    v0.40.0/x86_64-unknown-linux-musl)
      printf '%s\n' "5c8979f46873e052f59bdd9535636dca6e6f9f70571b73f6d63c3b92acfaa037"
      ;;
    *)
      echo "unsupported jj version for checksum verification: $1" >&2
      exit 1
      ;;
  esac
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage >&2
  exit 2
fi

version="$1"
if [[ "$version" != v* ]]; then
  version="v$version"
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_dir="${2:-$repo_root/.tmp/jj-releases/$version}"

platform="$(uname -s)"
arch="$(uname -m)"

case "$platform/$arch" in
  Darwin/arm64)
    target="aarch64-apple-darwin"
    ;;
  Darwin/x86_64)
    target="x86_64-apple-darwin"
    ;;
  Linux/aarch64)
    target="aarch64-unknown-linux-musl"
    ;;
  Linux/x86_64)
    target="x86_64-unknown-linux-musl"
    ;;
  *)
    echo "unsupported platform for release binaries: $platform/$arch" >&2
    exit 1
    ;;
esac

bin_dir="$install_dir/bin"
jj_path="$bin_dir/jj"
if [[ -x "$jj_path" ]]; then
  printf '%s\n' "$bin_dir"
  exit 0
fi

asset="jj-$version-$target.tar.gz"
url="https://github.com/jj-vcs/jj/releases/download/$version/$asset"
expected_sha="$(expected_sha256 "$version" "$target")"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

mkdir -p "$bin_dir"
archive_path="$tmp_dir/$asset"
curl --fail --location --silent --show-error --output "$archive_path" "$url"
actual_sha="$(sha256_file "$archive_path")"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "checksum verification failed for $asset" >&2
  echo "expected: $expected_sha" >&2
  echo "actual:   $actual_sha" >&2
  exit 1
fi
tar -xzf "$archive_path" -C "$tmp_dir"
install -m 0755 "$tmp_dir/jj" "$jj_path"

printf '%s\n' "$bin_dir"
