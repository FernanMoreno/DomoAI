# Auditoría de despliegue, CI y operaciones

## OPS-001 — Gateway ejecutado como root y contenedor sin hardening

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`deploy/Dockerfile:10-18`](../../deploy/Dockerfile#L10), [`deploy/compose.yaml:49-89`](../../deploy/compose.yaml#L49), [`test_gateway_container_hardening.py`](../../tests/security/test_gateway_container_hardening.py), [`specs/222-harden-gateway-container/composition-review.md`](../../specs/222-harden-gateway-container/composition-review.md)

La imagen crea el grupo/usuario dedicado `domoai` con UID/GID `10001`, prepara
`/app/data` con ownership compatible y termina con `USER domoai:domoai`. El
servicio gateway fija además `user: "10001:10001"`, `read_only: true`,
`cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` y un `/tmp` tmpfs
acotado a 64 MiB con `noexec,nosuid`. `/app/data` sigue siendo el único
volumen persistente escribible; los mounts de secretos y configuración siguen
siendo `:ro`.

### Impacto

Una vulnerabilidad de aplicación que permita ejecución de código tendría más capacidad para modificar datos, credenciales montadas o el entorno del proceso.

### Verificación de cierre

- RED del contrato: `2 failed` contra la configuración anterior.
- GREEN: contrato de hardening, assets de despliegue y preflight: `20 passed`.
- `docker compose -f deploy/compose.yaml config`: PASS.
- Build: `docker build -t domoai-ops001-test -f deploy/Dockerfile .`: PASS.
- Runtime de imagen: `uid=10001(domoai) gid=10001(domoai)`; bajo
  `--read-only`, `/app/data` y `/tmp` son escribibles y `/app` no lo es.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 245 source
  files`; Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `656 passed, 18 skipped`;
  arquitectura `4 kept, 0 broken`; PASS.

## OPS-002 — Dependencias e imágenes no totalmente reproducibles

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`deploy/Dockerfile:1-26`](../../deploy/Dockerfile#L1), [`deploy/compose.yaml:5`](../../deploy/compose.yaml#L5), [`deploy/compose.yaml:29`](../../deploy/compose.yaml#L29), [`test_reproducible_deployment.py`](../../tests/security/test_reproducible_deployment.py), [`specs/223-reproducible-deployment-inputs/composition-review.md`](../../specs/223-reproducible-deployment-inputs/composition-review.md)

El Dockerfile fija las imágenes de Python 3.12 y uv por digest de índice,
exporta `uv.lock` con `uv export --locked --no-dev --no-emit-project` e
instala sus hashes mediante `uv pip install --system --require-hashes`; el
proyecto se instala después con `--no-deps`. Compose fija por digest de índice
Mosquitto, Home Assistant y Caddy.

### Impacto

Un rebuild futuro puede introducir cambios de dependencias o imágenes sin que cambie el código fuente.

### Verificación de cierre

- RED del contrato: `2 failed` contra tags mutables y `pip install .`.
- GREEN: contratos de reproducibilidad y assets de despliegue: `20 passed`.
- `uv lock --check`: PASS.
- `docker compose -f deploy/compose.yaml config`: PASS.
- Build limpio: `docker build --no-cache -t domoai-ops002-test -f
  deploy/Dockerfile .`: PASS; el log confirmó 46 paquetes exactos desde la
  exportación bloqueada y `domoai==0.1.0` sin dependencias adicionales.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 245 source
  files`; Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `656 passed, 18 skipped`;
  arquitectura `4 kept, 0 broken`; PASS.

## OPS-003 — Sin límites de recursos del stack

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`deploy/compose.yaml:5-19`](../../deploy/compose.yaml#L5), [`deploy/compose.yaml:27-43`](../../deploy/compose.yaml#L27), [`deploy/compose.yaml:70-86`](../../deploy/compose.yaml#L70), [`deploy/compose.yaml:108-124`](../../deploy/compose.yaml#L108), [`test_compose_resource_limits.py`](../../tests/security/test_compose_resource_limits.py), [`specs/224-bound-stack-resources/composition-review.md`](../../specs/224-bound-stack-resources/composition-review.md)

Los cuatro servicios (`mqtt`, `homeassistant`, `gateway`, `proxy`) declaran
límites explícitos de CPU, memoria, PIDs y `ulimits.nofile` soft/hard. Los
healthchecks, restart policies, dependencias y límites de colas/frames de la
aplicación permanecen intactos.

### Verificación de cierre

- RED del contrato: `1 failed` por ausencia de `cpus` en la configuración
  anterior.
- GREEN: contratos de límites, hardening, reproducibilidad, assets y
  preflight: `24 passed`.
- `docker compose -f deploy/compose.yaml config`: PASS.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 245 source
  files`; Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `656 passed, 18 skipped`;
  arquitectura `4 kept, 0 broken`; PASS.
- Graphify code-only post-cambio: `10.675 nodos`, `30.933 aristas`, `703
  comunidades`; la configuración YAML fue verificada directamente y los
  tests de límites quedaron indexados junto a las fronteras de deployment.

El tamaño máximo de volúmenes y las alertas de host siguen siendo
responsabilidad del entorno de despliegue, no una propiedad portable del
Compose versionado.

## OPS-004 — TLS interno como configuración por defecto del proxy

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`Caddyfile:1-19`](../../deploy/reverse-proxy/Caddyfile#L1), [`Caddyfile.internal:1-20`](../../deploy/reverse-proxy/Caddyfile.internal#L1), [`compose.yaml:129-147`](../../deploy/compose.yaml#L129), [`test_production_tls_proxy.py`](../../tests/security/test_production_tls_proxy.py), [`composition-review.md`](../../specs/225-production-tls-proxy/composition-review.md)

La configuración productiva ya no usa `tls internal`: exige el certificado y
la clave del operador en `/etc/caddy/certs/fullchain.pem` y
`/etc/caddy/certs/privkey.pem`, montados en solo lectura desde rutas host
configurables. Los binds HTTP/HTTPS del proxy parten de `127.0.0.1`; exponerlos
externamente requiere overrides explícitos. La configuración con CA interna se
conserva únicamente en `Caddyfile.internal`, que no monta Compose.

**Verificación:** contrato focal `30 passed`; `docker compose config`: PASS;
imagen Caddy fijada con certificado efímero: `Valid configuration`; gate global
`656 passed, 18 skipped`; Ruff, mypy e Import Linter: PASS.

**Riesgo residual:** emisión/renovación, confianza CA, SAN/DNS y firewall son
responsabilidad del operador. El healthcheck conserva `--insecure` para poder
probar CAs privadas dentro del contenedor, sin cambiar la verificación de los
clientes externos.

## OPS-005 — CI permite composición degradada y no exige cobertura mínima

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`ci.yml:50-68`](../../.github/workflows/ci.yml#L50), [`ci.yml:124-163`](../../.github/workflows/ci.yml#L124), [`ci.yml:239-298`](../../.github/workflows/ci.yml#L239), [`run_mandatory_docker_composition.sh`](../../scripts/ci/run_mandatory_docker_composition.sh), [`test_ci_gates.py`](../../tests/contract/test_ci_gates.py), [`composition-review.md`](../../specs/226-mandatory-ci-gates/composition-review.md)

`test-unit` exige ahora `--cov-fail-under=60` (línea base medida: `63%`). Se
añadió `test-docker-composition`, que falla si `docker info` no puede conectar,
ejecuta Mosquitto real con Testcontainers y rechaza cualquier skip mediante
JUnit. El mismo job renderiza Compose y valida el Caddy productivo con la
imagen fijada y certificado efímero. Está incluido en `required-release-gates`
y `evidence-summary`; la suite amplia conserva únicamente skips de
infraestructura de laboratorio explícitamente no obligatorios.

**Verificación:** contrato focal `27 passed`; helper Docker `2 passed, 0
skipped`; Compose PASS; Caddy `Valid configuration`; gate global `656 passed,
18 skipped`; Ruff, mypy e Import Linter PASS.

**Riesgo residual:** el umbral 60% es un suelo inicial global, no un umbral
por módulo crítico; runners, pulls de imágenes y cualificaciones externas
siguen siendo dependencias operativas. `actionlint` no estaba instalado localmente;
el YAML se parseó y el contrato del workflow pasó.

## OPS-006 — Laboratorio MQTT inseguro si se expone fuera de localhost

**Severidad:** media contextual
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`dev/lab/compose.yaml`](../../dev/lab/compose.yaml), [`mosquitto.conf`](../../dev/lab/mqtt/mosquitto.conf), [`compose.shared-mqtt-tls.yaml`](../../dev/lab/compose.shared-mqtt-tls.yaml), [`mosquitto.shared.conf`](../../dev/lab/mqtt/mosquitto.shared.conf), [`test_secure_mqtt_lab.py`](../../tests/security/test_secure_mqtt_lab.py), [`test_lab_mqtt_assets.py`](../../tests/contract/test_lab_mqtt_assets.py), [`test_lab_mqtt_composition.py`](../../tests/composition/test_lab_mqtt_composition.py)

La composición base publica MQTT plaintext únicamente en
`127.0.0.1:${DOMOAI_MQTT_HOST_PORT:-1883}`, exige usuario y contraseña no
vacíos y genera el `password_file` dentro del contenedor en un volumen de
credenciales separado del estado. Mosquitto usa `allow_anonymous false` y el
healthcheck también autentica; Zigbee2MQTT y los cuatro emuladores Paho
reciben credenciales obligatorias antes de conectar.

Para redes compartidas, `compose.shared-mqtt-tls.yaml` es el único camino de
exposición: conserva el listener plaintext solo en loopback y añade únicamente
el listener TLS 8883 en el bind explícito `DOMOAI_MQTT_TLS_BIND`. El overlay
exige certificado y clave, valida que sean ficheros legibles antes de arrancar,
los copia a un volumen efímero con permisos del usuario Mosquitto y el broker
mantiene autenticación obligatoria en ambos listeners. No existe un override
de bind plaintext remoto.

### Verificación de cierre

- RED focal confirmado contra el broker wildcard/anónimo anterior.
- GREEN: `12 passed` en los contratos de seguridad/assets y composiciones
  reales del broker base y del overlay TLS.
- La prueba base publica con credenciales y rechaza publicación anónima.
- La prueba TLS publica con certificado verificado y credenciales; el overlay
  no contiene un bind remoto de 1883.
- El `.env.example` no contiene secretos y documenta las variables TLS
  obligatorias. Suite global: `2412 passed, 18 skipped`; gate de composición:
  `660 passed, 18 skipped`; arquitectura `4 kept, 0 broken`.
- La instancia antigua del broker wildcard que estaba activa en Docker fue
  detenida sin eliminar volúmenes; debe recrearse con `dev/lab/.env` antes de
  volver a usar el laboratorio.
