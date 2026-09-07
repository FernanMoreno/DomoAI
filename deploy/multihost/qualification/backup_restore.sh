#!/usr/bin/env bash
# NOT FOR PRODUCTION: bounded PostgreSQL backup/restore drill for the lab.
set -euo pipefail

network=""
dump_path=""
restore_container=""

usage() {
    echo "usage: $0 --network NAME --dump-path FILE --restore-container NAME" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --network)
            network=${2:?network value required}
            shift 2
            ;;
        --dump-path)
            dump_path=${2:?dump path required}
            shift 2
            ;;
        --restore-container)
            restore_container=${2:?restore container required}
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

[[ -n "$network" && -n "$dump_path" && -n "$restore_container" ]] || usage
[[ "$restore_container" =~ ^domoaiqv2[[:alnum:]_-]+-restore$ ]] || {
    echo "restore container must be a project-scoped lab name" >&2
    exit 2
}

epoch_ms() {
    date +%s%N | cut -c1-13
}

cleanup() {
    docker rm --force --volumes "$restore_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

dump_started=$(epoch_ms)
docker run --rm --network "$network" --env PGPASSWORD=lab-only-postgres-password \
    postgres:16.4-bookworm pg_dump --no-owner --no-privileges \
    -h postgres-writer -U postgres postgres >"$dump_path"
restore_started=$(epoch_ms)

docker run --detach --name "$restore_container" \
    --label com.domoai.lab=true --label com.domoai.not-for-production=true \
    --network "$network" --env POSTGRES_PASSWORD=restore \
    postgres:16.4-bookworm >/dev/null

deadline=$(( $(date +%s) + 120 ))
while ! docker exec "$restore_container" pg_isready -U postgres >/dev/null 2>&1; do
    (( $(date +%s) < deadline )) || {
        echo "restore database did not become ready before deadline" >&2
        exit 1
    }
    sleep 2
done

docker exec --interactive "$restore_container" \
    psql -U postgres -v ON_ERROR_STOP=1 postgres <"$dump_path" >/dev/null

physical_count=$(docker exec "$restore_container" psql -U postgres -Atqc \
    "SELECT COUNT(*) FROM physical_intents WHERE idempotency_key='backup-sentinel'" postgres)
outbox_count=$(docker exec "$restore_container" psql -U postgres -Atqc \
    "SELECT COUNT(*) FROM audit_outbox" postgres)
metric_count=$(docker exec "$restore_container" psql -U postgres -Atqc \
    "SELECT COUNT(*) FROM operational_metric_history WHERE metric_name='lab_backup_sentinel_total'" postgres)

[[ "$physical_count" == 1 && "$outbox_count" -ge 1 && "$metric_count" == 1 ]] || {
    printf 'restore sentinels missing: physical=%s outbox=%s metrics=%s\n' \
        "$physical_count" "$outbox_count" "$metric_count" >&2
    exit 1
}

restore_finished=$(epoch_ms)
printf '{"dump_duration_ms":%s,"metric_count":%s,"outbox_count":%s,"physical_count":%s,"restore_duration_ms":%s,"sentinels_preserved":true}\n' \
    "$((restore_started - dump_started))" "$metric_count" "$outbox_count" \
    "$physical_count" "$((restore_finished - restore_started))"
