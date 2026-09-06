# Auditoría Fase 3 — Escalabilidad e identidad

**Objetivo:** pasar de un runtime avanzado para una vivienda a una plataforma operable con varias casas, usuarios, agentes, providers y despliegues sin perder una única autoridad física verificable.

## 1. A-013 — Autenticación y tokens

### Evidencia

[`auth.py`](../src/domoai/mcp/auth.py:43) comprueba unicidad de `client_id`, pero no impide hashes de token duplicados. Dos identidades pueden presentar el mismo bearer y la primera entrada puede ganar de forma no explícita.

`expires_at` permite datetimes sin timezone, cuyo `.timestamp()` depende del entorno.

### Recomendación

- exigir `expires_at` aware;
- hacer únicos tanto `client_id` como `token_hash`;
- rotación y revocación durables;
- reload seguro del token file;
- permisos estrictos del fichero;
- rate limit y métrica de intentos fallidos;
- no usar el token de agente como sustituto de consentimiento humano.

Las protecciones existentes de no-loopback, HTTPS, scope `mutate` y separación de liveness/readiness deben conservarse.

## 2. A-018 — Ownership como capacidad, no booleano

### Evidencia

`aggregate_owner: bool` habilita una ruta interna de ejecución de miembros en [`execution_admission.py`](../src/domoai/application/execution_admission.py:88). No es una vulnerabilidad de red directa porque MCP no expone el parámetro, pero cualquier caller interno con acceso al executor puede pasar `True`.

### Recomendación

Sustituir el booleano por una capability opaca emitida por el aggregate:

```text
AggregateExecutionCapability {
  bundle_id,
  member_id,
  bundle_digest,
  nonce,
  expires_at
}
```

La admission debe verificar que esa capability procede del bundle correcto y no puede ser fabricada por un caller normal.

## 3. Modelo de identidad multi-home

El runtime actual está diseñado para una instancia compartida y una vivienda. Para crecer, el scope debe ser explícito en todas las entidades durables:

```text
tenant_id / household_id / area_id / device_id / capability
```

Cada Plan, ApprovalGrant, BundleCommit, Schedule, StateSnapshot, auditoría y secreto debe llevar `household_id` y principal propietario cuando aplique.

### Roles mínimos

- `viewer`: lectura de estado permitido;
- `planner`: puede preparar propuestas;
- `operator`: puede solicitar aprobación según policy;
- `owner`: puede gestionar adapters, policies y usuarios;
- `service`: identidad limitada para automatizaciones locales.

Las ACL deben poder limitar por vivienda, área, tipo de dispositivo, capability y operación. `mutate` por sí solo es demasiado amplio para una plataforma multi-home.

## 4. Persistencia y despliegue

SQLite es una decisión adecuada para una vivienda y un runtime único. No se debe introducir clustering activo-activo sin resolver antes:

- ownership distribuido;
- fencing token;
- orden global de eventos;
- idempotencia entre replicas;
- lease distribuido;
- migraciones coordinadas;
- outbox y auditoría compartida.

Para el alcance actual conviene mantener una sola autoridad por hogar y hacer explícitos los límites operativos en deployment. La documentación actual ya indica que active-active no está soportado; esa limitación debe permanecer fail-closed en configuración.

La evaluación detallada de ownership distribuido, pools de workers y métricas
remotas está en [`evaluacion-coordinacion-distribuida-pools-metricas.md`](evaluacion-coordinacion-distribuida-pools-metricas.md).

## 5. Gestión de secretos

La configuración de laboratorio usa token files y variables de entorno, lo cual es razonable para desarrollo. En producción debe añadirse:

- gestor de secretos externo o keyring local;
- rotación sin reiniciar toda la autoridad;
- separación entre credenciales de lectura y escritura;
- cifrado de backups;
- redacción de tokens en logs, traces y resources;
- prueba de restauración sin reactivar grants expirados.

## 6. Federación y acceso remoto

La federación entre hogares no debe compartir directamente adapters ni SQLite. Debe operar mediante una API de control con:

- identidad del hogar destino;
- intención firmada o autenticada;
- expiración;
- idempotency key;
- policy local del hogar;
- respuesta explícita de `accepted`, `rejected`, `unknown` o `completed`.

El hogar remoto nunca debe aceptar una decisión de otro hogar como aprobación local implícita.

## 7. Compatibilidad y evolución de contratos

El versionado `v1` y `extra="forbid"` son buenos fundamentos. Para escalar deben completarse con:

