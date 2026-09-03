#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)

kv_host=${DOMOAI_KNX_KV_HOST:-172.26.80.1}
kv_port=${DOMOAI_KNX_KV_PORT:-3671}
gateway_port=${DOMOAI_KNX_GATEWAY_PORT:-3672}
upstream_source_port=${DOMOAI_KNX_UPSTREAM_SOURCE_PORT:-3673}
tools_dir=${DOMOAI_KNX_TOOLS_DIR:-$repo_root/.lab-tools/knxd}
knxd_bin=$tools_dir/usr/bin/knxd
library_dir=$tools_dir/usr/lib/x86_64-linux-gnu

if ! command -v ip >/dev/null 2>&1; then
  echo "iproute2 is required to discover the WSL address" >&2
  exit 1
fi

wsl_ip=$(ip route get "$kv_host" | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}')
if [[ ! "$wsl_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
  echo "could not determine the WSL address used to reach $kv_host" >&2
  exit 1
fi

if [[ ! -x "$knxd_bin" ]]; then
  for command_name in apt-get dpkg-deb; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      echo "$command_name is required to bootstrap the user-local knxd bundle" >&2
      exit 1
    fi
  done

  package_dir=$(mktemp -d)
  (cd "$package_dir" && apt-get download knxd libusb-1.0-0 libinih1 libev4t64 libfmt10)
  mkdir -p "$tools_dir"
  for package in "$package_dir"/*.deb; do
    dpkg-deb -x "$package" "$tools_dir"
  done
fi

if [[ ! -x "$knxd_bin" ]]; then
  echo "knxd was not installed under $tools_dir" >&2
  exit 1
fi

runtime_config=$(mktemp)
trap 'rm -f "$runtime_config"' EXIT
sed \
  -e "s|@KV_HOST@|$kv_host|g" \
  -e "s|@KV_PORT@|$kv_port|g" \
  -e "s|@GATEWAY_PORT@|$gateway_port|g" \
  -e "s|@UPSTREAM_SOURCE_PORT@|$upstream_source_port|g" \
  "$script_dir/knxd-wsl.conf.in" > "$runtime_config"

export LD_LIBRARY_PATH="$library_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$knxd_bin" "$runtime_config" main
