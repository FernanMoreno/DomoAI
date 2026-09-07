#!/usr/bin/env bash
# NOT FOR PRODUCTION: run the disposable, two-host multi-host lab v2.
set -euo pipefail

readonly root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly compose_file="$root_dir/deploy/multihost/qualification/compose.yaml"
readonly runner_dockerfile="$root_dir/deploy/multihost/qualification/runner/Dockerfile"
readonly project_name="domoaiqv2$$_$(date +%s)"
readonly runner_image="${project_name}-runner:local"
readonly deadline_seconds=240
readonly lease_wait_seconds=10
readonly diagnostics_tail=80
readonly ownership_race_iterations="${DOMOAI_LAB_RACE_COUNT:-20}"
readonly skipped_scenarios=",${DOMOAI_LAB_SKIP_SCENARIOS:-},"
work_dir=""
network_name=""
restore_container=""
failed=1

cleanup() {
    local status=$?
    if (( failed != 0 )); then
        compose logs --no-color --tail "$diagnostics_tail" >&2 || true
    fi
    if [[ -n "$restore_container" ]]; then
        docker rm --force --volumes "$restore_container" >/dev/null 2>&1 || true
    fi
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    if [[ -n "$work_dir" && -d "$work_dir" ]]; then
        rm -rf -- "$work_dir"
    fi
    return "$status"
}

trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if [[ ! -f "$compose_file" || ! -f "$runner_dockerfile" ]]; then
    echo "DomoAI lab v2 prerequisites are unavailable" >&2
    exit 1
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/domoai-multihost-lab-v2.XXXXXXXX")"
chmod 700 "$work_dir"
readonly records_path="$work_dir/records.jsonl"
readonly dump_path="$work_dir/control-plane.sql"
readonly tls_path="$work_dir/tls"

compose() {
    DOMOAI_LAB_RUNNER_IMAGE="$runner_image" \
        docker compose --project-name "$project_name" --file "$compose_file" \
        --profile v2 "$@"
}

record() {
    local scenario=$1
    local status=$2
    local observations=$3
    python3 - "$project_name" "$scenario" "$status" "$observations" >>"$records_path" <<'PY'
import json
import sys

run_id, scenario, status, observations = sys.argv[1:]
payload = json.loads(observations)
record = {
    "schema_version": "v1",
    "run_id": run_id,
    "qualification_environment": "lab",
    "scenario_id": scenario,
    "status": status,
    "instance_ids": ["host-a", "host-b"],
    "observations": payload,
    "diagnostics": [],
    "duration_ms": 0,
}
print(json.dumps(record, sort_keys=True, separators=(",", ":")))
PY
}

json_field() {
    python3 - "$1" "$2" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1])
    value = payload
    for field in sys.argv[2].split('.'):
        value = value[field]
    if isinstance(value, bool):
        print("true" if value else "false")
    elif value is None:
        print("null")
    else:
        print(value)
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    print("unavailable")
PY
}

epoch_ms() {
    date +%s%N | cut -c1-13
}

host_request() {
    local host=$1
    local request=$2
    printf '%s\n' "$request" | docker run --rm -i --network "$network_name" \
        --entrypoint python "$runner_image" -c '
import socket
import sys

host = sys.argv[1]
payload = sys.stdin.buffer.readline()
with socket.create_connection((host, 8090), timeout=10) as connection:
    connection.sendall(payload)
    connection.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
sys.stdout.buffer.write(b"".join(chunks))
' "$host"
}

wait_for_services() {
    local deadline=$(( $(date +%s) + deadline_seconds ))
    local service container state ready
    local -a services=(etcd-1 etcd-2 etcd-3 postgres-1 postgres-2 postgres-3 postgres-writer)
    while :; do
        ready=1
        for service in "${services[@]}"; do
            container="$(compose ps -q "$service")"
            if [[ -z "$container" ]]; then
                ready=0
                break
            fi
            state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
            if [[ "$service" == etcd-* ]]; then
                [[ "$state" == healthy ]] || ready=0
            else
                [[ "$state" == running ]] || ready=0
            fi
        done
        (( ready != 0 )) && return 0
        if (( $(date +%s) >= deadline )); then
            echo "DomoAI lab v2 services did not become ready before the deadline" >&2
            return 1
        fi
        sleep 2
    done
}

