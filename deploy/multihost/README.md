# DomoAI multi-host active-passive

Este directorio documenta la integración real aprobada para la fase 3. No es
un Compose de producción y no arranca servicios automáticamente.

## Topología obligatoria

- etcd v3 en un quorum de 3 o 5 miembros, en dominios de fallo separados.
- PostgreSQL HA con un endpoint de escritura único, backups verificados y
  durabilidad síncrona cuando el RPO sea cero.
- Cada host DomoAI usa mTLS para etcd y PostgreSQL.
- El gateway físico debe transportar `tenant_id`, `household_id`,
  `deployment_id` y `fencing_epoch`, y rechazar cualquier epoch menor o igual
  al último aplicado para ese scope.
- El primer modo permitido es active-passive: un único owner físico por hogar.

## Provisionado

1. Provisiona el quorum etcd y verifica que los miembros minoritarios no
   aceptan escrituras durante una partición.
2. Provisiona PostgreSQL HA y ejecuta la migración explícita:

   ```bash
   domoai-admin migrate-postgres \
     --source-database data/domoai.sqlite3 \
     --dsn 'postgresql://domoai@postgres-primary.internal:5432/domoai' \
     --sslmode verify-full \
     --sslrootcert /run/secrets/postgres-ca.pem \
     --sslcert /run/secrets/postgres-client.pem \
     --sslkey /run/secrets/postgres-client.key
   ```

   El comando exige un destino vacío, copia las tablas del control plane en
   una transacción y devuelve conteos y hashes de verificación.
3. Configura las variables multi-host de `deploy/gateway.env.example` con
   certificados de solo lectura apropiados y un `DOMOAI_INSTANCE_ID` único.
4. Ejecuta la cualificación atendida desde un host que tenga las variables
   `DOMOAI_MULTI_HOST_*`, `DOMOAI_ETCD_*` y `DOMOAI_POSTGRES_*` configuradas.
   El bridge es un ejecutable local propiedad del operador; recibe y devuelve
   exactamente una línea JSON por probe y no se invoca mediante un shell:

   ```bash
   domoai-admin qualify-multihost \
     --confirm-physical-fencing \
     --gateway-command /opt/domoai/bin/gateway-fencing-probe --profile edge-a \
     --safe-command operator-approved-safe-noop \
     --output /run/secrets/multihost-qualification.json
   ```

   El comando consulta (sin mutar) el estado de todos los miembros etcd y de
   PostgreSQL. Solo crea leases efímeros y ejecuta la orden física inocua que
   el operador indicó. Exige comprobar: quorum de 3/5, renovación, takeover,
   primary PostgreSQL con al menos una réplica síncrona, aceptación del epoch
   actual y rechazo de epoch stale y replay en el último gateway.
5. Conserva el JSON generado como secreto operativo: contiene alcance,
   identidad de gateway, caducidad y digest, pero nunca DSN, credenciales o
   lease IDs. Configura su ruta e identidad exacta en:

   ```dotenv
   DOMOAI_MULTI_HOST_PRODUCTION_ENABLED=true
   DOMOAI_MULTI_HOST_QUALIFICATION_EVIDENCE_PATH=/run/secrets/multihost-qualification.json
   DOMOAI_MULTI_HOST_GATEWAY_IDENTITY=gateway-serial-or-attested-id
   ```

   El arranque con coordinador externo falla antes de abrir el control plane
   si la evidencia falta, expiró, no pasa todos los checks, su digest no
   coincide o su scope/gateway no coincide. Solo tras ello puede habilitarse
   active-passive en producción.

## Contrato del bridge físico

El proceso definido por `--gateway-command` lee un objeto JSONL v1 y emite un
objeto JSONL v1. La evidencia persistida liga el campo `gateway_identity` al
gateway físico que responde. El bridge debe aplicar la validación en el último
hop físico, por scope:

```json
{"schema_version":"v1","probe_id":"uuid","scope":{"tenant_id":"tenant","household_id":"home","deployment_id":"edge"},"fencing_epoch":7,"lease_id":"non-secret-lease-id","safe_command":"operator-approved-safe-noop"}
```

La respuesta debe repetir `probe_id`, indicar `accepted`, y devolver
`observed_epoch`. Tras aceptar 7, los epochs 6 y 7 deben devolverse con
`accepted: false`. Una respuesta no JSON, con otro `probe_id`, o fuera de este
contrato invalida la cualificación. El bridge no debe imprimir secretos.

## Active-active

Está deshabilitado por diseño. Ni etcd ni PostgreSQL sustituyen la validación
del epoch en el último punto físico. No se debe activar con un balanceador,
un lock Redis ni una segunda réplica sin qualification report y evidencia HIL.

La implementación usa `EtcdHttpLeaseCoordinator` sobre la API v3 JSON de etcd
y `PostgresDatabase` para el control plane compartido. El modo normal de
single-writer sigue usando SQLite y no necesita estos servicios.

## Laboratorio Docker no productivo

El ejercicio reproducible de infraestructura se ejecuta con:

```bash
./scripts/run_multihost_qualification_lab.sh
```

Construye un cliente efímero local, arranca un proyecto Compose aislado con
etcd ×3, Patroni/PostgreSQL ×3 y HAProxy, y escribe evidencia con procedencia
`lab`. No publica puertos al host ni añade autoridad al runtime productivo.
Antes del corte controlado espera que los dos standbys respondan como réplicas
estables; después detiene únicamente el primary etiquetado del proyecto y
comprueba una promoción distinta mediante Patroni y HAProxy. El script tiene
deadline, logs acotados y cleanup de sus propios contenedores y volúmenes.

El resultado Docker no acredita mTLS, dominios de fallo independientes,
backups/RPO, gateway físico ni commissioning HIL. La evidencia `lab` es
rechazada incondicionalmente por el gate de producción; active-active sigue
deshabilitado.

## Laboratorio Docker v2 — matriz completa no productiva

La matriz ampliada se ejecuta de forma aislada con:

```bash
./scripts/run_multihost_lab_v2.sh
```

El script construye un runner efímero, espera un writer estable antes de
arrancar los hosts y elimina sus contenedores, red, volúmenes y certificados
temporales al terminar. Las ocho comprobaciones son:

- carrera de ownership entre dos hosts, con 20 iteraciones;
- partición y takeover con rechazo del epoch stale;
- crash después del claim, replay e idempotencia de outbox;
- pérdida de un miembro etcd y recuperación;
- failover del primary PostgreSQL con writer HAProxy operativo;
- generación/rotación local mTLS y rechazo de cliente no confiable/expirado;
- backup/restore PostgreSQL con sentinelas de intents, outbox y métricas;
- colas bounded por hogar y límite de históricos de métricas.

La ejecución produce JSONL con `qualification_environment=lab` y nunca
contiene DSN, credenciales, lease IDs ni tokens. El resultado sólo cualifica
el comportamiento reproducible del laboratorio: no habilita producción ni
sustituye HIL, PKI real, dominios de fallo independientes, RPO/RTO, alertas
operativas o active-active.