- compatibilidad hacia atrás de resources MCP;
- migraciones de planes y schedules;
- deprecación anunciada de tools;
- digest de definición para evitar ejecutar templates antiguos;
- política de lectura para campos desconocidos;
- pruebas de downgrade/restore.

## 8. Criterio de salida de la fase

La fase termina cuando cada entidad de autoridad tiene un principal y un hogar, los tokens son únicos, fechados y revocables, los callers internos no pueden forjar ownership, los backups son cifrados y probados, y un segundo hogar no puede modificar el primero sin pasar por su policy local.

## 9. Ejecución de la fase — cierre técnico

La implementación aprobada se realizó como una ampliación local, compatible y
reversible. Los hallazgos iniciales de esta auditoría quedan resueltos así:

| Hallazgo | Corrección aplicada | Evidencia |
|---|---|---|
| A-013 — tokens | `ClientTokenDocument` rechaza `client_id` y `token_hash` duplicados; los timestamps son aware; `TokenFileManager` rota/revoca con escritura atómica, hash-only y permisos `0600`; el verifier recarga sin sustituir el conjunto anterior si el documento es inválido. | [`auth.py`](../src/domoai/mcp/auth.py), [`token_lifecycle.py`](../src/domoai/mcp/token_lifecycle.py), [`test_token_lifecycle.py`](../tests/unit/mcp/test_token_lifecycle.py) |
| A-018 — ownership booleano | Se retiró `aggregate_owner` de la cadena productiva. La ejecución de miembros exige `AggregateExecutionCapability` server-issued, ligada a bundle/digest, expirable y one-shot, incluyendo autoridad tenant/hogar. | [`execution_admission.py`](../src/domoai/application/execution_admission.py), [`bundle_commit.py`](../src/domoai/application/bundle_commit.py), [`test_execution_admission.py`](../tests/unit/application/test_execution_admission.py) |
| Identidad multi-home | `AuthorityContext` y `PrincipalRole` viajan en planes, grants, bundles, schedules, snapshots, automatizaciones y auditoría. `AuthorityPolicy` vincula el plan a los claims verificados y aplica tenant, hogar, área, dispositivo, capability, rol y operación. | [`models.py`](../src/domoai/domain/models.py), [`authority.py`](../src/domoai/application/authority.py), [`domotics_server.py`](../src/domoai/mcp/domotics_server.py) |
| Persistencia compatible | La migración 014 añade proyecciones e índices aditivos; filas v1 sin `authority` cargan como el hogar local por defecto y las nuevas escrituras persisten el contexto completo. | [`014_authority_identity.sql`](../src/domoai/persistence/migrations/014_authority_identity.sql), [`test_phase3_identity_persistence.py`](../tests/integration/test_phase3_identity_persistence.py) |
| Backups | El formato v2 cifra cada miembro SQLite con AES-256-GCM, mantiene el lock de ownership y descifra/verifica en staging antes de reemplazar el destino. Backups v1 sin cifrar siguen siendo legibles de forma explícita. | [`backup.py`](../src/domoai/persistence/backup.py), [`test_backup_restore_lifecycle.py`](../tests/integration/test_backup_restore_lifecycle.py), [`test_backup_service.py`](../tests/unit/persistence/test_backup_service.py) |
| Federación | `FederationIntent` firma HMAC, expira, direcciona tenant/hogar y usa idempotency key. `FederationBoundary` solo registra propuestas aceptadas; no llama adapters ni convierte aceptación remota en aprobación local. | [`federation.py`](../src/domoai/domain/federation.py), [`federation.py`](../src/domoai/application/federation.py), [`test_federation.py`](../tests/unit/application/test_federation.py) |

La auditoría de identidad y el contrato MCP también cubren filtrado de
listados/resources, persistencia en las lanes operativa y de auditoría y la
ausencia del bearer en claims serializados. Los artefactos ejecutables de la
fase están en [`spec.md`](../specs/185-phase3-escalabilidad-identidad/spec.md),
[`plan.md`](../specs/185-phase3-escalabilidad-identidad/plan.md) y
[`tasks.md`](../specs/185-phase3-escalabilidad-identidad/tasks.md).

## 10. Alcance deliberadamente diferido

Coordinación active-active, leases/fencing distribuidos, pools de workers
entre hosts y exportación remota de métricas no forman parte del cierre de
esta fase. El runtime conserva un único writer/owner por deployment, lanes
bounded process-local y métricas MCP locales. La evaluación concluye que no
hay necesidad medida ni protocolo de fencing para abrir ese cambio; cualquier
activación futura requiere una especificación y qualification separadas:
[`evaluacion-coordinacion-distribuida-pools-metricas.md`](evaluacion-coordinacion-distribuida-pools-metricas.md).

