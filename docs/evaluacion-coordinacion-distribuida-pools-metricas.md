# Evaluación: coordinación distribuida, pools y métricas remotas

**Fecha:** 2026-09-04
**Ámbito:** evolución posterior a la Fase 1
**Decisión:** mantener single-writer por defecto y no añadir una dependencia
remota. La base provider-neutral de lease/fencing, ledger idempotente, colas
por hogar, identidad de instancia e histórico bounded está implementada; la
activación multi-host productiva sigue bloqueada hasta inyectar y cualificar
un coordinador externo real.

## Resumen ejecutivo

El runtime actual está correctamente acotado para una vivienda y un despliegue
único. La coordinación de autoridad se resuelve con un lock POSIX y una fila
durable de ownership en SQLite; los trabajos bloqueantes tienen admisión
acotada y lifecycle propietario; y las métricas son un snapshot JSON local,
consultable por MCP.

Estas decisiones son seguras para el modelo actual, pero no constituyen una
plataforma active-active:

- no hay lease distribuido ni fencing token que invalide a un proceso aislado;
- los workers y sus colas viven dentro de un proceso y no se comparten entre
  réplicas;
- el exporter Prometheus pull es ahora opcional y bearer-protected, pero no
  existe push OTLP, agregación multi-réplica ni historial remoto.

La evolución debe ser una ampliación explícita de la frontera de autoridad,
no un cambio de despliegue que permita dos runtimes a escribir sobre la misma
casa sin fencing.

## Evidencia del código

### 1. Coordinación y ownership

`RuntimeOwnership.acquire()` toma `SQLiteAdvisoryLock` de forma no bloqueante
antes de conectar los adapters y genera un `owner_id` por proceso
([`runtime_ownership.py`](../src/domoai/application/runtime_ownership.py:44-74)).
El repositorio comprueba atómicamente `deployment_id`, `owner_id`, `status` y
`uncertain` dentro de `BEGIN IMMEDIATE`
([`repositories.py`](../src/domoai/persistence/repositories.py:62-103)).

Esto proporciona una propiedad fuerte para un solo host y un solo fichero de
SQLite: un segundo runtime no conecta adapters si el deployment está activo,
y un owner incierto no se recupera automáticamente. La recuperación requiere
una acción offline que verifique que el proceso anterior ya no está vivo.