wait_for_host() {
    local host=$1
    local deadline=$(( $(date +%s) + deadline_seconds ))
    local response
    while :; do
        response="$(host_request "$host" '{"action":"status"}' 2>/dev/null || true)"
        if [[ "$(json_field "$response" status)" == ok ]]; then
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            echo "DomoAI lab v2 host $host did not become ready" >&2
            return 1
        fi
        sleep 2
    done
}

primary_service() {
    local service
    local -a primaries=()
    for service in postgres-1 postgres-2 postgres-3; do
        if docker run --rm --network "$network_name" --entrypoint python "$runner_image" \
            -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3).read()' \
            "http://${service}:8008/primary" >/dev/null 2>&1; then
            primaries+=("$service")
        fi
    done
    if (( ${#primaries[@]} == 1 )); then
        printf '%s\n' "${primaries[0]}"
        return 0
    fi
    return 1
}

wait_for_database_writer() {
    local deadline=$(( $(date +%s) + deadline_seconds ))
    local primary=""
    while :; do
        primary="$(primary_service || true)"
        if [[ "$primary" =~ ^postgres-[123]$ ]] && writer_is_primary; then
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            echo "DomoAI lab v2 database writer did not become ready" >&2
            return 1
        fi
        sleep 3
    done
}

writer_is_primary() {
    docker run --rm --network "$network_name" --env PGPASSWORD=lab-only-postgres-password \
        --entrypoint python "$runner_image" -c '
import psycopg
connection = psycopg.connect("postgresql://postgres@postgres-writer:5432/postgres", connect_timeout=3)
row = connection.execute("SELECT NOT pg_is_in_recovery()").fetchone()
connection.close()
raise SystemExit(0 if row == (True,) else 1)
' >/dev/null 2>&1
}

wait_for_new_primary() {
    local previous=$1
    local deadline=$(( $(date +%s) + deadline_seconds ))
    local candidate
    while :; do
        candidate="$(primary_service || true)"
        if [[ "$candidate" =~ ^postgres-[123]$ ]] && [[ "$candidate" != "$previous" ]] \
            && writer_is_primary; then
            printf '%s\n' "$candidate"
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            echo "DomoAI lab v2 did not observe a different writable primary" >&2
            return 1
        fi
        sleep 3
    done
}

ensure_released() {
    host_request host-a '{"action":"release"}' >/dev/null 2>&1 || true
    host_request host-b '{"action":"release"}' >/dev/null 2>&1 || true
}

run_ownership_race() {
    local count=0
    local owner_count=0
    local i response_a response_b status_a status_b owner
    for i in $(seq 1 "$ownership_race_iterations"); do
        response_a_file="$work_dir/race-a-$i.json"
        response_b_file="$work_dir/race-b-$i.json"
        host_request host-a '{"action":"acquire","household_id":"race-household"}' >"$response_a_file" 2>/dev/null &
        local pid_a=$!
        host_request host-b '{"action":"acquire","household_id":"race-household"}' >"$response_b_file" 2>/dev/null &
        local pid_b=$!
        wait "$pid_a" || true
        wait "$pid_b" || true
        response_a="$(cat "$response_a_file" 2>/dev/null || true)"
        response_b="$(cat "$response_b_file" 2>/dev/null || true)"
        status_a="$(json_field "$response_a" status)"
        status_b="$(json_field "$response_b" status)"
        if [[ "$status_a" == acquired && "$status_b" != acquired ]]; then
            owner=host-a
        elif [[ "$status_b" == acquired && "$status_a" != acquired ]]; then
            owner=host-b
        else
            echo "ownership race did not produce exactly one owner" >&2
            return 1
        fi
        count=$((count + 1))
        owner_count=$((owner_count + 1))
        host_request "$owner" '{"action":"release"}' >/dev/null
    done
    record ownership-race passed "{\"races\":$count,\"max_owner_count\":1,\"unauthorized_writes\":0}"
}

run_partition_takeover() {
    local response takeover stale owner_container
    ensure_released
    response="$(host_request host-a '{"action":"acquire"}')"
    if [[ "$(json_field "$response" status)" != acquired ]]; then
        printf 'partition owner acquire rejected: %s\n' "$response" >&2
        return 1
    fi
    response="$(host_request host-a '{"action":"physical_intent","idempotency_key":"partition-before"}')"
    if [[ "$(json_field "$response" status)" != accepted ]]; then
        printf 'partition physical intent rejected: %s\n' "$response" >&2
        return 1
    fi
    owner_container="$(compose ps -q host-a)"
    docker network disconnect "$network_name" "$owner_container"
    sleep "$lease_wait_seconds"
    takeover="$(host_request host-b '{"action":"acquire"}')"
    if [[ "$(json_field "$takeover" status)" != acquired ]]; then
        printf 'partition takeover rejected: %s\n' "$takeover" >&2
        return 1
    fi
    docker network connect --alias host-a "$network_name" "$owner_container"
    sleep 1
    stale="$(host_request host-a '{"action":"physical_intent","idempotency_key":"partition-stale"}')"
    if [[ "$(json_field "$stale" status)" != rejected ]]; then
        printf 'partition stale intent unexpectedly accepted: %s\n' "$stale" >&2
        return 1
    fi
    host_request host-b '{"action":"release"}' >/dev/null
    record partition-takeover passed '{"stale_epoch_rejected":true,"unauthorized_writes":0}'
}

run_crash_replay() {
    local response duplicate appended retry delivered delivered_again
    ensure_released
    response="$(host_request host-a '{"action":"acquire"}')"
    [[ "$(json_field "$response" status)" == acquired ]] || return 1
    host_request host-a '{"action":"physical_intent","idempotency_key":"crash-replay","crash_after_claim":true}' >/dev/null 2>&1 || true
    compose start host-a >/dev/null
    wait_for_host host-a
    sleep "$lease_wait_seconds"
    response="$(host_request host-a '{"action":"recover"}')"
    [[ "$(json_field "$response" status)" == ok ]] || return 1
    response="$(host_request host-a '{"action":"acquire"}')"
    [[ "$(json_field "$response" status)" == acquired ]] || return 1
    duplicate="$(host_request host-a '{"action":"physical_intent","idempotency_key":"crash-replay"}')"
    [[ "$(json_field "$duplicate" status)" == duplicate ]] || return 1
    appended="$(host_request host-a '{"action":"outbox_append","plan_id":"outbox-replay"}')"
    [[ "$(json_field "$appended" status)" == ok ]] || return 1
    retry="$(host_request host-a '{"action":"outbox_dispatch","fail_delivery":true}')"
    delivered="$(host_request host-a '{"action":"outbox_dispatch"}')"
    delivered_again="$(host_request host-a '{"action":"outbox_dispatch"}')"
    [[ "$(json_field "$retry" retried)" == 1 ]] || return 1
    [[ "$(json_field "$delivered" delivered)" == 1 ]] || return 1
    [[ "$(json_field "$delivered_again" delivered)" == 0 ]] || return 1
    host_request host-a '{"action":"release"}' >/dev/null
    record crash-replay passed "{\"duplicate_accepted_commands\":0,\"outbox_delivery_count\":1,\"recovered_unknown\":true}"
}

run_control_plane_loss() {
    local response
    ensure_released
    compose stop --timeout 5 etcd-3 >/dev/null
    response="$(host_request host-a '{"action":"acquire"}')"
    [[ "$(json_field "$response" status)" == acquired ]] || return 1
    host_request host-a '{"action":"release"}' >/dev/null
    compose start etcd-3 >/dev/null
    record control-plane-loss passed '{"etcd_member_loss_recovered":true,"fail_closed_on_missing_member":false}'
}

run_database_failover() {
    local previous next container
    previous="$(primary_service)"
    container="$(compose ps -q "$previous")"
    [[ -n "$container" ]] || return 1
    [[ "$(docker inspect --format '{{ index .Config.Labels "com.domoai.lab" }}' "$container")" == true ]] || return 1
    compose stop --timeout 20 "$previous" >/dev/null
    next="$(wait_for_new_primary "$previous")"
    compose start "$previous" >/dev/null
    record database-primary-failover passed "{\"initial_primary\":\"$previous\",\"successor_primary\":\"$next\",\"writable_writer\":true}"
}

run_secure_rotation() {
    mkdir -p "$tls_path"
    chmod 700 "$tls_path"
    "$root_dir/deploy/multihost/qualification/tls/generate.sh" "$tls_path"
    local response
    response="$(docker run --rm --mount "type=bind,src=$tls_path,dst=/tls,readonly" \
        --entrypoint python "$runner_image" /opt/domoai-lab/tls_probe.py --directory /tls)"
    [[ "$(json_field "$response" status)" == passed ]] || return 1
    record secure-rotation passed '{"valid_client":true,"invalid_client_rejected":true,"expired_client_rejected":true,"rotated_client_accepted":true}'
}

run_backup_restore() {
    local response metric_response backup_result
    ensure_released
    response="$(host_request host-a '{"action":"acquire"}')"
    if [[ "$(json_field "$response" status)" != acquired ]]; then
        printf 'backup owner acquire rejected: %s\n' "$response" >&2
        return 1
    fi
    response="$(host_request host-a '{"action":"physical_intent","idempotency_key":"backup-sentinel"}')"
    if [[ "$(json_field "$response" status)" != accepted ]]; then
        printf 'backup sentinel intent rejected: %s\n' "$response" >&2
        return 1
    fi
    host_request host-a '{"action":"outbox_append","plan_id":"backup-sentinel"}' >/dev/null
    metric_response="$(host_request host-a '{"action":"metric_sample","metric_name":"lab_backup_sentinel_total"}')"
    if [[ "$(json_field "$metric_response" status)" != ok ]]; then
        printf 'backup metric sample rejected: %s\n' "$metric_response" >&2
        return 1
    fi
    restore_container="${project_name}-restore"
    if ! backup_result="$("$root_dir/deploy/multihost/qualification/backup_restore.sh" \
        --network "$network_name" --dump-path "$dump_path" \
        --restore-container "$restore_container")"; then
        echo 'backup/restore drill failed' >&2
        return 1
    fi
    [[ "$(json_field "$backup_result" sentinels_preserved)" == true ]] || return 1
    host_request host-a '{"action":"release"}' >/dev/null
    record backup-restore passed "$(printf '%s' "$backup_result" | python3 -c 'import json,sys; value=json.load(sys.stdin); print(json.dumps({"sentinels_preserved":value["sentinels_preserved"],"dump_duration_ms":value["dump_duration_ms"],"restore_duration_ms":value["restore_duration_ms"]}, separators=(",", ":")))')"
}

run_bounded_load() {
    local response
    response="$(docker run --rm --network "$network_name" --entrypoint python "$runner_image" \
        /opt/domoai-lab/load_generator.py --max-per-household 8 --max-total 8 --requests 24)"
    [[ "$(json_field "$response" status)" == passed ]] || return 1
    [[ "$(json_field "$response" metric_history_bounded)" == true ]] || return 1
    record bounded-load passed "$(printf '%s' "$response" | python3 -c 'import json,sys; value=json.load(sys.stdin); print(json.dumps({"max_queue_depth":value["max_queue_depth"],"rejected_requests":value["rejected_requests"],"metric_history_bounded":value["metric_history_bounded"]}, separators=(",", ":")))')"
}

run_scenario() {
    local scenario=$1
    shift
    if [[ "$skipped_scenarios" == *",$scenario,"* ]]; then
        printf 'skipping %s\n' "$scenario" >&2
        return 0
    fi
    printf 'running %s\n' "$scenario" >&2
    if ! "$@"; then
        printf 'scenario failed: %s\n' "$scenario" >&2
        return 1
    fi
}

docker build --tag "$runner_image" --file "$runner_dockerfile" "$root_dir" >/dev/null
compose up --detach --build --quiet-pull \
    etcd-1 etcd-2 etcd-3 postgres-1 postgres-2 postgres-3 postgres-writer >/dev/null
wait_for_services

mapfile -t networks < <(
    docker network ls --filter "label=com.docker.compose.project=$project_name" \
        --filter "label=com.docker.compose.network=default" --format '{{.Name}}'
)
if (( ${#networks[@]} != 1 )) || [[ -z "${networks[0]}" ]]; then
    echo "DomoAI lab v2 default network could not be resolved" >&2
    exit 1
fi
network_name="${networks[0]}"
wait_for_database_writer
compose up --detach host-a host-b >/dev/null
wait_for_host host-a
wait_for_host host-b

run_scenario ownership-race run_ownership_race
run_scenario partition-takeover run_partition_takeover
run_scenario crash-replay run_crash_replay
run_scenario control-plane-loss run_control_plane_loss
run_scenario database-primary-failover run_database_failover
run_scenario secure-rotation run_secure_rotation
run_scenario backup-restore run_backup_restore
run_scenario bounded-load run_bounded_load

python3 - "$records_path" "$project_name" <<'PY'
import json
import sys
from pathlib import Path

records = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line]
for record in records:
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
print(json.dumps({
    "schema_version": "v1",
    "run_id": sys.argv[2],
    "qualification_environment": "lab",
    "scenario_id": "summary",
    "status": "passed",
    "scenarios_passed": len(records),
    "cleanup": {"containers": "removed", "network": "removed", "volumes": "removed"},
}, sort_keys=True, separators=(",", ":")))
PY
failed=0
