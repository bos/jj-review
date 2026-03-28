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
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

mkdir -p "$bin_dir"
archive_path="$tmp_dir/$asset"
curl --fail --location --silent --show-error --output "$archive_path" "$url"
tar -xzf "$archive_path" -C "$tmp_dir"
install -m 0755 "$tmp_dir/jj" "$jj_path"

printf '%s\n' "$bin_dir"