El límite es estructural: `fcntl.flock()` sobre un fichero local
([`sqlite.py`](../src/domoai/persistence/sqlite.py:43-87) no coordina hosts,
contenedores o volúmenes independientes. Además, el owner actual no tiene un
epoch/fencing token que deba acompañar a cada escritura física. Por eso la
documentación de la pasarela mantiene active-active explícitamente soportado
como `no` ([`unified-mcp.md`](unified-mcp.md:83-89)).

**Valoración:** correcto y fail-closed para una casa; insuficiente para
active-active o failover automático.

### 2. Pools, colas y aislamiento

La persistencia usa `SerializedStorageExecutor`, una cola bounded y un único
hilo propietario por lane. La capacidad por defecto es 128 elementos en cola
y un slot activo adicional; los timeouts no cancelan una operación SQLite a
mitad de ejecución, sino que esperan a drenar su resultado
([`serialized.py`](../src/domoai/persistence/serialized.py:50-89,
[`serialized.py`](../src/domoai/persistence/serialized.py:118-154)).

`build_runtime()` crea lanes separados para persistencia autoritativa y
auditoría, con conexiones SQLite separadas
([`runtime_factory.py`](../src/domoai/application/runtime_factory.py:765-780)).
Esto evita que un backlog de auditoría bloquee la autoridad y conserva el
invariante de un propietario por conexión.

La optimización tiene dos fronteras locales:

- `OptimizationWorker` usa `ThreadPoolExecutor`, con `max_concurrency` y
  capacidad de admisión limitada ([`optimization_worker.py`](../src/domoai/application/optimization_worker.py:20-59));
- CP-SAT usa `ProcessOptimizationWorker` y `pebble.ProcessPool`, con deadline
  aplicado por el proceso servidor y reemplazo del worker tras un timeout
  ([`process_optimization_worker.py`](../src/domoai/application/process_optimization_worker.py:43-74,
  [`process_optimization_worker.py`](../src/domoai/application/process_optimization_worker.py:139-185)).

Ambos workers se registran en `RuntimeComposition` y se cierran junto al
runtime. No existe un pool distribuido, una cola durable de trabajos ni
afinidad por hogar. Es una decisión prudente mientras el volumen sea local:
un pool compartido no debe poder ejecutar comandos físicos sin volver a pasar
por la autoridad y la idempotencia del hogar.

**Valoración:** bounded y bien aislado para la carga actual; no hay evidencia
de que aumentar concurrencia o distribuir workers sea necesario en Fase 1.
Antes de cambiar tamaños deben medirse p95/p99 de admisión, cola, solver,
SQLite y auditoría bajo carga representativa.

### 3. Métricas y exportación remota

`RuntimeOperationalMetrics` es thread-safe, bounded y deliberadamente no es
autoridad. Ahora incluye identidad de instancia y contadores de fencing; el
histórico durable separado está limitado por muestras y no participa en la
decisión física ([`operational_metrics.py`](../src/domoai/runtime/operational_metrics.py)).
Limita las series por `adapter_id`/`capability`, valida labels y cuenta
overflow/fallos sin dejar que la telemetría falle dentro de la operación
observada ([`operational_metrics.py`](../src/domoai/runtime/operational_metrics.py:100-132)).

`RuntimeMetricsCollector` compone ese snapshot con salud de adapters, colas,
SQLite, lanes de storage, scheduler y worker
([`metrics.py`](../src/domoai/application/metrics.py:55-95,
[`metrics.py`](../src/domoai/application/metrics.py:150-213)). La resource
`domotics://metrics` lo expone como lectura autenticada del gateway
([`domotics_server.py`](../src/domoai/mcp/domotics_server.py:1110-1115)).

Spec 188 añade un endpoint de scrape Prometheus opcional y bounded, con bearer
hash-only, allowlist de familias/labels y proxy Caddy explícito. No hay
exportación OTLP, buffer remoto, retención, agregación entre réplicas ni
garantía de continuidad de contadores tras un reinicio. El endpoint resuelve
scrape de una instancia, pero no alertas multi-instancia ni análisis histórico.

**Valoración:** suficiente para la qualification de Fase 1 y para scrape remoto
de una instancia; la observabilidad histórica/distribuida sigue siendo una
evolución separada y no es requisito para ejecutar comandos.

## Riesgos si se escala sin diseño adicional

1. **Split-brain físico:** dos procesos podrían conservar sockets abiertos
   durante una partición y emitir órdenes si solo se añade un balanceador.
2. **Fencing inexistente:** liberar o renovar una fila de ownership no invalida
   una orden ya en vuelo en un adapter remoto.
3. **Duplicación de trabajos:** una cola local no evita que dos réplicas
   reintenten la misma intención sin una clave durable y un resultado único.
4. **Pérdida de trazabilidad:** snapshots process-local no permiten saber qué
   instancia produjo una latencia o una decisión después de reinicios.
5. **Retro-presión cruzada:** un pool global puede hacer que optimización,
   auditoría y comandos físicos compitan por los mismos recursos.

## Diseño recomendado para la evolución posterior

### P3.1 — Ownership distribuido y fencing

Adoptar una autoridad externa con semántica de lease y token monotónico por
`household_id`/`deployment_id`. Cada operación de escritura física debe llevar
el token actual o ser rechazada por el gateway/adaptador. La renovación debe
ser periódica, con margen de seguridad, y la pérdida de lease debe detener o
dejar en estado desconocido toda orden latched.

El contrato debe probar:

- exclusión bajo carreras de adquisición y renovación;
- token antiguo rechazado después de takeover;
- partición de red y recuperación sin doble escritura;
- idempotencia por `idempotency_key` entre réplicas;
- migración de SQLite sin perder planes, grants, outbox ni auditoría.

No basta con sustituir `fcntl` por un lock de Redis sin fencing. La opción de
persistencia, proveedor de lease y procedimiento de recuperación debe quedar
decidida antes de permitir más de una autoridad por hogar.

### P3.2 — Pools y workers

Primero ejecutar un benchmark reproducible con carga de discovery, refresh,
auditoría, ejecución y CP-SAT. Registrar p50/p95/p99, profundidad de cola,
rechazos por overload, memoria, CPU y tiempo de cierre.

Después separar explícitamente:

- lane de autoridad física, nunca compartida con tareas best-effort;
- lane de persistencia y outbox por hogar o partición durable;
- workers de optimización cancelables y sin acceso directo al adapter;
- límites independientes por tenant/hogar y por clase de trabajo;
- reintentos idempotentes con resultado durable.

La primera mejora probable es ajustar límites y observabilidad, no introducir
un pool distribuido. El solver process-backed ya cubre el riesgo más importante
de timeout no cancelable en CP-SAT.

### P3.3 — Exportación remota de métricas

Implementar como canal opcional de diagnóstico, separado de ejecución y
persistencia autoritativa:

- preferir endpoint pull Prometheus en el gateway o un exporter OTLP
  configurable;
- exportar solo métricas numéricas acotadas y labels de baja cardinalidad
  (`deployment`, `household`, `adapter`, `outcome`), nunca IDs de usuario,
  tokens, payloads ni valores sensibles;
- incluir `instance_id`, `process_start_time` y semántica explícita de reset;
- hacer envío no bloqueante, con cola bounded y descarte medible;
- si el exporter falla, conservar ejecución y registrar únicamente
  `telemetry_failure_total`;
- mantener `domotics://metrics` como snapshot local autenticado, no como
  reemplazo de una serie temporal remota.

La primera versión debe probar cardinalidad, backpressure, caída del collector,
reinicio, TLS/autenticación y ausencia de secretos en labels o payloads.

## Criterio de entrada a la evolución

Abrir una especificación separada cuando exista al menos uno de estos hechos:

- más de una réplica necesaria por disponibilidad o volumen;
- p95/p99 de storage o solver cerca del límite configurado;
- backlog observable que requiera repartir trabajo entre procesos/hosts;
- necesidad operativa de alertas históricas o correlación multi-instancia.

Hasta entonces, la decisión es mantener una única autoridad por deployment,
lanes bounded process-local y métricas MCP de diagnóstico. La evaluación queda
cerrada como evolución posterior y no como pendiente automatizable de Fase 1.

## Medición ejecutada — 2026-09-04

Se ejecutó una medición acotada sobre el runtime disponible y se registró la
evidencia completa en [`evidence/production-readiness-latest.md`](evidence/production-readiness-latest.md):

- el MCP live mantuvo `adapter_connected=true`,
  `event_consumer_alive=true`, 12 outcomes `confirmed_success`, cero
  mismatches de readback, cero overflow y cero fallos de telemetría;
- 100 operaciones contra `SerializedStorageExecutor` bounded de 32 posiciones
  produjeron 98 completadas y 2 rechazos por overload, sin timeout ni error,
  con p95 de espera de `0.016188 s`;
- el caso de 50 cargas del solver pasó bajo su deadline interno de 5 segundos;
  el proceso pytest total fue de 8.24 s por importación y arranque.

La muestra demuestra backpressure y límites locales, pero no aporta evidencia
de partición, failover, fencing o backlog sostenido multi-host. Por tanto no
activa el criterio de entrada a active-active o pool distribuido. El endpoint
pull de Spec 188 queda habilitado solo como diagnóstico por instancia; la
decisión de autoridad sigue siendo `single-writer + bounded process-local`.

## Implementación ejecutada — Spec 188

Se implementó y validó el primer escalón de métricas remotas:

- `DOMOAI_MCP_METRICS_ENABLED` está desactivado por defecto y exige el fichero
  de clientes hash-only cuando se habilita.
- `GET /metrics` valida `Authorization: Bearer` con el mismo verificador que
  MCP, devuelve texto Prometheus v0.0.4 y limita la respuesta a 262144 bytes
  por defecto.
- El renderer solo emite nombres/familias allowlisted, labels bounded de
  adapter/capability o enums fijos, y omite secretos, claims y mensajes de
  proveedores.
- Caddy reenvía `/metrics` al gateway interno; el fallback permanece en 404.
- La validación ejecutada cubre renderer, settings, contrato, proxy y un
  runtime real: `25 passed` en la matriz enfocada.

La implementación de base no convierte dos procesos en autoridades: el modo
normal sigue siendo `single_writer`. La inyección de un coordinador externo
es una condición explícita para `multi_host_enabled`; sin ella el arranque
falla fail-closed. El histórico es local y bounded, no agregación remota.
