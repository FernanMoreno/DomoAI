#!/usr/bin/env bash
set -euo pipefail

readonly repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly caddy_image="caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
readonly junit_path="$repo_root/junit-docker-composition.xml"

cd "$repo_root"

# This is a mandatory job. A missing daemon is an infrastructure failure, not
# a valid reason to turn the real-dependency test into a green skip.
docker info

uv run pytest -q tests/composition/test_zigbee2mqtt_broker_composition.py \
  --junitxml="$junit_path" -rs

uv run python - "$junit_path" <<'PY'
import sys
import xml.etree.ElementTree as element_tree

root = element_tree.parse(sys.argv[1]).getroot()
skipped = root.findall(".//skipped")
testcases = root.findall(".//testcase")
if skipped:
    raise SystemExit(f"mandatory Docker composition produced {len(skipped)} skipped test(s)")
if not testcases:
    raise SystemExit("mandatory Docker composition executed no test cases")
print(f"mandatory Docker composition: {len(testcases)} test(s), 0 skipped")
PY

tls_dir="$(mktemp -d)"
cleanup() {
  rm -f "$tls_dir/fullchain.pem" "$tls_dir/privkey.pem"
  rmdir "$tls_dir"
}
trap cleanup EXIT

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$tls_dir/privkey.pem" \
  -out "$tls_dir/fullchain.pem" \
  -days 1 -subj '/CN=mcp.example.test' \
  -addext 'subjectAltName=DNS:mcp.example.test' >/dev/null 2>&1

DOMOAI_TLS_CERT_FILE_HOST="$tls_dir/fullchain.pem" \
DOMOAI_TLS_KEY_FILE_HOST="$tls_dir/privkey.pem" \
  docker compose -f deploy/compose.yaml config >/dev/null

docker run --rm \
  -e DOMOAI_CADDY_HOSTNAME=mcp.example.test \
  -v "$repo_root/deploy/reverse-proxy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$tls_dir/fullchain.pem:/etc/caddy/certs/fullchain.pem:ro" \
  -v "$tls_dir/privkey.pem:/etc/caddy/certs/privkey.pem:ro" \
  "$caddy_image" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