## 11. Criterio de salida actualizado

La Fase 3 queda técnicamente cerrada cuando pasan las pruebas enfocadas,
contratos, integración, composición, suite completa, lint/type/architecture
checks, exportación de schemas, `project-composition-check` y revisión de
composición del sistema. La salida no implica que se hayan habilitado
active-active, pools distribuidos ni métricas remotas.

### Evidencia de cierre — 2026-09-04

- `uv run pytest -q`: **1707 passed, 18 skipped**.
- `project-composition-check "$(cat .ai/project-name)"`: **483 passed, 18 skipped**; 4 contratos arquitectónicos conservados, 0 rotos.
- `uv run mypy`: **Success**, 151 archivos fuente analizados.
- `uv run ruff check src tests`: **All checks passed**.
- `uv run python scripts/export_schemas.py`: 64 schemas v1 exportados, incluido `authority-context`.
- `uv run python scripts/check_runtime_contract_docs.py`: documentación coherente.
- `graphify . --update --no-viz --code-only`: 7006 nodos, 22366 aristas y 443 comunidades. Permanece el aviso no bloqueante de `tree_sitter_sql` ausente para 13 migraciones SQL.
- `git diff --check`: correcto.

La revisión de composición verificó explícitamente la propagación de
identidad MCP → policy → plan/grant → persistencia/auditoría; el fallback de
migraciones parciales; la rotación/revocación tras reload; el staging y
rollback de backups; y que la federación no tenga una ruta hacia ningún
adapter. Veredicto: **PASS WITH RISKS**, por el alcance deliberadamente
single-writer y la ausencia de dependencia SQL de Graphify, no por un fallo de
los contratos implementados.

## 12. Composition Review Report

- **Subsistemas cambiados:** dominio, policy/aplicación, MCP/auth, runtime de
  aprobación y auditoría, persistencia SQLite/backup, configuración, CLI,
  schemas y documentación.
- **Vecinos revisados:** `RuntimeComposition`, `PlanRepository`,
  `ApprovalStore`, `Scheduler`, `PlanExecutor`, `AuditLog`,
  `AuditEventRepository`, `SQLiteDatabase`, `FastMCP` y el boundary de
  adapters.
- **Contratos e invariantes:** identidad verificada no elevable, target local
  de tenant/hogar, ACL por rol/operación/área/dispositivo/capability, capability
  aggregate one-shot, lock de restore, AES-GCM antes de reemplazo,
  idempotencia de propuestas y no-actuación de federación.
- **Escenarios:** éxito, replay/duplicado, firma inválida, expiración, cambio
  de digest, revocación tras reload, clave errónea, manipulación, restore con
  ownership activo, migración parcial, fallo de sink y límites de auditoría.
- **Checks:** Import Linter, contracts, pytest completo, composición del
  proyecto, Ruff, mypy, exportación de schemas, documentación y `git diff`.
- **Dependencias reales:** SQLite disposable en integración y runtime fixture;
  no se habilitó ningún transporte federado ni dependencia remota que no
  estuviera disponible.
- **Fallos encontrados y corregidos:** digest de recurrencia local, fallback
  de migración con comentarios SQL y tamaño bounded de eventos con identidad.
- **Riesgos residuales:** single-writer/lock local, pools process-local,
  métricas locales y parser SQL opcional ausente en Graphify; quedan
  deliberadamente fuera del alcance de esta fase.
- **Veredicto:** **PASS WITH RISKS**; Fase 3 local y compatible cerrada.

La verificación posterior de provisión y carga está consolidada en
[`evidence/production-readiness-latest.md`](evidence/production-readiness-latest.md):
la rotación/revocación de tokens y los clientes HTTP autenticados pasan; el
preflight del checkout falla correctamente porque faltan los artefactos de
secretos productivos. La medición no justifica habilitar active-active, leases
distribuidos, pools entre hosts ni métricas remotas; permanecen bloqueados
hasta aportar infraestructura y una especificación de fencing independiente.

## 13. Addendum — cierre requerido para multi-host — 2026-09-05

### 13.1 Estado actual

Fase 3 resuelve identidad multi-tenant/multi-home, ACL por alcance, tokens,
backups cifrados y una frontera de federación que no llama adapters. La
decisión de ownership, sin embargo, continúa siendo un único writer por
deployment: lock local, fila SQLite durable, workers process-local y métricas
locales.

Eso es seguro para una casa en un host. No es suficiente para ofrecer
disponibilidad multi-host ni active-active.

### 13.2 Componentes que faltan

