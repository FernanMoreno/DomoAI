#!/usr/bin/env bash
# NOT FOR PRODUCTION: run the disposable Docker multi-host qualification lab.
set -euo pipefail

readonly root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly compose_file="$root_dir/deploy/multihost/qualification/compose.yaml"
readonly runner_dockerfile="$root_dir/deploy/multihost/qualification/runner/Dockerfile"
readonly project_name="domoaiq$$_$(date +%s)"
readonly runner_image="${project_name}-runner:local"
readonly deadline_seconds=240
readonly diagnostics_tail=80
work_dir=""
failed=1

cleanup() {
    local status=$?
    if (( failed != 0 )); then
        docker compose --project-name "$project_name" --file "$compose_file" \
            logs --no-color --tail "$diagnostics_tail" >&2 || true
    fi
    docker compose --project-name "$project_name" --file "$compose_file" \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    if [[ -n "$work_dir" && -d "$work_dir" ]]; then
        rm -rf -- "$work_dir"
    fi
    return "$status"
}

trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if [[ ! -f "$compose_file" || ! -f "$runner_dockerfile" ]]; then
    echo "DomoAI lab prerequisites are unavailable" >&2
    exit 1
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/domoai-multihost-qualification.XXXXXXXX")"
chmod 700 "$work_dir"
readonly evidence_path="$work_dir/evidence.json"
readonly state_path="$work_dir/fencing-state.json"

docker build --tag "$runner_image" --file "$runner_dockerfile" "$root_dir" >/dev/null

docker compose --project-name "$project_name" --file "$compose_file" \
    up --detach --build --quiet-pull >/dev/null

deadline=$(( $(date +%s) + deadline_seconds ))
services=(etcd-1 etcd-2 etcd-3 postgres-1 postgres-2 postgres-3 postgres-writer)
while :; do
    ready=1
    for service in "${services[@]}"; do
        container_id="$(docker compose --project-name "$project_name" --file "$compose_file" ps -q "$service")"
        if [[ -z "$container_id" ]]; then
            ready=0
            break
        fi
        state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
        if [[ "$service" == etcd-* ]]; then
            [[ "$state" == healthy ]] || ready=0
        else
            [[ "$state" == running ]] || ready=0
        fi
    done
    (( ready != 0 )) && break
    if (( $(date +%s) >= deadline )); then
        echo "DomoAI lab services did not become ready before the deadline" >&2
        exit 1
    fi
    sleep 2
done

