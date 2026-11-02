#!/usr/bin/env bash
# Run a command with a credential-free Docker client configuration.
#
# This keeps local WSL/CI checks independent from a host Docker Desktop
# credential helper that may not be installed in the current environment.
# An explicitly supplied DOCKER_CONFIG remains authoritative.
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "usage: $0 <command> [args...]" >&2
    exit 2
fi

if [[ -n "${DOCKER_CONFIG:-}" ]]; then
    exec "$@"
fi

docker_config_dir="$(mktemp -d /tmp/domoai-docker-config.XXXXXX)"
cleanup() {
    rm -rf -- "$docker_config_dir"
}
trap cleanup EXIT

printf '%s\n' '{"auths":{}}' >"$docker_config_dir/config.json"
export DOCKER_CONFIG="$docker_config_dir"

exec "$@"
