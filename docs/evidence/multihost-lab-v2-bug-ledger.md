# Registro de errores y bugs — laboratorio multi-host v2

Fecha de trabajo: 2026-09-06. Alcance: sólo el laboratorio Docker no
productivo y sus contratos/pruebas. Este registro no convierte la evidencia
del lab en cualificación productiva.

## Incidencias encontradas y corregidas

| ID | Síntoma | Causa raíz | Corrección | Verificación |
| --- | --- | --- | --- | --- |
| LAB-001 | El runner comparaba estados JSON con valores mal formados y fallaba escenarios de ownership/partición. | Comparaciones Bash contra campos JSON no normalizados. | Se centralizó `json_field` y se corrigieron las comparaciones booleanas/numéricas. | Ejecución Docker v2 final: 8/8 escenarios. |
| LAB-002 | La reconexión del host tras un failover PostgreSQL dejaba operaciones contra una conexión cerrada. | Patroni puede cerrar la sesión durante promoción; las operaciones no reintentaban. | `_database_call()` reabre la conexión y reintenta operaciones idempotentes ante `psycopg.Error`. | Crash/replay, failover de primary y ejecución final pasan. |
| LAB-003 | Dos hosts arrancando a la vez podían fallar con `duplicate key ... pg_proc ... json_extract`. | Bootstrap concurrente del schema compartido en PostgreSQL. | Advisory lock de sesión alrededor del bootstrap completo; además el lab espera un writer estable antes de iniciar hosts. | Inicialización concurrente y ejecución Docker final pasan. |
| LAB-004 | PostgreSQL rechazaba la limpieza histórica con `LIMIT -1 OFFSET`. | Sintaxis válida en SQLite pero no en PostgreSQL. | `PostgresDatabase.adapt_sql()` elimina el `LIMIT -1` conservando el offset. | Test unitario de adaptación y bounded-load pasan. |
| LAB-005 | El lab podía iniciar hosts mientras Patroni aún elegía primary; aparecían conexiones cerradas y `wait_for_host` podía agotar el deadline. | `wait_for_services` sólo comprobaba que los contenedores estuvieran en estado `running`. | Nuevo `wait_for_database_writer()` exige un único primary y writer HAProxy writable antes de los hosts. | Ejecución reducida y final de 20 carreras pasan. |
| LAB-006 | El load generator no emitía `metric_history_bounded`; el test de operaciones fallaba. | La cola bounded se probaba, pero el repositorio de históricos no se ejercitaba. | Se añadió una SQLite temporal y 12 muestras mediante `MetricHistoryRepository`, con límite 8. | 18 tests dirigidos y bounded-load pasan. |
| LAB-007 | El runner registraba `metric_history_bounded=true` aunque no leyera ese campo del generador. | El resultado se hardcodeaba al construir el registro. | El runner valida y propaga el booleano real del generador. | Registro final contiene el valor producido por la prueba. |
| LAB-008 | La reconexión del owner tras una partición no restauraba el alias de red esperado. | `docker network connect` reconectaba el contenedor sin alias `host-a`. | Reconnect project-scoped con `--alias host-a`. | partition-takeover pasa y rechaza epoch stale. |
| LAB-009 | El perfil TLS rechazaba/aceptaba casos incorrectos: SAN/CA incompletos, certificado expirado no realmente expirado y probe que no distinguía handshake de servidor. | Generación OpenSSL y probe no modelaban completamente mTLS. | Configuración OpenSSL con extensiones, certificado expirado real, SAN correcto y probe con éxito de ambos handshakes. | secure-rotation pasa: válido/rotado aceptados; no confiable/expirado rechazados. |
| LAB-010 | El backup/restore inicial no validaba correctamente los sentinelas restaurados. | Conteos y consultas del restore no se ejecutaban dentro del destino desechable correcto. | Script project-scoped con `pg_dump`, restore aislado, deadline y validación de intents/outbox/métricas. | backup-restore pasa; sentinelas preservadas, dump 825 ms y restore 3629 ms en la corrida final. |
| LAB-011 | La prueba pytest del runner fallaba aunque el runner pasaba. | La aserción incluía el registro `summary` en el conjunto de ocho escenarios y luego lo comprobaba aparte. | El filtro excluye `scenario_id=summary`; la aserción del resumen permanece separada. | Integración pytest pasa con `DOMOAI_LAB_RACE_COUNT=2`; la corrida manual final usa 20. |
| LAB-012 | El contrato documental antiguo exigía exactamente siete servicios y fallaba al leer la topología v2. | No distinguía servicios base de `host-a`/`host-b`, que están correctamente bajo el perfil opt-in `v2`; también faltaba `lab-state` en la expectativa de volúmenes. | La aserción valida siete servicios base, dos hosts perfilados y el volumen compartido del lab. | Batería documental repetida después del cambio. |
| LAB-013 | La regresión de composición falló en `crash-replay`: `host-a` no llegó a estar listo; los logs mostraron conexiones PostgreSQL cerradas y bootstrap concurrente con `duplicate key ... pg_proc ... json_extract`. | `compose up` arrancaba hosts y PostgreSQL simultáneamente; `restart: on-failure` permitía que los hosts inicializaran contra un writer aún inestable, por lo que el advisory lock no bastaba durante una elección/failover de Patroni. | El runner arranca primero sólo etcd/PostgreSQL/HAProxy, espera un writer estable y sólo después crea `host-a`/`host-b`; se añadió contrato de orden de arranque. | Contrato de operaciones/topología: 5 passed. Docker v2: 8/8 escenarios y 20/20 carreras, exit 0; `crash-replay` pasa. |

## Incidencias de validación no funcionales

| ID | Síntoma | Acción |
| --- | --- | --- |
| VAL-001 | Ruff encontró imports no usados/desordenados y líneas >100 en archivos nuevos. | Imports ordenados/eliminados y líneas partidas; Ruff dirigido pasa. |
| VAL-002 | La primera batería global se quedó ejecutando integración Docker sin salida visible. | Se inspeccionó el proceso, se dejó constancia y se repitió el gate con timeout; no se trató como éxito hasta obtener resultado. |
| VAL-003 | Una espera auxiliar para recoger el resultado del composition check no terminaba. | El patrón de búsqueda incluía el propio comando de espera; se descartó ese resultado y se relanzó `project-composition-check` directamente. |
| VAL-004 | Graphify avisa que 17 migraciones SQL no aportan nodos. | Falta el parser opcional `tree_sitter_sql`; la extracción Python terminó y el aviso queda como limitación no bloqueante. |
| VAL-005 | Graphify incremental no pudo extraer 93 documentos por falta de una clave semántica. | El entorno no tiene API key disponible. | Se ejecutó `--code-only`; el grafo estructural se actualizó a 8279 nodos y 24629 enlaces. La extracción documental sigue siendo una limitación explícita, no un resultado inventado. |

## Estado al cierre

- No quedan errores funcionales conocidos en la matriz del laboratorio.
- La evidencia final es 8/8 escenarios y 20/20 carreras de ownership; LAB-013
  quedó corregido y verificado.
- `project-composition-check domoai`: 530 tests pasados, 18 omitidos; 4
  contratos arquitectónicos conservados y 0 rotos.
- Suite completa `uv run pytest -q`: 1906 pasados, 18 omitidos, 0 fallos.
- HIL físico, PKI/service discovery productivos, fault domains, RPO/RTO y
  active-active no son bugs del lab: son límites de cobertura y permanecen
  pendientes/bloqueados para producción.