mapfile -t networks < <(
    docker network ls \
        --filter "label=com.docker.compose.project=$project_name" \
        --filter "label=com.docker.compose.network=default" \
        --format '{{.Name}}'
)
if (( ${#networks[@]} != 1 )) || [[ -z "${networks[0]}" ]]; then
    echo "DomoAI lab default network could not be resolved from its project label" >&2
    exit 1
fi
readonly network_name="${networks[0]}"

run_qualification() {
    rm -f -- "$evidence_path"
    docker run --rm --network "$network_name" \
        --user "$(id -u):$(id -g)" \
        --mount "type=bind,src=$work_dir,dst=/lab-output" \
        --env PGPASSWORD=lab-only-postgres-password \
        "$runner_image" \
        --etcd-endpoint http://etcd-1:2379 \
        --etcd-endpoint http://etcd-2:2379 \
        --etcd-endpoint http://etcd-3:2379 \
        --postgres-dsn postgresql://postgres@postgres-writer:5432/postgres \
        --bridge-path /opt/domoai-lab/gateway_fencing_lab.py \
        --bridge-state-file /lab-output/fencing-state.json \
        --evidence-path /lab-output/evidence.json
}

wait_for_qualification() {
    local qualification_deadline=$(( $(date +%s) + deadline_seconds ))
    while :; do
        if run_qualification; then
            return 0
        fi
        if (( $(date +%s) >= qualification_deadline )); then
            echo "DomoAI lab qualification did not pass before the deadline" >&2
            return 1
        fi
        sleep 3
    done
}

primary_service() {
    local service
    local -a primaries=()
    for service in postgres-1 postgres-2 postgres-3; do
        if docker run --rm --network "$network_name" \
            --user "$(id -u):$(id -g)" \
            --entrypoint python \
            "$runner_image" \
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

writer_is_primary() {
    docker run --rm --network "$network_name" \
        --user "$(id -u):$(id -g)" \
        --env PGPASSWORD=lab-only-postgres-password \
        --entrypoint python \
        "$runner_image" \
        -c 'import psycopg; connection = psycopg.connect("postgresql://postgres@postgres-writer:5432/postgres", connect_timeout=3); row = connection.execute("SELECT NOT pg_is_in_recovery()").fetchone(); connection.close(); raise SystemExit(0 if row == (True,) else 1)' \
        >/dev/null 2>&1
}

patroni_primary_status() {
    local service=$1
    local endpoint=${2:-primary}
    docker run --rm --network "$network_name" \
        --user "$(id -u):$(id -g)" \
        --entrypoint python \
        "$runner_image" \
        -c 'import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        print(f"http_{response.status}")
except urllib.error.HTTPError as error:
    print(f"http_{error.code}")
except Exception:
    print("unavailable")' \
        "http://${service}:8008/${endpoint}" 2>/dev/null
}

writer_status() {
    docker run --rm --network "$network_name" \
        --user "$(id -u):$(id -g)" \
        --env PGPASSWORD=lab-only-postgres-password \
        --entrypoint python \
        "$runner_image" \
        -c 'import psycopg
try:
    connection = psycopg.connect("postgresql://postgres@postgres-writer:5432/postgres", connect_timeout=3)
    row = connection.execute("SELECT NOT pg_is_in_recovery()").fetchone()
    connection.close()
    print("primary" if row == (True,) else "standby")
except Exception:
    print("unavailable")' \
        2>/dev/null
}

failover_diagnostics() {
    local service status
    echo "DomoAI lab failover diagnostics:" >&2
    for service in postgres-1 postgres-2 postgres-3; do
        status="$(patroni_primary_status "$service" || true)"
        [[ "$status" =~ ^http_[0-9]{3}$|^unavailable$ ]] || status="unavailable"
        printf 'Patroni %s /primary: %s\n' "$service" "$status" >&2
    done
    status="$(writer_status || true)"
    [[ "$status" == primary || "$status" == standby || "$status" == unavailable ]] \
        || status="unavailable"
    printf 'HAProxy writer read-only probe: %s\n' "$status" >&2
    docker compose --project-name "$project_name" --file "$compose_file" \
        logs --no-color --tail 60 postgres-1 postgres-2 postgres-3 postgres-writer >&2 || true
}

wait_for_new_primary() {
    local previous_primary=$1
    local failover_deadline=$(( $(date +%s) + deadline_seconds ))
    local candidate
    while :; do
        candidate="$(primary_service || true)"
        if [[ "$candidate" =~ ^postgres-[123]$ ]] \
            && [[ "$candidate" != "$previous_primary" ]] \
            && writer_is_primary; then
            printf '%s\n' "$candidate"
            return 0
        fi
        if (( $(date +%s) >= failover_deadline )); then
            failover_diagnostics
            echo "DomoAI lab failover did not reach a different writable primary before the deadline" >&2
            return 1
        fi
        sleep 3
    done
}

wait_for_replica_members() {
    local readiness_deadline=$(( $(date +%s) + deadline_seconds ))
    local primary service status ready
    while :; do
        primary="$(primary_service || true)"
        ready=1
        if [[ "$primary" =~ ^postgres-[123]$ ]]; then
            for service in postgres-1 postgres-2 postgres-3; do
                [[ "$service" == "$primary" ]] && continue
                status="$(patroni_primary_status "$service" replica || true)"
                if [[ "$status" != http_200 ]]; then
                    ready=0
                fi
            done
        else
            ready=0
        fi
        (( ready != 0 )) && return 0
        if (( $(date +%s) >= readiness_deadline )); then
            echo "DomoAI lab replicas did not become ready before the failover exercise" >&2
            failover_diagnostics
            return 1
        fi
        sleep 3
    done
}

wait_for_qualification
wait_for_replica_members

initial_primary="$(primary_service || true)"
if [[ ! "$initial_primary" =~ ^postgres-[123]$ ]]; then
    echo "DomoAI lab could not identify exactly one Patroni primary" >&2
    exit 1
fi
initial_container="$(docker compose --project-name "$project_name" --file "$compose_file" ps -q "$initial_primary")"
if [[ -z "$initial_container" ]] \
    || [[ "$(docker inspect --format '{{ index .Config.Labels "com.domoai.lab" }}' "$initial_container")" != true ]]; then
    echo "DomoAI lab refuses to interrupt a container outside its labelled project" >&2
    exit 1
fi

docker compose --project-name "$project_name" --file "$compose_file" \
    stop --timeout 20 "$initial_primary" >/dev/null

post_failover_primary="$(wait_for_new_primary "$initial_primary")"
wait_for_qualification

if [[ ! -s "$evidence_path" ]]; then
    echo "DomoAI lab qualification produced no evidence" >&2
    exit 1
fi
printf '{"initial_primary":"%s","post_failover_primary":"%s"}\n' \
    "$initial_primary" "$post_failover_primary"
cat "$evidence_path"
failed=0