1. **Coordinador de ownership:** servicio externo con lease por
   `tenant_id/household_id/deployment_id`, expiración y renovación.
2. **Fencing:** epoch monotónico emitido por el coordinador; cada comando,
   bundle y operación latched debe transportar el epoch y ser rechazado si es
   antiguo.
3. **Estado compartido:** base de datos durable adecuada para varias réplicas,
   con planes, grants, schedules, outcomes, audit outbox y ledger de
   idempotencia compartidos.
4. **Cola y pools:** cola durable particionada por hogar, workers sin acceso
   directo al adapter y lane de autoridad separada de solver, auditoría y
   telemetría.
5. **Failover:** procedimiento activo-pasivo o active-active definido,
   takeover, recuperación de owner incierto y parada de actuadores latched.
6. **Observabilidad:** identidad de instancia, históricos, agregación,
   métricas de leases/fencing/takeover y alertas multi-réplica.
7. **Seguridad de despliegue:** mTLS o TLS validado, service discovery,
   rotación de credenciales, network policy y aislamiento por hogar.

### 13.3 Pruebas obligatorias antes de activar réplicas

- carrera de dos hosts adquiriendo el mismo hogar;
- expiración y renovación del lease;
- partición de red con sockets de ambos hosts abiertos;
- takeover mientras una orden está en vuelo;
- rechazo de un fencing token anterior;
- replay de la misma `idempotency_key` desde hosts distintos;
- crash después de write, antes de outcome y durante outbox;
- recuperación sin duplicar comandos ni consumir dos veces una aprobación;
- migración/restore sin perder planes, grants, audit outbox u outcomes;
- collector remoto caído, réplica reiniciada y agregación correcta.

### 13.4 Decisión de arquitectura

No se debe activar active-active añadiendo únicamente un balanceador o
sustituyendo `fcntl` por un lock Redis. Sin fencing, ambos hosts podrían
conservar sockets y actuar durante una partición. La primera versión segura
puede ser active-passive con un único owner físico por hogar; active-active
requiere además que el backend o gateway físico valide epochs antiguos.

El diseño detallado está en
[`evaluacion-coordinacion-distribuida-pools-metricas.md`](evaluacion-coordinacion-distribuida-pools-metricas.md)
y la base provider-neutral está especificada en
[`specs/191-phase3-multihost-foundation/spec.md`](../specs/191-phase3-multihost-foundation/spec.md).

## 14. Addendum técnico — fundación provider-neutral — 2026-09-05

El addendum se implementó como una base segura y reversible, sin seleccionar
Redis, etcd, Postgres ni otro coordinador remoto:

- `LeaseScope` y `FencingToken` identifican tenant, hogar, deployment, owner y
  epoch monotónico; `DeterministicLeaseCoordinator` solo se usa en tests.
- `FencingGuard` se comprueba en admission y justo antes del adapter. Epoch
  antiguo, lease perdido, expiry o scope incorrecto bloquean la escritura.
- `physical_intents` usa `(household_id, idempotency_key)` como clave única,
  soporta `prepared`, `acknowledged`, `confirmed`, `rejected` y `unknown`, y
  recupera intenciones en vuelo como `UNKNOWN`.
- `HouseholdWorkQueues` mantiene FIFO y backpressure independiente por hogar;
  la lane física no comparte capacidad ilimitada con solver o telemetría.
- `InstanceIdentity`, contadores de fencing y `operational_metric_history`
  permiten correlacionar takeover y reinicios con límites bounded. El renderer
  Prometheus solo expone campos allowlisted y no secretos.
- `DOMOAI_MULTI_HOST_ENABLED=true` sin un `LeaseCoordinator` externo inyectado
  rechaza el arranque. El runtime normal continúa single-writer.

La base de software queda cerrada con riesgo explícito: no se ha afirmado que
un coordinador determinista sea una prueba de producción multi-host. Faltan la
dependencia externa cualificada, mTLS/service discovery y HIL del gateway que
debe validar epochs en el último punto físico.

## 15. Addendum técnico — integración etcd/PostgreSQL — 2026-09-05

La integración real aprobada queda implementada sin activar el modo por
defecto:

- `EtcdHttpLeaseCoordinator` usa la API v3 JSON de etcd con leases, CAS de
  epoch, rotación de endpoints y mTLS mediante `httpx`; no depende de un
  cliente Python etcd sin mantenimiento oficial.
- `PostgresDatabase` conserva la superficie de los repositorios JSON, crea el
  schema completo del control plane y comparte planes, grants, outcomes,
  audit outbox, intents e históricos entre hosts.
- `migrate-postgres` realiza la transición SQLite→PostgreSQL solo de forma
  explícita, sobre destino vacío, con rollback, conteos y hashes.
- El runtime multi-host exige PostgreSQL, etcd, certificados y un adapter que
  anuncie capacidad de fencing; un adapter no cualificado se rechaza antes de
  conectar o escribir.

La cualificación externa ya tiene un gate ejecutable: `qualify-multihost`
verifica quorum etcd 3/5, renovación/takeover, primary PostgreSQL con réplica
síncrona y aceptación/rechazo de epochs en un bridge JSONL del último hop
físico. El JSON resultante se firma por digest, expira, se liga a scope e
identidad de gateway y `DOMOAI_MULTI_HOST_PRODUCTION_ENABLED=true` rechaza el
arranque externo antes de abrir el control plane si esa evidencia no es válida.
La ejecución requiere confirmación explícita de la prueba física inocua; no
habilita active-active, que continúa bloqueado.

## 16. Addendum técnico — laboratorio Docker y failover — 2026-09-06

El laboratorio Docker ya valida la parte reproducible de infraestructura con
etcd de tres miembros, Patroni/PostgreSQL de tres nodos, réplica síncrona,
HAProxy y fencing JSONL. La cualificación se ejecuta en un cliente efímero
unido a la red privada de Compose, sin puertos host, y produce únicamente
evidencia `qualification_environment=lab`.

El ejercicio de failover detiene sólo el primary que Patroni identifica y
comprueba que otro nodo responde `/primary` y que HAProxy vuelve a tener un
writer. Durante la primera ejecución se detectó y corrigió una carrera: la
prueba podía cortar mientras los standbys aún estaban en `creating replica`.
Ahora exige que ambos respondan `/replica` antes del corte. La prueba real
final pasa con `2 passed`.

Este resultado reduce el riesgo de coordinación/HA, pero no sustituye el HIL
del gateway físico, PKI/mTLS, fault domains independientes, backups/RPO ni la
cualificación productiva externa.

## 17. Addendum técnico — matriz completa del laboratorio v2 — 2026-09-06

Se completó la cobertura reproducible que sí puede ejecutarse en Docker, sin
activar el runtime multi-host productivo. El comando es:

```bash
./scripts/run_multihost_lab_v2.sh
```

La ejecución final completó **8/8 escenarios** y **20/20 carreras de
ownership**, con `qualification_environment=lab` y cleanup confirmado:

| Escenario | Evidencia observada |
| --- | --- |
| ownership-race | `max_owner_count=1`, `unauthorized_writes=0` |
| partition-takeover | epoch stale rechazado, `unauthorized_writes=0` |
| crash-replay | `duplicate_accepted_commands=0`, un delivery de outbox, recovery a `unknown` |
| control-plane-loss | pérdida de un miembro etcd y recuperación posterior |
| database-primary-failover | `postgres-2` → `postgres-3`, writer HAProxy writable |
| secure-rotation | cliente válido/rotado aceptado; no confiable y expirado rechazados |
| backup-restore | sentinelas de intents, outbox y métricas preservadas; dump 825 ms, restore 3629 ms |
| bounded-load | profundidad máxima 8, 16 rechazos y histórico limitado |

Durante esta ampliación se corrigió una carrera de arranque del propio lab:
los hosts esperan a que exista un único primary Patroni y a que HAProxy sea un
writer antes de inicializar el schema compartido. También se verificó la
inicialización concurrente mediante advisory lock PostgreSQL y la adaptación
de `LIMIT -1 OFFSET` usada por la limpieza bounded de métricas.

Esto cubre en laboratorio la carrera, partición, takeover, fencing lógico,
replay, crash/outbox, pérdida parcial de etcd, failover de PostgreSQL,
rotación TLS local, backup/restore y límites de carga/históricos. No cubre ni
pretende acreditar:

1. commissioning HIL ni rechazo en el último salto del gateway físico;
2. PKI/mTLS productiva, rotación de secretos, service discovery y network
   policy de dominios de fallo independientes;
3. backups cifrados productivos, restore operativo, RPO/RTO y runbooks;
4. partición real entre hosts/zonas, alertas remotas, retención histórica o
   carga sostenida de producción;
5. active-active, que permanece bloqueado hasta completar HIL y coordinación
   externa cualificada.

La evidencia `lab` se mantiene fuera del gate de producción y no puede
convertirse en evidencia productiva por sí sola.

El registro completo de incidencias encontradas y corregidas durante esta
ejecución está en
[`docs/evidence/multihost-lab-v2-bug-ledger.md`](evidence/multihost-lab-v2-bug-ledger.md).
