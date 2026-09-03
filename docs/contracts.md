# Contratos públicos

## Fuente única y serialización

Los modelos Pydantic de
[`src/domoai/domain/models.py`](../src/domoai/domain/models.py) y
[`src/domoai/optimizer/`](../src/domoai/optimizer/) son la fuente de verdad.
Los JSON Schema publicados en [`schemas/v1/`](../schemas/v1/) se regeneran con
`uv run python scripts/export_schemas.py` y se validan en las pruebas de
contrato.

Los modelos usan `schema_version: "v1"`, timestamps con zona horaria y
`extra="forbid"`. Por tanto, una entrada desconocida no se ignora
silenciosamente: produce un error de validación seguro.

## Superficie MCP v1

### Gateway compartido para agentes

El entrypoint de red `domoai-mcp-gateway` publica la misma superficie semántica
por Streamable HTTP en `DOMOAI_MCP_PUBLIC_URL + DOMOAI_MCP_PATH`. Todos los
clientes compatibles comparten el proceso, el estado, el scheduler y la
autoridad física. `domoai-mcp` conserva stdio para desarrollo y compatibilidad.

Los tokens de cliente son credenciales de transporte server-owned. El scope
`mutate` es necesario para pedir approvals, ejecutar, programar o cancelar;
la autenticación del agente no sustituye una aserción de consentimiento
humano. `/healthz` solo indica liveness y `/readyz` exige lifecycle y adapter
conectados. Los binds no locales requieren HTTPS y token file.

En la deployment Compose de referencia, Caddy es el único borde publicado.
El puerto 8124 del gateway queda solo en la red interna, y las rutas públicas
se limitan a /mcp, /healthz y /readyz. Home Assistant y MQTT se enlazan a
localhost para operación del host, no como endpoints remotos; sus puertos
locales por defecto son 8123 y 1883 y pueden cambiarse mediante variables de
deployment server-owned si el lab virtual ya los ocupa, manteniendo siempre el
bind loopback. /healthz prueba liveness; /readyz conserva la señal estricta de
readiness sin que el proxy la transforme.

El gateway compartido aplica bootstrap estricto: si no existe al menos un
adapter/provider configurado, `domoai-mcp-gateway` falla con un error explícito
y no selecciona `SimulatedHomeAdapter`. La ruta `domoai-mcp` puede usar el
simulador únicamente como fixture local explícito. Antes de construir el
runtime, el builder valida el token file; una credencial ausente o inválida no
debe abrir SQLite, reclamar ownership ni conectar providers.

Con `DOMOAI_BOOTSTRAP_PROFILE=lab`, el bootstrap puede seleccionar el mapping y
los assets canónicos `dispatchable-battery-lab.json` y
`ev-charging-lab.json` cuando HA local está autenticado y reachable, tanto si
el endpoint fue descubierto como si se declaró explícitamente como
`http://127.0.0.1:8123`, y no hay paths explícitos. Esto solo carga bindings
server-owned: no infiere rutas, no crea evidencia HIL ni habilita production
dispatch. El manifest registra esos paths no secretos en
`operational_paths`, y `/readyz` continúa bloqueando la autoridad física hasta
qualification válida. URLs remotas o endpoints locales inalcanzables no
seleccionan assets del lab.

En la topología Windows/WSL de KNX Virtual, la IP del upstream de Windows y la
IP del túnel local no son la misma autoridad. El launcher de `knxd` debe usar
el endpoint Windows actualmente alcanzable (en WSL2 mirrored suele ser
`127.0.0.1:3671`) y el gateway DomoAI debe usar `127.0.0.1:3672`. Un túnel
KNX conectado no implica disponibilidad: discovery/readback debe recibir
respuestas válidas de las direcciones configuradas. `/readyz` conserva esta
señal estricta y no se relaja para ocultar una configuración ETS incompleta.

`domotics://runtime` es una resource autenticada de solo lectura con esta forma
estable v1:

```json
{
  "schema_version": "v1",
  "runtime_revision": "...",
  "providers": [{"provider_id": "...", "active": true}],
  "writable_capabilities": [
    {
      "device_id": "...",
      "capability": "...",
      "commands": ["..."],
      "available": true,
      "providers": ["..."]
    }
  ],
  "authority": {
    "physical_execution": "plan_executor",
    "risky_mutations": "policy_and_operator_approval",
    "battery_dispatch": "unsupported"
  }
}
```

La resource describe composición y disponibilidad; no transforma una ruta
writable en autorización. Toda mutación continúa atravesando plan, policy,
approval, freshness, safety y executor.

La evidencia EV también es agregada: `StateStoreEVProvider` conserva la edad
del componente más antiguo entre conexión, SOC, capacidad y departure. La
conexión reciente no puede rejuvenecer una lectura antigua de SOC/capacidad,
y cualquier cambio en esos snapshots —incluida la hora de salida— cambia la
`source_revision` que ata el contexto de energía a la validación.

El único servidor stdio local registra estas tools semánticas:

| Tool | Efecto |
| --- | --- |
| `discover_devices` | Lee o refresca el inventario canónico; admite `area_id`, tipos y `refresh`. |
| `inspect_commissioning` | Lee el informe v1 compartido de candidatos de batería/EV; solo `refresh=true` ejecuta discovery y nunca crea autoridad. |
| `get_state` | Lee estados acotados por dispositivos/capacidades. |
| `get_energy_context` | Lee un horizonte completo de tarifas, solar y batería opcional mediante un provider tipado. |
| `validate_command` | Valida un comando sin invocar el adapter. |
| `validate_plan` | Aplica capacidades, políticas, revisión y digest a un plan. |
| `request_approval` | Emite un `ApprovalGrant` de un solo uso, ligado al digest del plan/bundle y a una aserción humana del host. Único origen válido de una aprobación. |
| `execute_plan` | Ejecuta un plan validado. Si requiere confirmación, exige un `approval_id` emitido por `request_approval`; ya no acepta un objeto de aprobación construido por el caller. |
| `validate_scenario` | Valida un escenario de optimización contra dispositivos y capacidades canónicas. |
| `optimize_scenario` | Produce una propuesta determinista sin ejecutar comandos físicos. |
| `explain_solution` | Explica una propuesta tipada sin cambiar el estado del runtime. |

Resources de solo lectura:

```text
domotics://areas
domotics://capabilities
domotics://devices
domotics://energy
domotics://policies
domotics://commissioning
```

`domotics://commissioning` y `inspect_commissioning` exponen evidencia de
comisionamiento, no bindings. Un candidato `ready_for_binding` todavía
requiere profile, identidad, takeover, readback, HIL y las gates de producción
server-owned; discovery nunca convierte una ruta en autoridad física.

## Bundle commit boundary v3 (2026-08-21)

La Skill energética v3 sustituye la ejecución/programación miembro a miembro
por `commit_or_schedule_bundle`. La tool recibe el `bundle_digest`, el
`scenario_id` y todos los miembros ordenados con su `validation_digest`,
`execute_at` y, cuando corresponde, el `approval_id` emitido por
`request_approval`.

El runtime persiste un agregado `BundleCommit` antes de mutar el mundo físico:

- todos los miembros se someten a preflight antes de la primera escritura;
- si todos son futuros, las filas de scheduling y el estado agregado se
  confirman en una única transacción SQLite;
- los miembros físicos se ejecutan secuencialmente y un fallo posterior queda
  expresamente como `partially_committed`, `failed` o `unknown` según la
  evidencia disponible;
- la recuperación al arrancar reconcilia planes y schedules persistidos, no
  vuelve a ejecutar acciones y no promete rollback automático.

La auditoría registra `bundle_commit_started`, `bundle_commit_completed` y
`bundle_commit_recovered` con el `bundle_commit_id`, el escenario, el número de
miembros, el estado agregado y `replayed: false` en recuperación. La resource
`domotics://metrics` mantiene los contadores operativos existentes; el estado
durable del agregado y sus miembros es la fuente autoritativa de progreso.

## Composite event backpressure (2026-08-21)

`CompositeAdapter` mantiene las lanes `bulk` y `priority` acotadas por
`event_queue_max_size`. Los eventos estructurales aplican backpressure al
productor cuando la lane prioritaria está llena; no se descartan mientras el
consumer siga drenando. Los `state_changed` repetidos con identidad segura
usan latest-wins; el overflow restante se agrega en contadores.

La resource `domotics://metrics` expone, además de `event_queue_depth` y
`dropped_events_total`, `dropped_events_by_adapter`,
`dropped_events_by_kind` y `coalesced_events_total`. Los diagnósticos de fallo
del adapter se conservan en una ventana de memoria acotada; el audit durable
no se modifica.

`completed` significa que el commit agregado terminó; `scheduled` significa que
las acciones futuras quedaron duraderamente registradas. Ninguno debe
interpretarse como confirmación de que una acción futura ya se ejecutó.

Contrato detallado: [`specs/087-bundle-commit-saga/contracts/bundle-commit-v3.md`](../specs/087-bundle-commit-saga/contracts/bundle-commit-v3.md).

## Aserción explícita de consentimiento humano (2026-08-23)

Una `OperatorPrincipal` autenticada no equivale a consentimiento. Para emitir
un grant mediante el camino de host confiable, `request_approval` exige una
`ApprovalAssertion` que liga principal, digest de validación o bundle, nonce,
instante de aprobación y expiración. El nonce solo puede emitir un grant una
vez; el grant vuelve a comprobar su expiración al consumirlo para scheduling o
ejecución.

Si el host solo proporciona principal, el runtime responde
`approval_assertion_required` y no crea `approval_id`. Los campos de identidad
que pudiera enviar el caller no sustituyen la evidencia del host. El camino
de token queda únicamente como compatibilidad local/dev explícita y
deshabilitada por defecto; ni tokens ni secretos se serializan en la respuesta
MCP.

Contrato detallado: [`specs/128-explicit-approval-assertion/contracts/approval-assertion-v1.md`](../specs/128-explicit-approval-assertion/contracts/approval-assertion-v1.md).

## Verificación JIT de autoridad persistida (2026-08-30)

Un `Plan` persistido en estado `APPROVED` no es por sí mismo una fuente de
autoridad. Antes de cualquier claim o write físico, `ExecutionAdmission` exige
que `ApprovalStore` pueda cargar el `approval_id`, comprobar que el grant fue
consumido de forma single-use, validar su TTL y demostrar que coincide con la
proyección persistida completa: digest de validación, scope de plan/bundle/
recurrence, identidad, sesión, ventana, revisión y expiraciones.

La aprobación de un bundle solo puede verificarse con el `bundle_digest` del
agregado comprometido. Una aprobación de recurrencia no autoriza una ejecución
puntual. Un `dry_run` únicamente valida la autoridad y no consume el grant ni
persiste cambios de plan, aprobación, bundle o scheduling.

El grant se conserva en el repository durable de autoridad y puede
revalidarse después de un restart; la proyección `Plan.approval` no sustituye
ese ledger. La aprobación local atendida usada por HIL pasa por la misma
emisión/consumo interna, permanece fuera de MCP y no equivale a qualification
de producción.

## Freshness, disponibilidad y verdad de estado (2026-08-30)

`StateStore` conserva las observaciones por fuente y deriva una vista canónica
por `(device_id, capability)`. La vista no es last-writer-wins: dos valores
`CURRENT` distintos producen `INVALID` y valor nulo; la caída de una fuente
solo degrada sus snapshots, dejando intacta la evidencia independiente de
otras fuentes.

`observed_at` y `received_at` se conservan cuando el proveedor los entrega. La
lectura de una caché de Home Assistant no puede rejuvenecer una observación
antigua. La edad se comprueba en JIT por `FreshnessEvaluator`, aunque ningún
sweeper haya ejecutado antes `mark_stale()`. Las decisiones distinguen
evidencia ausente, expirada, stale no autorizada, unavailable, invalid y futura;
una policy que acepta stale nunca convierte `UNAVAILABLE` o `INVALID` en
autorizable.

Las señales estructuradas `source_unavailable` del `CompositeAdapter` llegan a
`RuntimeEventConsumer`, que marca solo el adapter afectado. La fuente vuelve a
ser utilizable únicamente después de reconexión y discovery, evitando que un
refresh de otra fuente reinstale silenciosamente una caché sana aparente.

## Supervisión de actuadores latched (2026-08-30)

Una ruta de mapping no concede por sí sola autoridad de actuador. Un binding
dispatchable debe resolver su `provider_id` a un adapter concreto; un
`CompositeAdapter` solo sirve para enrutar y nunca es el dueño físico. Si la
ruta de takeover no existe, es ambigua o no expone la operación requerida, el
runtime falla cerrado.

Antes de adquirir un lease de batería, `BatteryControlCoordinator` exige una
reconciliación startup. Consulta la ruta de feedback en vivo (también cuando
no hay snapshot persistido), envía un stop idempotente si el estado no es
seguro y solo abre la admisión tras confirmar un readback `CURRENT`, fresco y
dentro de la tolerancia. Un ACK de transporte no equivale a parada física.

Los leases tienen ownership por dispositivo, expiración y renovación
opcional. Si la renovación falla, el supervisor intenta stop y conserva la
autoridad revocada cuando no puede confirmar potencia cero. La liberación
normal de un plan físico usa el mismo stop/readback; el executor no repite el
write del plan tras un restart y el coordinador reconcilia el actuador antes
de volver a admitir órdenes.

## Solver y guards físicos JIT (2026-08-30)

El límite de horizon se valida en el proceso que recibe la propuesta y antes
de enviar trabajo al worker CP-SAT. Las restricciones `battery_min_soc` y
`battery_max_soc` se aplican a todos los estados `soc[0..N]`, incluido el
terminal; un caso de un solo slot no puede terminar por debajo de la reserva.

### Frontera del worker CP-SAT

El solve CP-SAT de la ruta configurada cruza una frontera de proceso explícita:
`ProcessOptimizationWorker` valida el escenario y sus límites en el proceso
padre, y solo envía al worker persistente el `OptimizationScenario` ya validado.
El worker usa contexto `spawn`, aplica el `WorkerBudget` y mata el proceso
concreto cuando vence el deadline; el pool crea un reemplazo para la siguiente
solicitud. El worker no recibe `DeviceRegistry` ni puede acceder a adapters,
planes, aprobaciones o estado físico. El contexto energético I/O sigue usando
el worker de hilos correspondiente.

Las constraints del escenario son evidencia de planificación del forecast. La
admisión física no las interpreta como una garantía actual del mundo: para
batería y EV `DynamicSafetyGuard` vuelve a comprobar en JIT el SOC,
telemetría fresca y el envelope de potencia; para EV exige además conexión y
que `departure_at` observado siga abierto. Timestamps futuros, snapshots no
current y valores fuera del profile se rechazan antes del adapter. La parada
queda disponible como operación de seguridad incluso cuando la telemetría de
carga no es utilizable.

## Qualification HIL y evidencia de identidad (2026-08-30)

La CLI HIL carga un único `DispatchableBatteryBinding` y lo pasa explícitamente
al composition root; no puede ejecutar el profile B sobre un runtime construido
con A. Cuando el binding llega como argumento programático, el bootstrap no
vuelve a auto-seleccionar el profile de batería del lab; si `Settings` contiene
además un path explícito, la composición lo rechaza como conflicto. Los valores
de prueba se limitan al envelope del profile y al ceiling
server-owned del deployment. `takeover_baseline` exige un `TakeoverResult`
adquirido, con owner/device/provider/capability correctos, baseline observado,
lease aún vivo y primer comando confirmado.

Un adapter que quiera participar en qualification puede exponer el hook
opcional `read_hil_identity()`, que devuelve un `AdapterIdentityObservation`
con hardware, firmware, `SourceRef` y timestamp observados por el provider.
Las cadenas `--hardware-id` y `--firmware-version` del operador solo se marcan
como observadas si coinciden con ese retorno; la evidencia incluye un digest
ligado a identidad, provider, profile y timestamp. Una nota manual se clasifica
como `verified`, `not_verified`, `not_exercised` o `not_applicable`; solo la
primera puede contribuir a `hil-qualified`. El artefacto sigue necesitando una
autoridad de confianza para convertirse en credencial de producción.

## Identidad de fuentes y readbacks durables (2026-08-30)

`SourceRef.source_device_id` conserva la identidad estable que un provider usa
para agrupar entidades físicas, separada del `external_id` mutable (por
ejemplo, un `entity_id` de Home Assistant). `DeviceRepository` lo serializa
como parte del dispositivo y `DeviceRegistry.load_persisted()` reconstruye el
índice de identidad con ese valor. La rehidratación solo reserva el canonical
ID: no restaura `CapabilityRoute` ni permite commands hasta que un discovery
actual confirme la fuente y reconstruya la ruta.

Los readbacks tienen una única autoridad durable en runtime compuesto:
`PlanExecutor → StateStore → RuntimeStatePersistencePort`. El sink de snapshots
de repository se conserva únicamente como compatibilidad para fixtures que no
han enlazado el puerto de persistencia del `StateStore`, evitando doble I/O y
que un segundo write redundante convierta un readback confirmado en `UNKNOWN`.
Cuando discovery elimina una entidad o dispositivo, también elimina sus
snapshots activas a través del mismo owner de persistencia.

## Runtime safety hardening (2026-08-18)

Cierra el trust boundary MCP-agente descrito en
`specs/021-runtime-safety-hardening/`:

- `PolicyEngine.evaluate` ya no confía en `Command.risk_class` del caller.
  Un `RiskClassifier` server-side clasifica `(device, capability, command)`
  de forma independiente; el riesgo efectivo es el máximo entre lo que
  clasifica el runtime y lo que envía el caller, así una política solo puede
  escalar el riesgo, nunca rebajarlo.
- `build_runtime()` carga `Settings.policy_config_path` (TOML,
  `load_policy_file`) en producción en vez de construir un `PolicyEngine([])`
  vacío; si no hay archivo configurado, se registra un evento de auditoría
  `policy_default_applied` en vez de arrancar en silencio sin políticas.
- `execute_plan` ya no acepta un objeto `approval` construido por el
  caller. `PlanService.approve()`/`DomoticsFacade.approve_plan()` requieren
  un `ApprovalGrant`, que solo puede emitir `ApprovalStore.issue()` a través
  de la nueva tool `request_approval`. El grant es de un solo uso y está
  ligado al `plan_id`/`validation_digest`.

Ver `specs/021-runtime-safety-hardening/contracts/mcp-tools.md` para el
contrato detallado de `request_approval`/`execute_plan`.

## Dependencias granulares de plan (2026-08-18)

Cierra `specs/022-granular-plan-dependencies/`: `PlanService` ya no invalida
un plan validado por un `runtime_revision` global único.

- `StateStore.runtime_revision` (inventory revision) ahora solo avanza
  cuando `DiscoveryService.refresh()` detecta un cambio real en el
  conjunto de dispositivos/capacidades/disponibilidad — no en cada pasada
  de discovery.
- `StateStore` trackea una versión por `(device_id, capability)`, que solo
  avanza cuando ese valor/status concreto cambia.
- `ValidationResult` gana un campo aditivo `dependencies: PlanDependencies
  | None`, con `inventory_revision`, `policy_revision` y los
  `state_versions` exactos que el plan usó (uno por comando resuelto).
- `PlanService.assert_executable()` compara solo esas dependencias
  concretas, no una cadena global. Un sensor no relacionado ya no invalida
  planes; un cambio real en lo que el plan depende sí sigue marcándolo
  `STALE_PLAN`.
- `ValidationResult.runtime_revision` mantiene su forma/significado
  anterior (compatibilidad con `EnergySkillWorkflow`); `dependencies` es
  aditivo, no lo reemplaza.

### Fingerprints de semántica ejecutable (2026-08-21)

La revisión de inventario ya no observa solo nombres de capabilities y
disponibilidad. `DiscoveryService` canoniza y hashea la semántica que puede
alterar una escritura física: tipo/área/disponibilidad/protocolo del device,
kind/unidad/permisos/bounds/enums/commands/constraints de la capability y
metadata de todas sus rutas y `SourceRef`. Nombres de presentación y
`last_seen_at` quedan fuera para evitar churn cosmético.

`PlanDependencies.capability_fingerprints` es un mapa aditivo por
`device_id::command`. `PlanService.assert_executable()` compara esa evidencia
con el registry actual antes de reclamar o escribir un plan. Un plan persistido
con dependencias antiguas sin fingerprints falla cerrado y exige revalidación.

El mismo fingerprint global se persiste en la metadata JSON de runtime junto a
`inventory_revision`. Tras un restart sirve de baseline para la primera
rediscovery, evitando que la ausencia temporal de rutas en el registry
rehidratado genere un `rev-*` falso; una metadata histórica sin el campo se
reconfirma de forma conservadora.

El registry reemplaza metadata cuando la misma source identity se actualiza,
pero conserva el diagnóstico y la postura conservadora ante metadata
incompatible de otra fuente. No se añade migration SQLite: los planes son JSON
y el campo nuevo tiene default vacío.

## Event pipeline incremental (2026-09-03)

Cierra `specs/174-event-driven-refresh-and-graceful-shutdown/` sobre la base de
`specs/023-incremental-event-pipeline/`: `RuntimeEventConsumer` ya no hace
`DiscoveryService.refresh()` completo por cada evento y el refresher no genera
polling físico redundante para fuentes con eventos autoritativos.

- Eventos `kind="state_changed"` toman un camino barato. Los adapters que
  declaran un stream de estado autoritativo (actualmente KNX) transportan la
  observación recibida dentro del evento y el runtime la persiste directamente,
  sin ejecutar otra lectura física. Los eventos legacy/no autoritativos pueden
  leer solo los `SourceRef` conocidos de ese adapter, sin re-descubrir
  identidad/capacidades. Esto evita el ciclo `GroupValueRead` →
  `GroupValueResponse` → `StateChangedEvent` en KNX.
- Los eventos `availability_changed` actualizan la disponibilidad de la fuente
  y su estado cacheado mediante la frontera compartida, sin reconstruir el
  inventario. La pérdida de un child de un composite degrada solo esa fuente.
- Los diagnósticos ordinarios no disparan lecturas físicas globales. Los
  eventos `device_membership_changed` y `metadata_changed` sí conservan la
  `discovery.refresh()` completa porque modifican inventario ejecutable o
  semántica. Un kind desconocido falla cerrado y no hace una lectura amplia.
- `RuntimeStateRefresher` excluye de `refresh_state()` las fuentes que declaran
  eventos de estado autoritativos. KNX además declara su inventario estático,
  por lo que las rediscoveries periódicas no vuelven a enviar
  `GroupValueRead`; discovery inicial, telegramas del bus y readback explícito
  siguen siendo válidos.
- `read_state()` devuelve `device_id` como el external_id crudo del
  adapter, no el canonical id del registry; `_apply_state_only` lo remapea
  vía `registry.canonical_id_for_source(...)` antes de guardar, igual que
  ya hacía `PlanExecutor._readback`.
- La extensión interna `payload.states` solo transporta evidencia ya recibida;
  no cambia el contrato MCP. La fuente sigue siendo responsable de no emitir
  una observación sin timestamps válidos.

## Registry reconciliation (2026-08-18)

Cierra `specs/024-registry-reconciliation/`: `DeviceRegistry.apply_snapshot`
ya no es puramente aditivo.

- Tras aplicar un snapshot, se reconcilia: para cada adapter_id
  "autoritativo" esta ronda (no reportado como `failure` en
  `snapshot.unsupported_sources` — misma señal que ya usa
  `DiscoveryService.refresh()` para `mark_source_unavailable`), cualquier
  `SourceRef` conocido de ese adapter que no apareció en esta ronda se
  elimina (su ruta también); si un dispositivo se queda sin `source_refs`,
  se elimina del registry entero.
- Un adapter reportado como fallido/desconectado nunca pierde sus
  dispositivos por reconciliación — solo la marca de no disponible ya
  existente. Un dispositivo respaldado por dos adapters distintos sobrevive
  perdiendo solo uno.
- `self._identity_to_canonical` nunca se toca en la reconciliación, así que
  la misma identidad reaparecida más tarde recupera el mismo canonical ID.
- Sin nuevo estado de lifecycle en `Device`/`AvailabilityStatus`: un
  dispositivo reconciliado simplemente desaparece del registry.
- Se añadió `tests/unit/runtime/__init__.py` (faltaba) para que los tests de
  ese directorio puedan importar `tests.fixtures.*` de forma independiente.

## Persistencia de state/inventory (2026-08-18)

Cierra `specs/025-state-inventory-persistence/`: `devices` y
`state_snapshots` ya se leen/escriben en SQLite.

- Nuevo `DeviceRepository` (reutiliza la tabla `devices` ya en el allowlist
  de `SQLiteJsonRepository`) y `StateSnapshotRepository` (dedicado, PK
  compuesta `(device_id, capability)`), en `persistence/repositories.py`.
- `DiscoveryService.refresh()` persiste el inventario y estado actuales tras
  cada rediscovery exitosa (ya acotada a eventos de tipo inventario por
  P1.2/P1.3, no por cada `state_changed`), y **borra** de `devices`
  cualquier id que ya no esté en el registry — necesario para que un
  dispositivo reconciliado (Spec 024) no reaparezca tras un reinicio (bug
  real capturado por el propio test de esta feature: un simple upsert no
  bastaba).
- `build_runtime()` carga lo persistido ANTES de conectar cualquier
  adapter: `DeviceRegistry.load_persisted()` puebla solo `_devices` y
  `_source_entity_ids` (legible, pero sin rutas — `resolve_command_route`
  devuelve `route_not_found` hasta que una discovery real reconfirme el
  dispositivo esta sesión); `StateStore.load_persisted()` fuerza
  `status=STALE` en todo lo restaurado, sin importar qué se persistió.
- No se persiste telemetría histórica completa, solo el último valor por
  `(device_id, capability)` (mismo shape que la tabla existente). No hay
  migración nueva.

## Arranque degradado y reconexión (2026-08-18)

Cierra `specs/026-degraded-startup-reconnect/`: `build_runtime()` ya no
aborta el proceso si el adapter no conecta o la primera discovery falla.

- `build_runtime()` envuelve `connect()` + primer `discovery.refresh()` en
  un único `try/except (ConnectionError, OSError)`; si falla, audita
  `runtime_started_degraded` y sigue construyendo el resto de la
  composición — lo ya cargado desde persistencia (Spec 025) sigue legible.
- `RuntimeEventConsumer.run()` (el loop de reconexión real, lanzado por
  `run_stdio()`) ahora consulta `adapter.health()` en cada iteración; si no
  está conectado, llama `connect()` y luego `discovery.refresh()` antes de
  volver a `subscribe_events()` — nunca llama `connect()` de más mientras
  el adapter ya está sano (varios adapters no son seguros de reconectar dos
  veces sin motivo).
- El delay fijo de 1s pasa a backoff exponencial acotado (por defecto hasta
  60s), que se resetea a su valor inicial tras cada reconexión exitosa.
- `CompositeAdapter` no se modifica: su tolerancia a fallo parcial en
  `connect()`/`discover()` ya era correcta y se reutiliza tal cual.
- Sin cambios en cómo se rechaza un comando contra una fuente no conectada
  (`ConnectionError` → `ExecutionStatus.UNAVAILABLE` en `PlanExecutor`, ya
  existente).

## Corrección del optimizador energético (2026-08-18)

Cierra `specs/027-energy-optimizer-correctness/`: cuatro defectos
verificados en el solve path CP-SAT (`optimizer/cp_sat.py`).

- **Signo invertido en `maximize_solar_self_consumption`**: el coeficiente
  se aplicaba a `grid_export` con la polaridad equivocada — con la
  declaración esperada (`direction="maximize"`) el solver en realidad
  MAXIMIZABA la exportación en vez de minimizarla. Corregido con una
  negación adicional específica de este objetivo (comentario en el código
  explica por qué `grid_export` tiene polaridad inversa al nombre del
  objetivo). Probado con un escenario dorado (solar solo en un slot de
  dos) con única respuesta óptima posible.
- **Prioridad lexicográfica real**: `_optimize_energy` ya no combina todos
  los objetivos en una única suma ponderada. Ahora agrupa por `priority`,
  resuelve cada nivel en orden, y fija el valor alcanzado como cota antes
  de optimizar el siguiente nivel — un objetivo de mayor prioridad nunca
  se sacrifica por uno de menor prioridad. Objetivos con la misma
  `priority` se siguen combinando en un único nivel ponderado.
- **Soft constraints reales**: un constraint `hard=False` de un tipo
  soportado gana una variable de violación no-negativa, minimizada como el
  nivel de MÁS alta prioridad (antes que cualquier objetivo declarado por
  el usuario) — nunca bloquea la factibilidad. `constraint_summary["soft_violations"]`
  ya no es un `[]` hardcodeado: reporta `type`/`slot`/`amount` de cada
  violación real.
- **`EnergyContext.base_load_forecast`** (nuevo, aditivo, `None` por
  defecto = cero en todo el horizonte): consumo base de la casa,
  incorporado a la ecuación de balance y por tanto a todo constraint/coste
  que dependa de ella.
- **Diferido explícitamente, no implementado aquí**: constraint de SOC
  terminal de batería (evita que el optimizador vacíe la batería al final
  del horizonte por un ahorro marginal) — mismo audit original, epic
  separado del backlog.

Evidencia: `380 → 387 passed, 8 skipped`. Ruff y mypy limpios.

## Evidencia y reproducibilidad del solver (2026-08-18)

Cierra `specs/028-solver-evidence-reproducibility/`: `OptimizationResult`
ganó un campo aditivo `solver_evidence: SolverEvidence | None`
(`optimizer/ports.py`) — antes nada reportaba cómo se llegó a un resultado,
solo el resultado mismo.

- **`SolverEvidence`**: `solver_name` (`"cp-sat"`), `solver_version`
  (`ortools.__version__` real cargado en el proceso), `num_search_workers`/
  `random_seed` (leídos de `solver.parameters` tras configurarlo — hoy
  siempre `1`/`0`, sin cambios de comportamiento), `wall_time_seconds`
  (suma de `solver.WallTime()` de cada tier resuelto), `tiers: list[SolvedTier]`
  y `scenario_fingerprint`.
- **`SolvedTier`**: `priority` (la prioridad declarada del tier, o `None`
  para el tier implícito de violación de soft constraints, que no tiene
  prioridad de usuario), `terms` (nombres de objetivos, o tipos de
  constraint soft, contenidos en ese tier) y `achieved_value` (el valor
  entero crudo de CP-SAT que `_solve_tiers` ya usa para fijar la cota del
  tier — no reescalado a unidad física, ver `research.md` de la spec para
  el porqué).
- **`scenario_fingerprint`**: `sha256(scenario.model_dump_json())` del
  `OptimizationScenario` exacto resuelto — permite verificar
  criptográficamente que dos resultados vienen de la misma entrada antes de
  compararlos por reproducibilidad.
- `solver_evidence` es `None` para cualquier resultado que nunca completó un
  solve (escenario inválido, infactible o timeout) — nunca se fabrica
  evidencia de un cómputo que no ocurrió.
- Cambio puramente aditivo: `build_result(...)` recibe `solver_evidence`
  como keyword opcional (`None` por defecto), así que todo call site
  existente sigue funcionando sin cambios. El campo viaja automáticamente a
  través de MCP porque `OptimizationResult` ya se serializa entero.
- Probado con un test de reproducibilidad real: la misma escenario resuelta
  dos veces desde instancias de optimizador independientes produce `plan`,
  `objective_values`, `tiers` (con cada `achieved_value`) y
  `scenario_fingerprint` idénticos — `wall_time_seconds` queda
  explícitamente excluido de la comparación por ser reloj de pared legítimo.
- Dos tests de paridad MCP preexistentes (`test_ortools_mcp_parity.py`,
  `test_unified_mcp_compatibility.py`) comparaban resultados byte a byte
  entre dos clientes independientes y ya excluían `created_at`/
  `validated_at` por ser reloj de pared; se les añadió excluir también
  `solver_evidence.wall_time_seconds` por la misma razón — actualización
  legítima de un test que ya normalizaba campos de reloj de pared, no un
  fallo de diseño.
- **Diferido explícitamente, no implementado aquí** (mismo audit original,
  epic separado del backlog): persistir esta evidencia en un audit log
  durable (`AuditEventRepository`).

Evidencia: `387 → 394 passed, 8 skipped`. Ruff y mypy limpios.

## Conflictos de identidad cross-adapter auditados (2026-08-18)

Cierra `specs/029-cross-adapter-identity-conflicts/`. El enlace explícito
`canonical_id` entre adapters ya era correcto e intencional (documentado
arriba, en "Runtime multi-adapter": las claves estables `identifiers`/
`connections` solo mantienen identidad DENTRO de una fuente; `canonical_id`
explícito es el único mecanismo de enlace entre adapters — sin cambios en
esta spec). El hueco estaba en lo que pasaba DESPUÉS de un merge
cross-adapter exitoso:

- **`DeviceRegistry.diagnostics`** (`canonical_type_conflict`,
  `capability_metadata_conflict`, `source_entity_rejected`) se calculaba
  correctamente pero nadie lo leía nunca — invisible en la práctica y sin
  límite de crecimiento. Nuevo `DeviceRegistry.drain_diagnostics()`
  (pop-all) llamado por `DiscoveryService.refresh()` justo después de
  `apply_snapshot(...)`, auditando cada diagnóstico como
  `registry_identity_conflict` exactamente una vez por ciclo.
- **Conflicto de estado mismo-ciclo entre adapters**: `CompositeAdapter.discover()`
  concatena los `source_states` de todos los sub-adapters conectados;
  `DiscoveryService._record_states` los guardaba vía `StateStore.save()`
  solo por `(device_id, capability)` — si dos adapters fusionados por
  `canonical_id` reportaban la MISMA capability con valores distintos en el
  mismo ciclo, el segundo `save()` sobrescribía el primero en silencio, sin
  diagnóstico ni evento de auditoría, sin regla de desempate documentada —
  asimétrico con `resolve_command_route`, que ya detecta y bloquea esta
  misma situación (`ambiguous_route`) en el lado de escritura. Corregido:
  `_record_states` detecta, dentro de su propio bucle por ciclo, cuando más
  de una fuente distinta reporta un valor para el mismo
  `(device_id, capability)` y discrepan (misma igualdad `(value, status)`
  que `StateStore.save()` ya usa para decidir si versionar); audita
  `state_source_conflict` con ambas fuentes, ambos valores y cuál se
  retuvo, antes de dejar que el orden last-write-wins existente proceda sin
  cambios.
- Cambio puramente observacional: qué valor queda cacheado y qué ruta se
  ejecuta no cambian en absoluto — solo la visibilidad de los conflictos.
- **Hallazgo real durante la verificación**: el fixture compartido
  `tests/fixtures/multi_adapter.py` (reutilizado por specs 008+ desde hace
  tiempo) ya tenía un `canonical_type_conflict` latente e invisible dentro
  de un solo adapter — la entidad `light.main_brightness` deriva
  `semantic_type="sensor"` (no tiene capability "power") mientras
  `light.main_power` del mismo dispositivo compartido deriva `"light"` —
  quedó expuesto por primera vez al activar esta observabilidad; el test
  de "cero conflictos" de esta spec usa una combinación de fixture distinta
  para evitarlo, sin tocar el fixture compartido (fuera de alcance).
- Faltaba `tests/unit/application/__init__.py` (mismo gap de colección
  standalone que Spec 024 encontró en `tests/unit/runtime/`); añadido.

Evidencia: `394 → 401 passed, 8 skipped`. Ruff y mypy limpios.

## Preconditions aplicadas en ejecución (2026-08-18)

Cierra `specs/030-precondition-enforcement/`. `Precondition(device_id,
capability, expected)` y `Command.preconditions` ya estaban completamente
modelados desde antes, pero se leían en cero sitios de
`plan_service.py`/`executor.py` — TOCTOU real: un caller podía declarar
"solo ejecuta esto si battery.soc == 60" y no cambiaba absolutamente nada
sobre si/cuándo el comando se ejecutaba.

- `PlanExecutor.execute()` ahora evalúa, justo antes de llamar al adapter
  por cada comando (no al validar el plan, no una sola vez al principio —
  freshest read point posible, verificado leyendo la posición donde ya se
  capturaba `before_state`), cada `Precondition` contra el valor actual en
  `StateStore`. Satisfecha solo si existe un snapshot y su `value` iguala
  exactamente a `expected`; ausencia de estado se trata igual que
  desacuerdo (nunca se asume satisfecho).
- Si alguna precondition falla, el adapter NUNCA se llama para ese
  comando: se registra un `ExecutionOutcome` `REJECTED` con
  `error.code=precondition_failed`, `retryable=True` (el mundo puede
  cambiar y volver a satisfacerla) y todas las preconditions fallidas
  listadas — por el mismo camino (`outcome_repository`, audit) que
  cualquier otro outcome.
- Comandos dentro del mismo plan ven los efectos confirmados de comandos
  anteriores del mismo plan: verificado con un test de dos comandos donde
  el segundo depende del capability que el primero acaba de confirmar vía
  `_readback` (que ya guardaba en `StateStore` antes de esta spec).
- Un plan donde todos los comandos fallan sus preconditions completa sin
  excepción y sin ninguna llamada a adapter.
- Cambio puramente aditivo: comandos sin preconditions declaradas se
  comportan exactamente igual que antes (verificado, cero regresión en
  los tests preexistentes de `test_plan_lifecycle.py`).
- **Diferido explícitamente, no implementado aquí**: operadores de
  comparación/rango en preconditions (`battery.soc >= 50`) — solo
  igualdad exacta por ahora, epic separado.

Evidencia: `401 → 407 passed, 8 skipped`. Ruff y mypy limpios.

## Lifecycle de ejecución atómico y evidencia preservada (2026-08-18)

Cierra `specs/031-execution-lifecycle-atomicity/`. Dos huecos reales de
corrección de ejecución detectados por el segundo audit del usuario:

- **Claim no atómico**: `PlanExecutor.execute()` escribía `EXECUTING`
  sin comprobar antes el estado persistido del plan — una segunda
  llamada `execute()` (retry tras timeout, dos callers en carrera) podía
  reclamar y re-ejecutar un plan ya `EXECUTING` o ya terminal, reenviando
  todos los comandos a los adapters. Corregido: cuando hay
  `plan_repository` configurado, `execute()` lee el estado persistido
  ANTES de `assert_executable()`; si ya está `EXECUTING` o en cualquier
  estado terminal, se rechaza con `ErrorCode.INVALID_TRANSITION` sin
  llamar a ningún adapter. Verificado que un read-then-write sin `await`
  de I/O real entre medias ya es atómico para el modelo de concurrencia
  real de este runtime hoy (un proceso, una conexión SQLite compartida,
  `sqlite3` síncrono envuelto en `async def`) — locking multi-proceso
  real queda diferido a SQLite hardening (P2.6, aparte).
- **`execution_outcomes` destruía evidencia previa en un retry**:
  `ExecutionOutcomeRepository.save()` hacía `ON CONFLICT DO UPDATE` —
  un segundo save del mismo `(plan_id, command_id)` sobrescribía en
  silencio la evidencia del primer intento. Corregido de forma
  puramente aditiva: nueva tabla `execution_attempts` (INSERT-only,
  `attempt_id` autoincrement) que `save()` también escribe en cada
  llamada, sin tocar `execution_outcomes` ni ningún lector existente.
  Nuevo `list_attempts_for_plan(plan_id)` devuelve el historial completo
  en orden; `list_for_plan(plan_id)` sigue devolviendo solo el más
  reciente, sin cambios.
- Cambio de infraestructura mínimo necesario: `SQLiteDatabase.initialize()`
  pasó de ejecutar un único `SCHEMA_PATH` hardcodeado a ejecutar cada
  `*.sql` en `migrations/` en orden alfabético — sin tabla de versiones
  ni framework de migraciones (eso sigue siendo P2.2, diferido); solo lo
  mínimo para que pueda existir un segundo archivo de migración aditivo
  (`CREATE TABLE IF NOT EXISTS`).
- Cambio puramente aditivo: sin `plan_repository` configurado, o para la
  primera ejecución de un plan, el comportamiento es idéntico a antes.
- **Diferido explícitamente, no implementado aquí**: persistencia de
  idempotency keys por-adapter a través de reinicios (hoy solo en
  memoria en los 6 adapters — confirmado por grep) — el claim atómico ya
  cierra el disparador más probable en la práctica (una llamada
  `execute()` duplicada) antes de que llegue a un adapter una segunda
  vez; locking SQLite multi-proceso real (P2.6).

Evidencia: `407 → 413 passed, 8 skipped`. Ruff y mypy limpios.

## Scheduling temporal de planes (2026-08-18)

Cierra `specs/032-plan-scheduling/`, el tercer y último P0 restante del
segundo audit del usuario. CP-SAT elegía el slot correcto (Spec 027) pero
el Plan resultante no tenía semántica temporal — `execute_plan` ejecutaba
ya, no en el slot elegido.

- **`Plan.execute_at: datetime | None`** (aditivo). `PlanExecutor.execute()`
  gana un guard, el PRIMERO de todos (antes incluso del claim atómico de
  la Spec 031): si `execute_at` está en el futuro, rechaza con
  `ErrorCode.NOT_YET_DUE` sin tocar ningún adapter. Sin `execute_at`,
  comportamiento idéntico a siempre.
- **Splitting multi-slot real, no shortcut con pérdida de información**:
  cuando una optimización asigna loads a slots distintos, `_proposal_plan`
  los agrupa por su `execute_at` calculado y emite un `Plan` por cada
  tiempo distinto (cada uno internamente consistente — nunca mezcla
  comandos de tiempos distintos). Nuevo `OptimizationResult.plans: list[Plan]`;
  `.plan` se mantiene como el primero (más temprano) para compatibilidad
  total con todo caller existente de un único load.
- **`Scheduler` persistente** (`src/domoai/runtime/scheduler.py`, nueva
  tabla `scheduled_plans` INSERT/UPDATE por fila, no historial —
  distinto de `execution_attempts` de la Spec 031): mantiene planes
  aprobados esperando su hora, sobrevive a un reinicio (verificado con un
  test que reconstruye repository/scheduler contra el mismo fichero
  SQLite), y aplica una ventana de gracia acotada — un plan vencido más
  allá de la ventana se marca `missed` y se audita (`schedule_missed`),
  NUNCA se ejecuta tarde en silencio ("nunca ejecutar todo lo perdido
  automáticamente", advertencia explícita del audit). Un sweep completo
  usa un único `now` consistente para todas las filas.
- La ejecución real, cuando llega la hora, pasa siempre por el mismo
  `PlanExecutor.execute()` sin modificar — policy, aprobación, claim
  atómico (Spec 031), preconditions (Spec 030) e idempotencia se aplican
  exactamente igual que a una ejecución inmediata. El Scheduler nunca
  llama a un adapter directamente.
- Nuevas tools MCP: `schedule_plan`, `cancel_scheduled_plan`,
  `reschedule_plan`, `list_scheduled_plans` (mismo patrón de
  digest/aprobación que `execute_plan`). `DomoticsMcpContext.scheduler`
  es opcional (`None` en despliegues sin persistencia, p. ej. el fixture
  server). El loop de fondo (`Scheduler.run()`) sigue el mismo patrón ya
  establecido por `RuntimeEventConsumer.run()` (Spec 023/026), arrancado
  en `run_stdio()`.
- La frontera MCP parsea `execute_at` con una única función que exige una
  zona horaria explícita para `schedule_plan` y `reschedule_plan`. No se usa
  `model_copy(update=...)` como sustituto de validación: un timestamp naive
  se rechaza antes de persistir. La hora de ejecución forma parte de un
  `ExecutionWindow` canónico y entra en los digests de definición,
  validación y aprobación. El `reschedule_plan` genérico es fail-closed:
  conserva la fila pendiente y exige una nueva intención validada/aprobada;
  un miembro de bundle exige una revisión de bundle.
- **Fallout real detectado y corregido**: tres tests preexistentes
  hardcodeaban la lista exacta de tools MCP (`test_domotics_mcp_contract.py`,
  `test_unified_mcp_contract.py`, `test_home_assistant_provider_runtime.py`)
  — actualizados con las 4 tools nuevas. Dos tests de paridad MCP
  (`test_ortools_mcp_parity.py`, `test_unified_mcp_compatibility.py`) ya
  normalizaban `created_at`/`wall_time_seconds` antes de comparar dos
  clientes byte a byte; se les añadió normalizar también
  `plans[].created_at`/`plans[].validation.validated_at` por la misma
  razón (reloj de pared).
- **Deuda técnica cerrada de paso**: `schemas/v1/` llevaba desde las
  Specs 027/028 sin regenerar (`base_load_forecast`, `solver_evidence`
  nunca se habían reflejado); `scripts/export_schemas.py` ejecutado,
  diff puramente aditivo (328 líneas, 5 archivos), sin romper nada.
- **Diferido explícitamente, no implementado aquí** (backlog P3.1,
  "scheduler avanzado"): horarios recurrentes, política DST más allá de
  usar timestamps timezone-aware, scheduling más fino que por-Plan
  (per-Command), y conectar el state machine de la skill energética
  portable al nuevo camino de scheduling (sigue usando `execute_plan`
  inmediato hoy; el scheduling lo inicia el caller/host que elija usar
  las tools nuevas).

Evidencia: `413 → 433 passed, 8 skipped`. Ruff y mypy limpios.

## Pipeline de CI en GitHub (2026-08-18)

Cierra `specs/033-ci-pipeline/`, el primer ítem de P2 (production
hardening). No existía `.github/workflows/` en absoluto — todos los
gates de calidad (ruff, mypy strict, suite completa, `uv.lock`) se
verificaban solo a mano, cada vez, por el propio agente.

- Nuevo `.github/workflows/ci.yml`, disparado en `pull_request` y
  `push` a `main`, con 9 jobs INDEPENDIENTES (sin `needs:` entre ellos,
  para que cada categoría de fallo sea atribuible por separado en la
  lista de checks del PR): `lock-check` (`uv lock --check`), `lint`
  (`ruff check .`), `typecheck` (`mypy src`), cuatro jobs de test que
  reutilizan el split ya existente `tests/{unit,contract,integration,performance}`
  (sin inventar una categorización nueva), `package` (`uv build` +
  import de los dos entrypoints `domoai-mcp`/`domoai-lab`), y
  `schema-check` (`export_schemas.py` + `git diff --exit-code
  schemas/v1/` — automatiza literalmente la misma secuencia manual que
  esta sesión usó para detectar y corregir el drift de las Specs
  027/028 en la Spec 032).
- Reporte de cobertura (`pytest-cov`, ya instalado) en el job
  `test-unit`, subido como artifact — sin umbral numérico obligatorio en
  este incremento: un umbral es una decisión de política que le
  corresponde al dueño del repo, no algo que inventar en silencio.
- Verificado localmente que la lógica de `schema-check` (regenerar +
  diff) es idempotente: dos ejecuciones consecutivas de
  `export_schemas.py` producen cero diferencias entre sí — el diff
  contra `HEAD` que se observa ahora mismo es solo ruido de que esta
  sesión entera sigue sin ningún commit (`30a43bc` sigue siendo HEAD),
  no un fallo del mecanismo.
- **Incidente real durante la verificación, corregido en el momento**:
  al simular deliberadamente una violación de ruff para probar el
  aislamiento de checks, se usó `git checkout -- src/domoai/domain/errors.py`
  para revertir — pero como NINGÚN archivo de esta sesión (021-032)
  estaba comprometido a git todavía, ese comando borró trabajo real no
  comprometido (los `ErrorCode.PRECONDITION_FAILED`/`NOT_YET_DUE` de las
  Specs 030/031). Detectado inmediatamente por mypy (`ErrorCode` sin
  esos atributos) y corregido restaurando las dos líneas exactas antes
  de continuar; verificado con la suite completa (`433 passed, 8
  skipped`) que no quedó ningún daño. Lección aplicada de inmediato para
  el resto de la verificación: nunca más `git checkout --` sobre
  archivos con cambios no comprometidos de la sesión; usar `Edit` para
  simular y deshacer roturas deliberadas.
- **Diferido explícitamente, no implementado aquí** (fuera del alcance
  de un cambio de código): configurar branch protection en GitHub para
  exigir estos checks antes de mergear — es un ajuste administrativo del
  repositorio que solo un admin puede aplicar desde la UI/API de
  GitHub, no algo que un workflow YAML pueda activar por sí mismo.
  Escaneo de vulnerabilidades/secretos y SBOM (P2.9, aparte). Umbral
  numérico de cobertura (decisión de política pendiente).

Evidencia: `uv lock --check`, `ruff check .`, `mypy src` y las cuatro
suites de test verificadas localmente comando por comando, coincidiendo
exactamente con cada job — `433 passed, 8 skipped` total, sin cambios de
comportamiento (esta spec no modifica ningún archivo fuente, solo añade
`.github/workflows/ci.yml`).

## Tracking real de migraciones de esquema (2026-08-18)

Cierra `specs/034-schema-migration-tracking/`, segundo ítem de P2.
`SQLiteDatabase.initialize()` ejecutaba TODOS los ficheros de migración
en CADA arranque, sin ningún ledger de qué ya se había aplicado —
funcionaba hoy solo por accidente, porque las tres migraciones
existentes eran `CREATE TABLE IF NOT EXISTS` (tolera re-ejecución). El
primer `ALTER TABLE`/backfill real habría roto en el segundo arranque
tras añadirse.

- Nueva tabla `schema_migrations (filename TEXT PRIMARY KEY, applied_at
  TEXT NOT NULL)`, añadida a `001_initial.sql` (única ubicación posible
  sin problema del huevo-y-la-gallina: tiene que existir antes de poder
  consultarse a sí misma).
- `SQLiteDatabase.initialize()`: bootstrap incondicional e idempotente
  de `schema_migrations`, lee qué ficheros ya están aplicados, y solo
  ejecuta + registra los que faltan, en orden, uno a uno (commit tras
  cada fichero, no una transacción gigante) — una migración se aplica
  exactamente una vez, para siempre, por fichero de base de datos.
- Nuevo parámetro `migrations_dir` opcional (por defecto el directorio
  real) — permite testear la lógica real contra un directorio temporal
  sin fabricar un cambio de esquema falso en producción.
- Probado con datos reales: una base con solo `001_initial.sql`
  registrado (simulando un despliegue antiguo), con un `Plan` insertado
  vía `PlanRepository`, reabierta con el set de migraciones completo —
  el `Plan` original sigue íntegro Y las tablas de migraciones
  posteriores (`scheduled_plans`) ya son usables.
- Probado con una sentencia genuinamente no-idempotente: directorio de
  migraciones temporal con un `ALTER TABLE ... ADD COLUMN` real —
  inicializar la misma base dos veces seguidas no falla ni duplica la
  columna. Este es el caso concreto que el mecanismo anterior no podía
  sobrevivir.
- Sin renumerar `002_execution_attempts.sql`/`004_scheduled_plans.sql`
  (el hueco en 003 se mantiene — renumerar migraciones ya "aplicadas en
  la práctica" sería exactamente el tipo de drift sin versionar que
  esta spec existe para evitar).
- **Diferido explícitamente, no implementado aquí**: mecanismo de
  rollback/down-migration (sin necesidad expresada para el modelo
  local-first de un solo desarrollador); documento de política de
  compatibilidad semver para `schemas/v1/`; librería de migraciones
  externa (sigue siendo `sqlite3` stdlib puro).

Evidencia: `433 → 438 passed, 8 skipped`. Ruff y mypy limpios.

Las respuestas estructuradas incluyen `schema_version` y, cuando procede,
`runtime_revision`. Las operaciones de mutación pasan por la frontera de plan
y política; no existe una tool MCP por fabricante.

## TLS en el transporte MQTT (2026-08-18)

Cierra `specs/035-mqtt-tls/`, tercer ítem de P2. `AiomqttTransport`
(`src/domoai/adapters/zigbee2mqtt/transport.py`) construía su
`aiomqtt.Client` sin ningún parámetro TLS — toda conexión MQTT era
en claro, credenciales incluidas. `runtime_factory.py` rechazaba
`mqtts://` explícitamente con un `ValueError`. La librería `aiomqtt`
2.5.1 ya exponía `tls_context`/`tls_params`/`tls_insecure` en su
`Client.__init__` — solo faltaba la conexión desde el lado de DomoAI.

- `AiomqttTransport` gana `tls`, `ca_cert_path`, `client_cert_path`,
  `client_key_path`, `tls_insecure` — `connect()` construye un
  `ssl.SSLContext` vía `_build_tls_context()` solo cuando `tls=True`
  (CA propia opcional → certificado de cliente opcional para mTLS →
  override inseguro aplicado al final) y lo pasa como `tls_context=` a
  `aiomqtt.Client`. Con `tls=False` (por defecto) el comportamiento
  es idéntico al anterior.
- `Settings` gana `mqtt_ca_cert_path`, `mqtt_client_cert_path`,
  `mqtt_client_key_path`, `mqtt_tls_insecure` (nuevas env vars
  `DOMOAI_MQTT_CA_CERT_PATH`, `DOMOAI_MQTT_CLIENT_CERT_PATH`,
  `DOMOAI_MQTT_CLIENT_KEY_PATH`, `DOMOAI_MQTT_TLS_INSECURE`) —
  certificado de cliente y clave deben configurarse juntos, mismo
  patrón de validación por tupla que ya existía para KNX/Modbus y para
  `mqtt_password`/`mqtt_username`.
- `runtime_factory.py`: la rama `mqtts://` ya no lanza — puerto por
  defecto 8883 (distinto del 1883 de `mqtt://`), TLS habilitado, los
  cuatro settings nuevos se pasan al transporte.
- `mqtt_tls_insecure` es **opt-in, por defecto `False`** — desactivarlo
  deshabilita verificación de certificado y de hostname
  (`check_hostname=False`, `verify_mode=ssl.CERT_NONE`); probado que el
  valor por defecto deja la verificación activa y que el flag, cuando
  se activa, gana incluso si hay una CA propia configurada (combinación
  válida para laboratorios locales, no un error).
- **Fuera de alcance, documentado en el spec**: TLS para Modbus TCP y
  KNX/IP (sin variante TLS de uso común / KNX Data Secure es un
  mecanismo distinto, no TLS); Home Assistant y Matter Server ya llevan
  TLS a nivel de esquema de URL (`https://`/`wss://`) vía sus propias
  librerías cliente, sin hueco equivalente que cerrar; gestión de ciclo
  de vida de certificados (rotación/renovación).

Evidencia: `438 → 451 passed, 8 skipped`. Ruff y mypy limpios.

## Consulta del audit trail (2026-08-18)

Cierra `specs/036-audit-trail-query/`, cuarto ítem de P2. DomoAI ya
registraba un audit trail rico, persistente y con secretos redactados
(`AuditEventRepository.append_event()`, sink de `AuditLog` para cada
servicio del runtime) — pero `list_all()`, el único método de lectura
existente, no tenía ningún caller fuera de su propia clase (`grep -rn
"list_all" src/domoai` restringido a audit no encontró ningún sitio de
llamada real). El audit trail era de solo escritura en la práctica:
nada exponía "qué pasó" sin abrir el fichero SQLite directamente con un
cliente SQL crudo.

- Nuevo `AuditEventRepository.list_events(*, event_type=None,
  subject_id=None, since=None, limit=100)` — filtrado y acotado en la
  capa SQL (`WHERE`/`LIMIT`, no fetch-y-descarta en Python), orden más
  reciente primero (`ORDER BY created_at DESC, id DESC`), límite
  siempre acotado a `min(limit, 500)` sin importar lo que pida el
  caller. `list_all()` queda intacto (lo siguen usando cuatro tests de
  integración existentes como lector de historial completo).
- Nueva tool MCP de solo lectura `list_audit_events`
  (`readOnlyHint=True, destructiveHint=False`, mismo patrón que
  `discover_devices`/`get_state`) con parámetros opcionales
  `event_type`, `subject_id`, `since`, `limit`.
- `since`, cuando se proporciona, exige una zona horaria explícita. El filtro
  y el orden comparan instantes absolutos, no el orden lexicográfico de ISO;
  offsets equivalentes no pueden cambiar qué eventos devuelve la consulta.
- `DomoticsMcpContext` gana `audit_repository: AuditEventRepository |
  None = None` (mismo patrón que `energy_context_provider`/`scheduler`)
  — `None` en el path fixture en memoria sin persistencia, poblado
  desde `runtime.audit_repository` en `build_configured_server()`. Sin
  repositorio configurado, la tool devuelve un `error_envelope`
  claramente distinto de una respuesta vacía, nunca un crash.
- Probado que el filtrado por `event_type`/`subject_id` (individual y
  combinado) realmente reduce resultados, que `since` excluye eventos
  en o antes del timestamp dado, que el límite por defecto es 100 y el
  tope duro de 500 se respeta incluso pidiendo un límite absurdamente
  alto, y el path `None` de la tool MCP.
- **Fuera de alcance, con justificación explícita**: logging estructurado
  a stdout/stderr (segundo hueco real, verificado — cero usos de
  `import logging` en `src/domoai` — pero es una preocupación distinta,
  telemetría en vivo del proceso, no historial retrospectivo
  consultable); métricas/tracing (Prometheus/OpenTelemetry, sin
  requisito nombrado); política de retención/poda del audit trail (real
  pero ortogonal a "poder leer lo que ya existe", sin problema
  observado todavía en el modelo local-first de un solo desarrollador).

Evidencia: `451 → 462 passed, 8 skipped`. Ruff y mypy limpios.

## Backpressure de eventos en CompositeAdapter (2026-08-19)

Cierra `specs/037-composite-adapter-backpressure/`, quinto ítem de P2.
`CompositeAdapter._event_stream()` (`src/domoai/runtime/composite_adapter.py`)
fusiona los streams de eventos de todos los adapters conectados en una
única `asyncio.Queue()` compartida construida SIN `maxsize` — un event
storm de cualquier fuente conectada (un mesh Zigbee reconectando, un
broker MQTT reproduciendo un backlog grande de mensajes retained, un
dispositivo mal comportado) hacía crecer la cola sin límite, consumiendo
memoria indefinidamente. Cero cobertura de test existía sobre
`_event_stream()`/`subscribe_events()` de `CompositeAdapter` bajo
cualquier condición de carga.

- Nuevo parámetro `CompositeAdapter(..., event_queue_max_size=1000)` —
  se convierte en el `maxsize` de la cola interna (antes ilimitada).
  Nuevo `Settings.composite_event_queue_max_size` (env var
  `DOMOAI_COMPOSITE_EVENT_QUEUE_MAX_SIZE`), enhebrado en el único punto
  de construcción de `CompositeAdapter` en `create_adapter()`.
- Cada tarea `pump(adapter)` usa `queue.put_nowait(...)` en vez de
  `await queue.put(...)`; en `asyncio.QueueFull` el evento se
  DESCARTA (nunca se bloquea, nunca se reintenta) y el drop se registra
  en la lista `self._diagnostics` ya existente, con la misma forma que
  `_record_failure()` (`event_type`, `adapter_id`, `message`).
  Deliberadamente NO se emite un nuevo `SourceEvent` por cada drop —
  `RuntimeEventConsumer._apply_event()` ya trata cualquier kind
  distinto de `state_changed` como disparador de un `discovery.refresh()`
  completo, así que emitir un diagnóstico por cada drop durante un storm
  crearía un bucle de retroalimentación real (más diagnósticos → más
  refreshes caros → drenado más lento → más drops).
- El drop es no-bloqueante y por-`put` individual (no una cola aparte
  por adapter): un adapter que inunda la cola compartida absorbe la
  mayoría de sus propios drops por simple probabilidad (intenta muchos
  más `put`s que uno bien comportado), sin necesitar sub-colas
  aisladas ni scheduling de fairness — probado explícitamente que un
  segundo adapter bien comportado NO queda completamente silenciado
  durante el flood de otro (al menos algunos de sus eventos llegan),
  sin reclamar fairness perfecto.
- Probado: tráfico normal por debajo del umbral se entrega íntegro, sin
  drops, sin cambio de comportamiento; un burst mayor al umbral
  configurado resulta en como mucho `event_queue_max_size` eventos
  entregados de ese burst, el resto descartado y registrado
  individualmente por `adapter_id`; drops de dos adapters simultáneos
  se registran distinguibles entre sí; ningún `SourceEvent` con
  `kind="adapter_diagnostic"` se emite como consecuencia de un drop; el
  umbral es honrado end-to-end desde `Settings` hasta
  `create_adapter()`.
- **Fuera de alcance, con justificación explícita en el spec**:
  sub-colas por-adapter o fairness scheduling ponderado (diseño
  materialmente mayor al que exige el hueco verificado — crecimiento
  de memoria sin límite); conectar `CompositeAdapter.diagnostics` a una
  superficie de lectura (MCP tool, audit log) — hueco real,
  pre-existente, de la misma forma que el que cerró la Spec 036 para el
  audit trail, pero pertenece naturalmente a P2.7 (health model
  unificado), no a este fix; backpressure en el path de un solo adapter
  (sin `CompositeAdapter` de por medio) — ya tiene backpressure natural
  vía `await` directo, sin cola intermedia que desbordar; rate-limiting
  o desconexión automática de una fuente persistentemente ruidosa —
  decisión de política más grande, no requerida para cerrar el hueco
  concreto de esta spec.

Evidencia: `462 → 470 passed, 8 skipped`. Ruff y mypy limpios.

## SQLite hardening (2026-08-19)

Cierra `specs/038-sqlite-hardening/`, sexto ítem de P2.
`SQLiteDatabase.initialize()` conectaba sin ningún `PRAGMA` — sin WAL,
sin `busy_timeout` explícito. `ExecutionOutcomeRepository.save()`
(única escritura multi-statement de todo `repositories.py` — el resto
hace un `execute()` por `commit()`, atómico por construcción) nunca
llamaba a `rollback()` si el segundo `execute()` fallaba, dejando la
conexión compartida y de larga vida con una escritura a medias sin
confirmar.

- `SQLiteDatabase.initialize()` ejecuta `PRAGMA journal_mode=WAL` y
  `PRAGMA busy_timeout={ms}` justo tras conectar, antes de las
  migraciones. Nuevo `SQLiteDatabase(path, busy_timeout_ms=5000)` —
  default explícito que preserva el comportamiento de facto actual
  (Python ya defaulteaba `timeout=5.0s` en `sqlite3.connect()`, pero
  como efecto secundario no documentado, no como valor deliberado).
  Nuevo `Settings.sqlite_busy_timeout_ms` (env var
  `DOMOAI_SQLITE_BUSY_TIMEOUT_MS`), enhebrado en el único punto de
  construcción de `SQLiteDatabase` en `build_runtime()`.
- `ExecutionOutcomeRepository.save()`: el segundo `execute()`
  (`execution_attempts`) envuelto en `try/except sqlite3.Error:
  rollback(); raise` — deshace también el primer insert
  (`execution_outcomes`), re-lanza la excepción original sin cambiar
  su tipo, deja la conexión limpia para la siguiente operación.
- Probado: `PRAGMA journal_mode` reporta `"wal"` tras `initialize()`;
  `PRAGMA busy_timeout` reporta el default (5000) y un valor custom
  configurado; el fallo del segundo insert deja CERO filas de ambas
  tablas (no solo la segunda); la excepción sigue siendo
  `sqlite3.IntegrityError`; una `save()` posterior no relacionada
  funciona con normalidad tras el fallo; el caso exitoso inserta ambas
  filas igual que antes.
- Técnica de test: `sqlite3.Connection` es un tipo C inmutable —
  `monkeypatch.setattr(sqlite3.Connection, "execute", ...)` falla con
  `TypeError: cannot set 'execute' attribute of immutable type`.
  Los tests intercambian `SQLiteDatabase._connection` por un proxy
  delegante pequeño que falla solo el `execute()` dirigido a
  `execution_attempts`.
- **Fuera de alcance, con justificación explícita**: convertir otros
  métodos de repositorio a transacciones explícitas (todos los demás
  ya son atómicos por construcción, un solo `execute()` por
  `commit()`); un context manager genérico `with database.transaction():`
  (infraestructura especulativa para una segunda escritura
  multi-statement que no existe todavía); benchmarking de WAL o
  stress-testing multi-proceso (fuera de escala local-first); cambiar
  la arquitectura de conexión única de larga vida (cambio
  arquitectónico mayor no requerido por los huecos concretos
  verificados).

Evidencia: `470 → 479 passed, 8 skipped`. Ruff y mypy limpios.

## Health model unificado por-adapter (2026-08-19)

Cierra `specs/039-unified-adapter-health/`, séptimo ítem de P2, y el
límite documentado y diferido explícitamente en la Spec 026 ("Límite
documentado: `CompositeAdapter.health()` reporta conectado con
cualquier sub-adapter vivo, así que una caída parcial del composite no
dispara este reconnect — health model unificado por-adapter es P2.7,
aparte"). `CompositeAdapter.health()` colapsaba el health individual de
cada sub-adapter conectado en un único booleano agregado `connected =
any(...)` — verdadero si AL MENOS UN sub-adapter está vivo, sin
importar cuántos otros estén caídos. Dos consecuencias reales
verificadas: (1) sin visibilidad de CUÁL adapter concreto está caído
en un despliegue multi-adapter (cero referencias a `AdapterHealth` bajo
`src/domoai/mcp`); (2) un hueco funcional real —
`RuntimeEventConsumer.run()` solo intenta `connect()` cuando
`health.connected` es falso; con la semántica "al menos uno vivo", un
despliegue con 1 de N adapters caído reporta `connected=True` para
siempre mientras quede otro vivo, así que el bucle de reconexión nunca
se dispara para ese adapter muerto — se queda desconectado en silencio
y para siempre hasta que todos los demás también mueran o el proceso
se reinicie.

- `AdapterHealth` (`src/domoai/domain/models.py`) gana
  `components: list[AdapterHealth] | None = None` — aditivo, `None`
  para cada implementación `health()` de un solo adapter (ninguna de
  las seis cambia). `CompositeAdapter.health()` ya calculaba el
  `AdapterHealth` de cada sub-adapter vía `asyncio.gather()` y lo
  descartaba tras reducirlo a dos escalares — ahora incluye esa lista
  ya calculada como `components`, sin gather adicional, sin I/O nuevo.
- `connected`/`message` de `CompositeAdapter.health()` mantienen su
  cálculo y significado agregado exactos de hoy ("al menos uno vivo")
  — sin redefinir semántica existente, deliberadamente, para no
  ambigüar el otro lector existente de `.connected`
  (`adapters/sdk/conformance.py`).
- `RuntimeEventConsumer.run()`: condición de reconexión extendida de
  `if not health.connected` a `degraded = not health.connected or
  (health.components is not None and any(not c.connected for c in
  health.components)); if degraded:`. Para un adapter único,
  `components` siempre es `None`, así que la cláusula añadida es
  siempre falsa y la condición se reduce exactamente a la de hoy —
  cambio de comportamiento cero para el caso no-composite, por
  construcción, no solo por test. Reconectar sigue significando
  `self.adapter.connect()`, que para `CompositeAdapter` ya reintenta
  TODOS los sub-adapters vía su `asyncio.gather()` existente, sin
  cambios — mismo mecanismo ya aceptado por el diseño de reconexión de
  la Spec 026 para el caso de fallo total.
- Probado: con todos los sub-adapters sanos, `components` los lista
  todos conectados; con uno caído de varios, `connected` agregado
  sigue en `True` (semántica sin cambios) mientras `components`
  identifica el `adapter_id` concreto caído; con dos caídos
  simultáneamente, ambos identificables individualmente, no fusionados
  en una sola señal; `RuntimeEventConsumer.run()` ahora SÍ llama a
  `connect()` ante un fallo parcial (la regresión exacta nombrada en
  la nota de la Spec 026); NO llama a `connect()` cuando todos los
  componentes están sanos (sin reconexión espuria); los doce tests
  preexistentes de `test_runtime_event_consumer.py` (añadidos en la
  Spec 026 y desde entonces) siguen pasando sin modificar — prueba de
  regresión cero para el caso de un solo adapter.
- **Fuera de alcance, con justificación explícita en el spec**:
  exponer el health por-adapter vía una tool MCP (mismo hueco real que
  la Spec 036 cerró para el audit trail, pero pertenece a una spec
  separada, no bundleada aquí); redefinir qué significa `connected` en
  el composite (p.ej. a "todos vivos") — rechazado explícitamente para
  no ambigüar el otro lector existente; reconexión selectiva por-adapter
  (reintentar solo el sub-adapter concreto caído en vez de todo el
  composite) — `connect()` del composite ya reintenta todos
  incondicionalmente, comportamiento existente ya aceptado por el
  diseño de la Spec 026; una abstracción de "modelo de salud unificado"
  más amplia (historial, métricas, alerting) — sin requisito nombrado
  por ninguna auditoría.

Evidencia: `479 → 484 passed, 8 skipped`. Ruff y mypy limpios.

## Failure injection sistemático (2026-08-19)

Cierra `specs/040-systematic-failure-injection/`, octavo ítem de P2.
`PlanExecutor.execute()` (`src/domoai/runtime/executor.py`) era el
ÚNICO sitio en todo el código que capturaba `ConnectionError` a secas
— verificado con `grep -rn "except (ConnectionError" src/domoai`
(cada adapter, `composite_adapter.py`, `event_consumer.py`,
`runtime_factory.py` capturan consistentemente la tupla
`(ConnectionError, OSError, TimeoutError)`) contra `grep -rn "except
ConnectionError" src/domoai` (solo dos sitios, ambos en
`executor.py`). Bug real y severo, no solo inconsistencia de estilo:
`PlanExecutor.execute()` persiste `status=EXECUTING` ANTES del bucle
de comandos y solo persiste un status terminal DESPUÉS de que el
bucle completo termine sin excepción sin capturar. Un `OSError` o
`TimeoutError` desde un adapter (ambos, fallos reales que CADA
implementación de adapter real ya lanza desde su propia capa de
transporte) durante `execute()` o el `read_state()` del readback no
era capturado — se propagaba sin capturar, saltándose tanto el
registro del outcome del comando como el guardado final de status
terminal. Combinado con el guard atómico de reclamación de la Spec
031 (`EXECUTING` está en `_NON_CLAIMABLE_STATUSES`), un solo
`OSError`/`TimeoutError` dejaba ese plan atascado en `EXECUTING` para
siempre — sin outcome registrado, sin evento de auditoría
`plan_execution_completed`, con una excepción sin capturar propagando
hacia quien haya llamado a `execute()`. Confirmado como no testeado en
absoluto: `grep -rn "TimeoutError\|OSError"
tests/integration/test_plan_lifecycle.py` devolvía cero resultados —
el hueco era invisible porque cada test de failure-injection existente
usaba `ConnectionError` específicamente para simular "adapter caído",
nunca los otros dos tipos que los adapters reales realmente lanzan.

- Ambos sitios `except ConnectionError` en `executor.py` ampliados a
  `except (ConnectionError, OSError, TimeoutError)` — sin ningún otro
  cambio de lógica, solo alinea con el patrón ya establecido en todo
  el resto del código.
- Nuevo `FailureInjectingAdapter`
  (`tests/fixtures/failure_injection.py`) — implementación completa de
  `AdapterPort` (los siete métodos), construible con
  `fail: dict[str, BaseException]` para inyectar cualquier excepción
  en cualquier método concreto; cada método no configurado tiene éxito
  trivial por defecto. Reemplaza la necesidad de escribir una clase de
  adapter-que-falla ad-hoc por cada test nuevo (patrón disperso
  existente en `test_multi_adapter_runtime.py`,
  `test_runtime_event_consumer.py`, `test_composite_adapter_health.py`).
- Corrección real durante la implementación, documentada
  explícitamente en `tasks.md`: la garantía original del spec
  ("el plan queda reintentable") era imprecisa —
  `PlanStatus.UNKNOWN` YA estaba deliberadamente en
  `_NON_CLAIMABLE_STATUSES` desde la Spec 031 (un outcome incierto no
  debe reintentarse automáticamente, para no arriesgar una doble
  actuación sobre un dispositivo) — así que ni siquiera `ConnectionError`
  permite reintento automático por el mismo `plan_id` hoy. La garantía
  real y precisa que este fix entrega: `OSError`/`TimeoutError` ahora
  alcanzan el MISMO estado terminal bien definido (`UNKNOWN`, con
  outcome registrado y evento de auditoría) que `ConnectionError` ya
  alcanza — no un estado nuevo de "atascado en EXECUTING para siempre
  sin ningún outcome registrado y una excepción sin capturar".
- Probado: `OSError`/`TimeoutError` desde `execute()` y desde el
  `read_state()` del readback ya no dejan el plan atascado en
  `EXECUTING`; el outcome se registra correctamente
  (`UNAVAILABLE`/`UNKNOWN` según el punto de fallo, código
  `adapter_unavailable`); `ConnectionError` produce exactamente el
  mismo estado terminal `PlanStatus.UNKNOWN` (test de regresión); el
  fixture solo falla el método configurado, no todos. Verificación
  empírica adicional: se revirtió temporalmente el fix, se confirmó
  que exactamente los 4 tests dirigidos fallaban con la excepción sin
  capturar, y se restauró el fix confirmando que los 24 tests del
  fichero pasan.
- **Fuera de alcance, con justificación explícita en el spec**:
  migrar las clases de adapter-que-falla ad-hoc existentes al nuevo
  fixture compartido (limpieza futura válida, no requerida para
  cerrar el bug verificado); añadir manejo de `OSError`/`TimeoutError`
  en `adapters/sdk/conformance.py` (valida el contrato de manejo de
  errores del AUTOR del adapter, preocupación distinta al boundary de
  `PlanExecutor` que esta spec cierra); inyección de fallos
  aleatoria/chaos-engineering, simulación de fallos a nivel de red —
  sin requisito nombrado por ninguna auditoría.

Evidencia: `484 → 490 passed, 8 skipped`. Ruff y mypy limpios.

## Supply-chain security en CI (2026-08-19)

Cierra `specs/041-supply-chain-security/`, noveno y último ítem de P2.
Las tres GitHub Actions que usa `.github/workflows/ci.yml` en sus 9
jobs (`actions/checkout@v4`, `astral-sh/setup-uv@v3`,
`actions/upload-artifact@v4`) estaban fijadas a un tag de versión
mutable, no a un commit inmutable — verificado con `gh api
repos/actions/checkout/git/ref/tags/v4` etc. Un tag mutable es un
vector de ataque de supply-chain real y bien documentado (la
recomendación #1 de la propia guía de hardening de GitHub Actions y
el check "Pinned-Dependencies" de OpenSSF Scorecard): si el
repositorio de una de esas actions fuera comprometido, un commit
malicioso publicado bajo el tag existente se descargaría en silencio
en cada run futuro de CI, sin ningún cambio de código en DomoAI que
revisar. Segundo hueco verificado: no existía `.github/dependabot.yml`
en ningún sitio del repo (`command find .github -type f` solo
encontraba `ci.yml`), así que ni los SHAs fijados (una vez fijados) ni
las dependencias Python de `uv.lock` recibían nunca propuestas de
actualización automatizadas. Tercer hueco verificado: ningún job de CI
ni mecanismo alguno escaneaba el conjunto de dependencias bloqueado
contra vulnerabilidades conocidas — `uv run --with pip-audit pip-audit --local` ejecutado
contra el entorno real sincronizado confirmó cero vulnerabilidades
conocidas hoy (confirmado, no asumido) — esta spec añade el mecanismo
de detección que faltaba, no corrige una vulnerabilidad ya presente.

- Las tres actions fijadas a su commit SHA exacto y ya resuelto,
  obtenido directamente vía `gh api` contra los repositorios reales
  (`actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4`,
  `astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39 # v3`,
  `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4`)
  — con comentario de versión legible preservado. Detalle no trivial:
  el tag `v3` de `astral-sh/setup-uv` es un tag anotado (`object.type
  == "tag"`, no `"commit"`), así que su `object.sha` es el SHA del
  propio objeto tag, no del commit — dereferenciado correctamente vía
  `gh api repos/astral-sh/setup-uv/git/tags/<sha-del-tag>` para
  obtener el commit real. Aplicado en las 9 ocurrencias de
  `checkout`/`setup-uv` (una por job) y la única ocurrencia de
  `upload-artifact` (solo en `test-unit`); cero cambio de
  comportamiento en ningún job — verificado con diff completo del
  fichero, solo cambian las líneas `uses:` y se añade un job nuevo.
- Nuevo `.github/dependabot.yml`: dos entradas `updates`, ecosistemas
  `github-actions` y `pip`, cadencia semanal, sin auto-merge (las
  propuestas quedan para revisión humana, comportamiento por defecto
  de GitHub sin configuración adicional).
- Nuevo job `dependency-audit` en `ci.yml`: mismo patrón de setup que
  cada otro job (`uv sync`), ejecuta `uv run --with pip-audit pip-audit --local` contra el
  entorno sincronizado — falla el job si encuentra cualquier
  vulnerabilidad conocida. `pip-audit` invocado vía `uvx` (no añadido
  a `pyproject.toml`) — herramienta de solo-CI, no dependencia del
  proyecto en tiempo de ejecución ni desarrollo.
- Probado: grep confirma cero referencias `@v4`/`@v3` mutables
  restantes en todo el fichero; el fichero sigue siendo YAML válido;
  `dependabot.yml` es YAML válido con ambos ecosistemas presentes;
  `uv sync && uv run --with pip-audit pip-audit --local` ejecutado de verdad contra el
  entorno real sincronizado de este repositorio, reportando su
  resultado real actual ("No known vulnerabilities found", 57
  paquetes verificados). `uv.lock` ya pinaba versiones exactas con
  hash para cada dependencia (277 entradas `hash = ` confirmadas) —
  correcto ya de antes, sin cambios necesarios ahí.
- **Fuera de alcance, con justificación explícita en el spec**:
  generación de SBOM (práctica adyacente real pero sin hueco nombrado
  por ninguna auditoría); firma/attestation de artefactos de build
  (Sigstore/cosign — DomoAI no publica artefactos firmados en ningún
  registro hoy, nada que firmar todavía); pinning de dependencias
  transitivas más allá de lo que `uv.lock` ya hace (ya correcto);
  auto-merge de PRs de Dependabot (decisión de política del dueño del
  repo, no un cambio de código unilateral); escaneo de vulnerabilidades
  en ecosistemas no-Python (el repo no tiene ninguno hoy).

Evidencia: `490 passed, 8 skipped` (sin cambios — esta spec no toca
código Python). Ruff y mypy limpios.

## Planificación recurrente con revalidación por-ocurrencia (2026-08-19)

Cierra `specs/042-recurring-plan-scheduling/`, primer ítem de P3. Gap
verificado leyendo `Scheduler` (Spec 032) completo: solo soporta una
ejecución por `Plan` — `mark_executed(plan_id)` lo mueve a un estado
terminal `executed` y `grep -ri "recur|RRULE|cron|repeat" src/domoai/
runtime/scheduler.py src/domoai/domain/models.py` no devolvía nada. Un
operador que quiere "esto todos los días a las 22:00" no tenía forma
de expresarlo: tendría que recrear y reprogramar un `Plan` nuevo a
mano tras cada ejecución, para siempre.

Decisión de diseño confirmada con el usuario vía AskUserQuestion antes
de escribir el spec: cada ocurrencia de un schedule recurrente se
revalida contra el estado real del runtime inmediatamente antes de
ejecutar — nunca se valida una vez y se reproduce ciegamente. Esto es
un requisito directo del Principio III de la constitución
(capacidades/política pueden cambiar entre ocurrencias) y tiene una
consecuencia de seguridad concreta: una ocurrencia cuyos comandos
resulten `REQUIRES_CONFIRMATION` o inválidos en el momento de revalidar
NUNCA se auto-aprueba — se salta y se audita, y la recurrencia sigue
hasta su siguiente ocurrencia programada en vez de romperse.

- Nuevo modelo `RecurrenceRule` (`time_of_day`, `timezone` IANA,
  `days_of_week` opcional — `None` = diario), en
  `src/domoai/domain/models.py`.
- Nueva función pura `next_occurrence(rule, after) -> datetime` en
  `src/domoai/runtime/recurrence.py`, usando `zoneinfo` de la stdlib
  (sin dependencia nueva). Calcula siempre en hora local del
  `timezone` de la regla y convierte a UTC solo al final — nunca al
  revés — para que "22:00 hora local" siga siendo 22:00 hora local
  cruzando un cambio de horario de verano/invierno, no un salto
  silencioso de una hora en UTC. Probado contra fechas reales de
  cambio de DST en `Europe/Madrid` (spring-forward 2026-03-29 y
  fall-back 2026-10-25) — ambas verifican que la hora local resultante
  sigue siendo exactamente 22:00.
- Nueva tabla `recurring_schedules` (migración
  `005_recurring_schedules.sql`), completamente independiente de
  `scheduled_plans` — mismo patrón `payload TEXT` JSON-serializado ya
  usado por `ScheduledPlanRepository`. Nuevo
  `RecurringScheduleRepository` (`create`/`get`/`list_active`/
  `advance`/`cancel`) en `src/domoai/persistence/repositories.py`.
- `Scheduler` extendido (no modificado) con
  `schedule_recurring`/`cancel_recurring`/`list_recurring`/
  `run_due_recurring`, en `src/domoai/runtime/scheduler.py`. El nuevo
  `recurring_repository` es un parámetro keyword-only con default
  `None`, así que los 4 call sites existentes de `Scheduler(...)`
  siguen funcionando sin modificar. `run_due_recurring()` es un método
  nuevo y paralelo a `run_due()` (Spec 032), no una fusión — mantiene
  el camino one-shot ya probado completamente intacto. Por cada
  schedule activo vencido: construye un `Plan` fresco con
  `id=f"{schedule_id}@{execute_at.isoformat()}"` (único por
  construcción, no colisiona con el guard de reclamo de Spec 031 al
  ser siempre un id nuevo), llama `PlanService.validate()` contra el
  estado real; si el resultado es `PlanStatus.READY` ejecuta vía
  `PlanExecutor.execute()`; si no (`REQUIRES_CONFIRMATION` o
  inválido), audita `recurring_occurrence_skipped` con
  `payload.reason` (`"requires_confirmation"` o `"invalid"`) — un solo
  tipo de evento para ambos casos, por decisión explícita del spec.
  Cualquiera que sea el resultado, calcula la siguiente ocurrencia y
  hace `advance(...)`, así que un salto nunca rompe la recurrencia.
  `Scheduler.run()` ahora llama a `run_due()` y `run_due_recurring()`
  en cada iteración del poll loop.
- `build_runtime()` en `runtime_factory.py` construye
  `RecurringScheduleRepository` y la pasa a `Scheduler(...)`; expuesta
  también en `RuntimeComposition.recurring_schedule_repository`.
- Tres tools MCP nuevas en `domotics_server.py`:
  `schedule_recurring_plan` (toma un `plan_id` ya validado vía
  `validate_plan`, descarta su validación obsoleta y usa solo
  `.commands` como plantilla — coherente con la decisión de revalidar
  cada ocurrencia), `cancel_recurring_schedule`,
  `list_recurring_schedules`. Mismo patrón de
  `mutation_annotations`/`read_annotations` que las tools de
  scheduling one-shot existentes. Listas de nombres de tools
  hardcodeadas actualizadas en `test_domotics_mcp_contract.py`,
  `test_unified_mcp_contract.py` y
  `test_home_assistant_provider_runtime.py`.
- Probado: `next_occurrence` correcto en spring-forward, fall-back,
  restricción por `days_of_week`, y garantía "siempre estrictamente
  después de `after`" (`tests/unit/runtime/test_recurrence.py`, 4
  tests). Un schedule recurrente de riesgo SAFE se auto-ejecuta y
  avanza sin doble-ejecución antes de su siguiente hora; una ocurrencia
  cuyo dispositivo ya no existe se salta como inválida; una ocurrencia
  con `risk_class=CONFIRM` se salta, nunca se auto-aprueba, y queda
  auditada con `reason="requires_confirmation"`; tras ese salto la
  recurrencia sigue funcionando con normalidad en su siguiente hora;
  cancelar detiene toda ocurrencia futura (`tests/unit/runtime/
  test_scheduler.py`, 7 tests nuevos). Los 6 tests one-shot existentes
  en ese mismo fichero siguen pasando sin ninguna modificación —
  cero regresión a Spec 032.
- **Fuera de alcance, con justificación explícita en el spec**: EV/HVAC
  como modelos de dominio de primera clase, tarifas de exportación,
  incertidumbre de forecast, degradación de batería, Provider SDK
  externo — ítems P3 separados e independientes, sin dependencia de
  recurrencia. Expresiones de recurrencia complejas tipo RRULE
  (mensual/anual, fechas de excepción) — `time_of_day` + `days_of_week`
  opcional cubre los patrones reales de automatización del hogar sin
  inventar una gramática de calendario que nadie ha pedido. Reintentar
  o recuperar automáticamente una ocurrencia saltada — el
  comportamiento seguro es saltarla y seguir con el horario, no
  inventar un mecanismo de retry/backfill con sus propias implicaciones
  de seguridad. Editar un schedule recurrente existente in-place —
  este spec soporta crear y cancelar; cambiar su regla se asume
  cancelar-y-recrear por ahora, una operación más pequeña y segura que
  una API de mutación in-place.

Evidencia: `500 passed, 8 skipped` (490 → 500). Ruff y mypy limpios
(93 ficheros fuente).

## Planes, políticas y ejecución

Un `Plan` contiene entre 1 y 50 `Command`. Cada comando identifica
`device_id`, `command`, valor/unidad opcionales, precondiciones, `risk_class` e
`idempotency_key`. La validación devuelve:

- `status`: `valid`, `invalid`, `stale` o `requires_confirmation`;
- `runtime_revision` y `digest` para detectar cambios entre preview y execute;
- decisiones de política y errores tipados (`ErrorDetail`).

El digest debe coincidir en `execute_plan`. Si cambia el inventario, estado,
política o expiración del plan, el runtime exige validación de nuevo. Las
acciones sensibles no se autoaprueban: requieren un `approval` explícito y el
executor produce un `ExecutionOutcome` por comando, con estado anterior,
posterior y error sanitizado.

Los errores MCP siguen la forma:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Input does not satisfy the v1 contract",
    "retryable": false,
    "details": {}
  }
}
```

No se incluyen tokens, cabeceras de autorización ni secretos en errores o
eventos de auditoría.

## Runtime multi-adapter

La configuración acepta varias fuentes completas a la vez. Cada adapter
mantiene su `adapter_id`; el `CompositeAdapter` los coordina y anota esa
procedencia en las lecturas, eventos y diagnósticos internos. El MCP continúa
recibiendo únicamente `Device`, `Capability`, `StateSnapshot` y planes
canónicos.

La identidad sigue el modelo dispositivo/entidad de Home Assistant: varias
entidades pueden pertenecer a un dispositivo físico, pero cada una conserva su
`SourceRef`. Las claves estables (`identifiers`/`connections`) permiten
mantener la identidad dentro de una fuente; un `canonical_id` explícito es el
único mecanismo de enlace entre adapters. Los nombres y áreas nunca fusionan
dispositivos por sí solos.

Antes de ejecutar, el registry resuelve cada comando a una única ruta
`(canonical_device, capability, command) → (adapter_id, source_entity_id)`.
`route_ambiguous`, `route_not_found`, `source_unavailable` y referencias
desconocidas son errores de validación y producen cero escrituras. No existe
failover automático entre rutas ambiguas. Una fuente caída se marca no
disponible y las contribuciones de las fuentes sanas permanecen operativas.

## Provider SDK v1

El Provider SDK es la frontera para futuras fuentes de telemetría y comandos,
no un segundo modelo de dispositivos. Reutiliza `DeviceType`, `Capability`,
`SourceRef` y `ExecutionStatus` del dominio canónico y añade:

- `ProviderManifest`: identidad, protocolo, versión de paquete, roles
  (`telemetry`, `commands`) y capacidades semánticas no sensibles.
- `DeviceDescriptor`: identidad local del proveedor, metadatos, claves de
  identidad, conexiones y topología antes de una posible fusión canónica.
- `Measurement`: métrica canónica, unidad, valor, calidad, timestamps con zona
  horaria y `SourceRef` cuyo adapter coincide con `provider_id`.
- `ProviderCommand`/`ProviderExecutionResult`: traducción acotada con
  idempotencia, sin registros Modbus, topics MQTT, entidades HA o credenciales.
- `ProviderRegistry`: orden estable por ID, routing por rol, resultados
  parciales y diagnósticos sanitizados cuando un proveedor falla.

Un proveedor puede ser solo de lectura, solo de comandos o combinado. MCP,
OR-Tools, `StateStore` y `AdapterPort` siguen viendo contratos semánticos. La
implementación concreta `HomeAssistantProvider` reutiliza el cliente
autenticado existente y la normalización semántica del adapter clásico: agrupa
entidades por `device_id`, consulta el registro de entidades por WebSocket
cuando el payload de estados no trae ese dato, conserva metadatos de identidad
y traduce únicamente comandos declarados.

El runtime registra este provider en `ProviderRegistry` y lo envuelve en
`HomeAssistantProviderAdapter`, que satisface `AdapterPort` y alimenta el
`DeviceRegistry`, `StateStore`, ejecución y MCP existentes. Así se
mantiene una única conexión y una única ruta semántica — es la única
integración de Home Assistant del runtime (Spec 081 retiró el
`HomeAssistantAdapter` clásico y el switch `DOMOAI_HOME_ASSISTANT_PROVIDER`
que seleccionaba entre ambos, cerrando P2 #9 del re-audit 2026-08-19:
las dos implementaciones traducían comandos de forma independiente y ya
habían empezado a divergir).

La [documentación del registro de dispositivos de Home Assistant](https://developers.home-assistant.io/docs/device_registry_index/)
sirve como referencia para preservar identificadores, conexiones,
fabricante/modelo y relaciones `via_device`, pero no autoriza a inferir por sí
sola la semántica energética de una entidad.

Los sensores energéticos requieren un mapping explícito por entidad y
capacidad, por ejemplo `sensor.pv_power` + `power` → `energy.pv.power`.
El provider no adivina si un sensor de potencia representa fotovoltaica, red,
consumo, batería o vehículo. La REST API proporciona estados y servicios; la
WebSocket API proporciona `state_changed` y el registro de entidades. El token
queda exclusivamente en el cliente de transporte.

Los schemas publicados son `provider-manifest`, `device-descriptor`,
`measurement`, `provider-command`, `provider-execution-result`,
`provider-diagnostic`, `provider-discovery-result` y
`provider-collection-result` bajo [`schemas/v1/`](../schemas/v1/). El contrato
completo y el alcance diferido están reflejados en los schemas publicados y
en los tests de contrato del repositorio.

## Operabilidad del laboratorio y compatibilidad MCP

`domoai-lab` es una herramienta de desarrollo, no otro runtime domótico ni un
bus MCP. Su `LabRunner` solo acepta las operaciones `up`, `status`, `down` y
`smoke`, valida una allowlist de servicios y construye argv para Docker
Compose sin evaluar shell. El archivo local `dev/lab/.env` solo se inyecta al
subproceso Compose; el smoke determinista lo excluye expresamente junto con
todas las variables `DOMOAI_*`.

El perfil `smoke` selecciona pruebas fixture de las cinco fronteras locales:
Home Assistant, MQTT/Zigbee2MQTT, Modbus, Matter y KNX. No es evidencia de
commissioning ni sustituye los smoke tests live. La configuración real de
OMIE/Open-Meteo, un nodo Matter comisionado y un gateway KNX permanecen fuera
del camino determinista.

`domoai.mcp.compat.ensure_fastmcp_settings_ready()` es una corrección estrecha
de compatibilidad para el `Settings.lifespan` genérico de FastMCP cuando la
versión instalada de Pydantic lo deja incompleto. Se invoca antes de construir
el servidor unificado, no modifica modelos de dominio, no crea una ruta MCP
alternativa y no instala filtros globales de warnings. Si el SDK ya
está completo, la función no hace nada.

Cuando KNX Virtual/ETS vive en Windows y el runtime en WSL, el laboratorio usa
un único gateway KNX/IP: KNX Virtual conserva su endpoint upstream y WSL
publica el endpoint que consumen ETS, DomoAI y el bridge. El bridge de batería
es un proceso de laboratorio, no un proveedor de autoridad física.

`domoai-lab up --services mqtt battery knx-bridge` inicia MQTT y la batería
antes del bridge y espera su estado supervisado. `status` distingue un proceso
vivo de un bridge `ready`: ready exige conexión MQTT/KNX, un estado completo
retenido proyectado a los grupos KNX y un sondeo independiente que lea todos
los grupos de estado de energía. El proceso hijo publica la proyección como
`degraded`; solo el supervisor puede promoverla a `ready`. `degraded`,
`failed`, un PID muerto o un status corrupto nunca habilitan una prueba live.

El estado y el log del bridge viven en `.lab-state/`, están fuera de Git y no
contienen credenciales. `domoai-lab down` detiene primero el PID supervisado y
después Compose. No debe arrancarse simultáneamente el perfil experimental
Compose `knx-gateway` y el gateway WSL de la topología Windows/WSL.

## Optimización

`OptimizationScenario` es una DSL intermedia, no código Python ejecutable. Sus
componentes son `horizon`, `loads`, `constraints`, `objectives`, `inputs`,
`assumptions` y un límite de solver. La validación semántica comprueba que el
dispositivo/capacidad/comando existen, que las unidades son compatibles y que
las cargas caben en el horizonte.

`OptimizationResult` devuelve `status` (`optimal`, `feasible`, `infeasible`,
`invalid`, `timeout` o `unknown`), diagnóstico, objetivos y un `Plan` de
propuesta. El solver nunca llama a un adapter: el plan se valida de nuevo y
se ejecuta únicamente mediante el runtime y sus políticas.

Cuando el escenario incluye `energy_context`, el dominio valida una serie
completa y ordenada por slot, con `W`/`kW` para potencia y `kWh` para energía y
SOC. El modelo acotado calcula por slot carga, solar, importación/exportación
de red y batería; impide carga y descarga simultáneas y devuelve evidencia en
`constraint_summary.slots`. Soporta `minimize_energy_cost`,
`minimize_peak_import` y `maximize_solar_self_consumption`, además del
`minimize_start` existente. Un contexto inválido, una restricción imposible o
un timeout no contienen `plan`.

El provider `EnergyContextProvider` es una frontera de solo lectura. En v1 la
implementación de aceptación es `StaticEnergyContextProvider`, por lo que no
se requieren tarifas, meteorología, solar ni batería live. La tool MCP
`get_energy_context` recibe solo un `Horizon` y devuelve un envelope con
`runtime_revision`; no expone protocolos, registros ni permisos de escritura.

La implementación compuesta `ComposedEnergyContextProvider` mantiene esa misma
frontera y recibe tres puertos independientes: `TariffProvider`,
`SolarForecastProvider` y `BatteryProvider` opcional. Cada proveedor devuelve
un resultado v1 con `Horizon`, `source_id`, `source_revision` y
`observed_at`; el composer comprueba alineación de slots, frescura y
timestamps antes de construir el `EnergyContext`. El revision combinado es
determinista y los fallos se serializan como `EnergyProviderDiagnostic`, sin
excepción original, URL, cabecera ni credencial.

El host inyecta una composición ya construida (built-in u opcionalmente
propia) mediante el parámetro `energy_context_provider` de
`build_runtime`/`build_configured_server`
(`src/domoai/application/runtime_factory.py`,
`src/domoai/mcp/configured.py`), con el mismo patrón que `adapter` ya usa
para dispositivos: si se entrega, el runtime lo usa tal cual y nunca
construye los proveedores OMIE/Open-Meteo integrados; si se omite, el
comportamiento por defecto no cambia. Requiere `energy_live=True` y que el
objeto entregado exponga `get_context(horizon)`; el host es responsable de su
propio ciclo de vida. Contrato completo en
[`specs/161-generic-energy-provider/contracts/energy-context-provider-injection.md`](../specs/161-generic-energy-provider/contracts/energy-context-provider-injection.md).

`ComposedEnergyContextProvider` también acepta `ev_providers`, una tupla de
`EVProvider` (uno por cargador EV vinculado, a diferencia de `battery` que es
singular). `StateStoreEVProvider` es la implementación de referencia: lee
conectado/SOC/capacidad/hora de salida desde `StateStore` vía un
`EVChargingBinding` (ligero, sin la ceremonia de atestación de capacidad de
`DispatchableBatteryBinding`, porque `EVState.capacity_kwh` es un valor
observado ordinario, no una declaración regulatoria) y aplica las mismas
comprobaciones de frescura/identidad/calidad que `StateStoreBatteryProvider`.
`build_runtime`/`build_configured_server` aceptan `ev_charging_bindings` para
auto-construirlos, mismo patrón que `dispatchable_battery_binding`. En la raíz
de composición, cada binding deriva también la allowlist de comandos y el
`DynamicSafetyGuard` de su actuador: la ruta de estado y la autoridad de
escritura no pueden divergir. Contrato completo en
[`specs/162-ev-charging-provider-simulator/contracts/ev-charging-provider-and-simulator.md`](../specs/162-ev-charging-provider-simulator/contracts/ev-charging-provider-and-simulator.md).

El binding incluye además `control_policy`. Para un EV latched, el runtime
resuelve el `provider_id` a un adapter concreto, reconcilia el readback al
arrancar y mantiene un lease supervisado. Los planes con varios actuadores
latched pasan por un `ControlTakeoverGroup`, que exige todos los leases antes
del primer write y ejecuta parada/readback en la liberación. Un ACK de
transporte nunca sustituye el readback físico.
Cuando un adapter publica SOC en porcentaje, debe conservar esa unidad en el
`StateSnapshot`; `StateStoreEVProvider` lo convierte a kWh únicamente cuando
la unidad es explícitamente `%` y existe una capacidad kWh válida del mismo
binding. Un valor numérico sin unidad mantiene compatibilidad histórica y se
trata como kWh, pero no se acepta ninguna conversión implícita por magnitud.

Un proveedor live futuro debe traducir sus unidades y semántica antes de
devolver el modelo canónico. Por ejemplo, [OMIE documenta periodos de 15
minutos](https://www.omie.es/en/market-results/daily/daily-market/day-ahead-price?scope=daily)
y precios de mercado que pueden ser negativos, por lo que `TariffPoint`
admite valores negativos y el adaptador debe convertir EUR/MWh a EUR/kWh. La
[API pública de REE](https://www.ree.es/es/datos/apidatos) es una fuente REST de
solo lectura con rangos temporales y agregación; sus detalles de transporte
no deben aparecer en MCP. [Open-Meteo](https://open-meteo.com/en/docs) expone
radiación solar en W/m²: no equivale automáticamente a potencia fotovoltaica
en kW, así que un proveedor debe aplicar parámetros de instalación explícitos
o usar una fuente de producción ya convertida.

El primer wrapper concreto es `OmieTariffProvider`. Lee, cuando el host lo
inyecta explícitamente, el fichero público `marginalpdbc_YYYYMMDD.v`, valida
los 92/96/100 periodos correspondientes al día local usando intervalos UTC,
selecciona `MarginalES` y convierte EUR/MWh a EUR/kWh. El cliente HTTP y el
parser son inyectables/testeables; un fallo de red o de formato se convierte
en `EnergyProviderError` sin cuerpo ni URL. El runtime live opt-in puede
componerlo con `OpenMeteoSolarProvider`; el modo por defecto sigue sin crear
clientes de red.

`OpenMeteoSolarProvider` solicita `global_tilted_irradiance` en `minutely_15`
y timestamps UNIX, y convierte W/m² mediante
`installed_kwp × irradiance / 1000 × performance_ratio`, con límite AC
opcional del inversor. El provider valida cobertura temporal exacta antes de
devolver kW canónicos y no expone detalles de Open-Meteo a MCP u OR-Tools. La
configuración de ambos providers exige `DOMOAI_ENERGY_LIVE=1`; si está ausente
o es falso, el runtime conserva fixtures/providers locales y no realiza red.
El contrato se valida con los tests de provider OMIE/Open-Meteo y sus smoke
tests independientes.

La instalación solar puede configurarse una sola vez mediante el schema
`solar-installation-profile` publicado:

```bash
export DOMOAI_ENERGY_LIVE=1
export DOMOAI_TARIFF_PROVIDER=omie
export DOMOAI_SOLAR_PROVIDER=open_meteo
export DOMOAI_SOLAR_PROFILE_PATH=config/solar-profile.json
```

El perfil persistente contiene únicamente metadatos físicos y procedencia;
`config/solar-profile.example.json` documenta su forma. Las variables solares
individuales existentes siguen funcionando como fallback de compatibilidad,
pero no pueden combinarse con el perfil. OMIE y Open-Meteo continúan
recogiéndose dinámicamente al solicitar el contexto energético. El límite
entre ambas clases de datos evita que el runtime invente kWp, orientación o
rendimiento cuando no están disponibles. La fuente JSON es reemplazable por
una futura fuente de Home Assistant o inversor sin cambiar MCP.

## EV charging y HVAC comfort como conceptos de primera clase (2026-08-19)

Cierra `specs/043-ev-charging-and/`, segundo ítem de P3. Gap
verificado leyendo `scenario.py`/`cp_sat.py` completos: hasta hoy EV y
HVAC solo podían expresarse como `Load` genérico — un único bloque
contiguo de potencia fija elegido por `_start_variables()`/
`_active_load_terms()` (`cp_sat.py:298-332`). Sin semántica propia
para ninguna de las dos clases de dispositivo: un cargador EV necesita
una potencia de carga variable por slot que acumula hacia un estado de
carga objetivo antes de un deadline — estructuralmente idéntico a
cómo `BatteryProfile` ya modela el SOC de la batería
(`cp_sat.py:133-153,176-189`, IntVars de SOC por slot encadenadas por
una ecuación de balance con eficiencia), no un bloque de potencia
fija. Un dispositivo climático necesita "estar activo lo suficiente
dentro de una ventana," no "un único bloque contiguo." Verificado que
no existe modelado térmico/ambiental en absoluto en el repo
(`grep -rniE "ambient|thermal|outdoor_temp|comfort" src/domoai` sin
resultados) — una simulación térmica RC completa sería
desproporcionada sin ningún andamiaje existente, así que esta spec
deliberadamente no la añade.

- Nuevo `EVChargingLoad` (`capacity_kwh`, `initial_soc_kwh`,
  `target_soc_kwh`, `max_charge_kw`, `min_charge_kw` opcional,
  `deadline_slot`, `charge_efficiency`) en `scenario.py` — valida su
  dominio SOC igual que `BatteryProfile.validate_state_domain`. Nuevo
  `ComfortLoad` (`earliest_slot`, `deadline_slot`, `min_active_slots`,
  `power`/`power_unit`) — valida en construcción que la ventana
  contiene al menos `min_active_slots` slots (`ValidationError`
  inmediata si no, antes de llegar siquiera al solver).
  `OptimizationScenario.ev_loads`/`comfort_loads` (aditivos,
  default vacío); la comprobación de unicidad de ids de
  `validate_loads` ahora cubre las tres listas combinadas.
- `validate_scenario()` extendida con las mismas comprobaciones de
  dispositivo/capacidad/comando/unidad que ya aplicaban a `Load`
  (factorizadas en `_validate_device_capability_command`, reutilizada
  por las tres clases de carga), comprobación de encaje en el
  horizonte, y `energy_context_required` si `ev_loads`/`comfort_loads`
  no está vacío — ninguna de las dos clases nuevas tiene sentido sin
  el contexto energético, porque el SOC y la ecuación de balance de
  red solo existen dentro de `_optimize_energy()` (`_optimize_legacy()`
  no se toca en absoluto).
- `cp_sat.py`, `_optimize_energy()`: cadena de IntVars de SOC por EV
  (idéntica al patrón de la batería), variable de carga por slot
  acotada en `[0, max_charge_kw]` (o `{0} ∪ [min_charge_kw,
  max_charge_kw]` vía el mismo patrón booleano
  `charging`/`OnlyEnforceIf` que ya usa la batería, si
  `min_charge_kw > 0`), restricción dura
  `soc[deadline_slot] >= target_soc_kwh`. Por cada `ComfortLoad`:
  BoolVars independientes por slot SOLO dentro de su ventana (sin la
  restricción "exactamente un bloque contiguo" de `Load`),
  `sum(vars) >= min_active_slots`. El término `active_load` del bucle
  por slot (que alimenta la ecuación de balance de red Y
  `_add_energy_constraints`, incluyendo `max_house_power`) se extiende
  con la suma de cargas EV y potencia de confort activa — ambas
  restricciones existentes cubren automáticamente las dos clases
  nuevas sin ningún cambio adicional, porque ya eran genéricas sobre
  "lo que sea que `active_load` contenga."
- `_proposal_plan()` gana dos parámetros opcionales
  (`ev_charge_slots`/`comfort_active_slots`, default `None`) que
  añaden `Command`s ya construidos al mismo `groups` que agrupa por
  `execute_at` (reutiliza sin cambios el splitting multi-slot de la
  Spec 032). El call site de `_optimize_legacy()` no pasa ninguno de
  los dos — su salida queda comprobadamente idéntica.
  `constraint_summary["slots"]` gana `ev_charge_kw`/`comfort_power_kw`
  por slot (antes solo se calculaban internamente, no se exponían) —
  necesario para que la invariante de balance sea auditable con las
  dos clases nuevas presentes, no solo internamente correcta.
- Probado: un EV con objetivo alcanzable llega al SOC objetivo en su
  deadline; un objetivo físicamente inalcanzable (potencia/tiempo
  insuficiente) reporta `INFEASIBLE`, nunca un plan que sub-entrega en
  silencio; un EV ya en su objetivo resuelve trivialmente; un
  `ComfortLoad` se activa en al menos `min_active_slots` de su
  ventana, no necesariamente contiguos; una ventana imposible se
  rechaza en construcción (`ValidationError`); la potencia de confort
  respeta `max_house_power` igual que cualquier otra carga (probado
  forzando una infeasibilidad real, no solo verificando aritmética);
  EV + confort + carga genérica + batería coexisten sin que ninguno
  se salte `max_house_power`; los 24 tests preexistentes del solver
  (`test_energy_optimization.py`, `test_optimization_fixtures.py`,
  `test_energy_optimizer_targets.py`) siguen pasando sin ninguna
  modificación — cero regresión.
- **Fuera de alcance, con justificación explícita en el spec**: V2G
  (descarga vehículo-a-red) — esta spec solo añade carga
  unidireccional; `BatteryProfile` ya tiene el patrón de descarga para
  extender si algún día se nombra una necesidad real. Simulación
  térmica RC completa para HVAC — sin andamiaje existente,
  desproporcionado. Arbitraje de potencia compartida entre múltiples
  cargadores EV más allá de `max_house_power` — la restricción
  genérica ya cubre la potencia agregada de todas las cargas
  incluyendo EVs. Editar/cancelar un plan de carga EV o confort en
  curso — mismo tratamiento que cualquier otra carga hoy (un escenario
  nuevo se resuelve desde cero). Integración a nivel de adapter con un
  cargador EV o termostato real — fuera de alcance; el plan propuesto
  ya fluye por el boundary Command/Plan/PlanExecutor existente sin
  cambios.

Evidencia: `500 passed, 8 skipped` (500 → 507, 7 tests nuevos). Ruff y
mypy limpios (93 ficheros fuente). `schemas/v1/
optimization-scenario.schema.json` regenerado — solo cambio aditivo
(dos nuevas definiciones de modelo, dos nuevos campos opcionales en
`OptimizationScenario`), ningún campo existente cambia de forma.

## Fidelidad de actuación del optimizador y límite de batería (2026-08-21)

La compilación de propuestas trata ahora las decisiones por slot como un
timeline físico completo, no como una lista de slots positivos:

- Un `Load` con `end_command` conserva su acción de fin incluso cuando cae
  exactamente en `horizon.end`.
- EV emite también `0 kW` durante huecos de carga y una parada final en el
  primer instante posterior al último slot positivo, incluyendo
  `horizon.end`.
- `ComfortLoad` exige `end_command`/ `end_value` explícitos y compila una
  acción de inicio y otra de fin para cada tramo activo contiguo. El runtime no
  inventa si el termostato debe restaurar, volver a automático o apagarse.
- Un escenario válido sin acciones devuelve una propuesta vacía
  (`plan=null`, `plans=[]`) y no un error de indexación. La dirección
  declarada de `minimize_start` se respeta también en el camino energético.

`BatteryProfile` conserva el contexto matemático y gana un binding opcional
`actuator` con `device_id`, `capability`, `charge_command`,
`discharge_command`, `stop_command` y `power_unit="kW"`. Los tres comandos
deben ser distintos: la semántica v1 usa magnitudes positivas y no infiere
inversores de setpoint firmado.

Cuando el binding existe, `_proposal_plan()` compila las decisiones de
`battery_charge_kw`/`battery_discharge_kw` como transiciones físicas: emite
la orden de carga o descarga al cambiar dirección/valor, una parada al pasar
a cero y otra exactamente en `horizon.end` si el último estado sigue activo.
Los comandos generados atraviesan la validación normal de registry,
capability, route, policy, fingerprints, safety y preconditions; el binding no
crea ni autoriza una ruta por sí mismo.

Cuando el binding falta, el proveedor puede seguir entregando contexto para
análisis, pero una propuesta con carga o descarga no nula conserva el guard
fail-closed: `OptimizationService.validate_proposal()` devuelve
`status=invalid`, diagnóstico `battery_actuation_unbound` y ningún plan. Ese
resultado no entra en aprobación, scheduling ni ejecución.

El binding dispatchable también debe declarar `power_feedback_capability`,
`power_feedback_convention` (`charge_positive` o `discharge_positive`) y una
`power_feedback_tolerance_kw` estrictamente positiva. La capability debe ser
numérica, legible, estar expresada en `kW` y tener exactamente una ruta
canónica disponible; el binding no crea esa ruta. `BatteryState` sigue siendo
evidencia de planificación de solo lectura y conserva la procedencia inicial
del SOC, no una autorización ni una reconciliación post-ejecución.

Cada comando de carga, descarga y parada lleva un único
`CommandPostcondition` sobre esa capability medida. El optimizador conserva
magnitudes positivas internamente y las traduce al signo declarado: con
`charge_positive`, cargar es positivo, descargar negativo y parar es `0`; con
`discharge_positive`, los signos se invierten. Después de la aceptación del
adapter, `PlanExecutor` lee la ruta de feedback —que puede ser distinta de la
ruta de escritura—, persiste la observación y solo confirma si está `CURRENT`
y cae dentro de la tolerancia absoluta. Falta de lectura, estado stale,
unavailable/invalid o discrepancia producen `UNKNOWN`.

Los comandos custom sin postcondition tipado también son fail-closed: ya no se
confirman por la mera existencia de una snapshot. Este contrato no pretende
resolver todavía el settling del SOC, control en lazo cerrado, protocolo de
un inversor concreto ni recuperación de un write ambiguo.

## Settling bounded del feedback físico (2026-08-22)

La confirmación de una postcondition de feedback puede declarar ahora una
ventana acotada de estabilización:

- `settle_timeout_seconds` es opcional, no negativo y está limitado a 120 s;
  omitido o igual a cero conserva la confirmación inmediata de una sola
  lectura.
- `poll_interval_seconds` es positivo, está limitado a 10 s y, cuando existe
  una ventana positiva, no puede superarla.
- `BatteryActuator` lleva la misma política en
  `power_feedback_settle_timeout_seconds` y
  `power_feedback_poll_interval_seconds`; CP-SAT la copia a los comandos de
  carga, descarga y parada.

Después de que el adapter acepte, `PlanExecutor` mantiene una sola escritura
física y repite únicamente el `read_state()` de la ruta canónica de feedback.
Sale inmediatamente cuando encuentra una observación `CURRENT` dentro de la
tolerancia. Si el plazo termina, conserva la última observación persistida y
devuelve `UNKNOWN`/`UNAVAILABLE`; nunca reenvía automáticamente la orden.
Errores transitorios de lectura pueden reintentarse dentro del presupuesto y
`CancelledError` sigue propagándose.

Este contrato resuelve el settling de la medición de potencia, no el settling
del SOC. No añade proveedor/inversor de vendor, estimación o reconciliación de
SOC, control en lazo cerrado, reconciliación de writes ambiguos ni recovery
post-crash. Contrato detallado: [`specs/098-battery-feedback-settling/contracts/battery-feedback-settling.md`](../specs/098-battery-feedback-settling/contracts/battery-feedback-settling.md).

## Provenance del SOC inicial (2026-08-22)

`BatteryProfile.initial_soc_kwh` sigue siendo el único valor numérico que
consume CP-SAT, pero ahora puede llevar `initial_soc_observation` con la
evidencia que lo respalda: `provider_id`, `device_id`, valor canónico en `kWh`,
`MeasurementQuality`, `observed_at`, `received_at` y `SourceRef`. El valor
observado debe coincidir con `initial_soc_kwh` dentro de `1e-6 kWh`; los dos
campos no representan dos SOC independientes.

La `BatteryState` despachable —un perfil con `actuator`— falla cerrado si no
tiene observación, si la calidad no es `GOOD`, si el proveedor o el dispositivo
no coincide, o si la observación está fuera de la ventana de frescura del
`ComposedEnergyContextProvider`. Las observaciones futuras, stale, unavailable
o invalid no llegan al `EnergyContext`. Un perfil sin actuador puede seguir
siendo análisis-only sin telemetría live.

Esta provenance permanece en el contexto serializado y permite diagnóstico,
pero no es autorización, claim de ejecución, postcondition de potencia ni
reconciliación posterior al write. La Spec 099 no convierte porcentajes,
estima SOC, integra un inversor concreto ni actualiza el SOC después de una
actuación.
Contrato detallado: [`specs/099-battery-soc-provenance/contracts/battery-soc-provenance.md`](../specs/099-battery-soc-provenance/contracts/battery-soc-provenance.md).

## Bridge de telemetría SOC (2026-08-22)

El provider boundary expone ahora un bridge único desde el `Measurement` del
Provider SDK hacia `BatterySocObservation`. Solo acepta la métrica exacta
`battery.soc` con valor numérico no booleano y unidad canónica `kWh`; copia sin
pérdida `provider_id`, `device_id`, métrica, unidad, valor, calidad,
`observed_at`, `received_at` y `SourceRef`.

Una medición `battery.soc` en `%`, `Wh`, sin unidad, textual, booleana o con
otra métrica se rechaza con un `EnergyProviderDiagnostic` sanitizado. No se
infieren capacidad/reserva, no se convierten porcentajes y no se elige entre
fuentes concurrentes. `STALE`, `UNAVAILABLE` e `INVALID` se conservan como
evidencia y siguen siendo rechazados por la guardia de `BatteryState` cuando el
perfil es despachable.

El bridge es puro y de solo lectura: no llama adapters, no muta `StateStore`,
no autoriza comandos y no reconcilia SOC después de un write. Contrato
detallado: [`specs/100-battery-soc-measurement-bridge/contracts/battery-soc-measurement-bridge.md`](../specs/100-battery-soc-measurement-bridge/contracts/battery-soc-measurement-bridge.md).

## Conversión explícita de SOC porcentual (2026-08-22)

La Spec 101 añade una segunda frontera explícita para proveedores que entregan
`battery.soc` en `%`. La conversión solo acepta valores finitos entre `0` y
`100`, capacidad positiva y finita declarada por configuración, y evidencia de
capacidad ligada al mismo `provider_id` y `device_id`. El resultado es:

```text
value_kwh = source_value_percent / 100 * capacity_kwh
```

`BatterySocObservation` conserva `conversion_evidence` con el porcentaje
original, la capacidad y el método fijo
`percentage_of_declared_capacity`. La provenance, timestamps y
`MeasurementQuality` se copian sin elevar calidad; una observación `STALE`,
`UNAVAILABLE` o `INVALID` sigue sin ser despachable.

Home Assistant continúa publicando la medición raw en `%`; el Provider SDK no
adivina capacidad ni convierte automáticamente. Un proveedor específico debe
invocar esta frontera con evidencia explícita. Unidades distintas, porcentajes
fuera de rango, capacidad ausente/inválida o identidad cruzada fallan cerrado
con diagnósticos sanitizados. La conversión no llama adapters, no muta
`StateStore`, no autoriza ni ejecuta planes y no reconcilia SOC post-write.
Contrato detallado: [`specs/101-battery-soc-percent-conversion/contracts/battery-soc-percent-conversion.md`](../specs/101-battery-soc-percent-conversion/contracts/battery-soc-percent-conversion.md).

## Binding explícito de capacidad Home Assistant (2026-08-22)

La Spec 102 añade a la configuración v1 un mapa opcional de bindings de
capacidad nominal. Cada entrada usa el `entity_id` exacto de Home Assistant y,
preferentemente, claims estables del registro (`identity_keys` y/o
`connections`). El `device_id` esperado sigue admitido solo como fallback
legacy: no se acepta emparejamiento por nombre, área, `friendly_name` ni fuzzy
matching. En modo estable, el provider consulta el registro vivo, resuelve
exactamente un `device_id` actual y falla cerrado ante cero o múltiples
coincidencias; así una recreación del `device_id` no rompe el binding.

Cuando la entidad coincide, el provider la proyecta como medición read-only
`battery.capacity` en `kWh`, conservando provider/device, `SourceRef`, calidad
y timestamps. La configuración declara explícitamente que el valor es
capacidad nominal; energía restante y flujo acumulado son semánticas distintas
y no se autodetectan.

El bridge `battery_capacity_evidence_from_measurement()` acepta únicamente
`battery.capacity`/`kWh` con valor numérico positivo y finito y crea
`BatteryCapacityEvidence` con `capacity_source="provider_measurement"`. La
evidencia conserva `SourceRef`, `observed_at`, `received_at` y
`MeasurementQuality`. La conversión `% → kWh` de Spec 101 acepta esa fuente
solo con calidad `GOOD` y misma identidad provider/device; la evidencia
`provider_config` anterior sigue siendo compatible.

Sin binding, Home Assistant mantiene su SOC raw en `%` y el runtime no fabrica
capacidad ni habilita dispatch. No se añaden descubrimiento vendor, conversión
automática de unidades, StateStore, comandos, aprobación ni reconciliación
post-write. Contrato detallado: [`specs/102-home-assistant-battery-capacity/contracts/home-assistant-battery-capacity.md`](../specs/102-home-assistant-battery-capacity/contracts/home-assistant-battery-capacity.md).

### Gate mínimo de clase de dispositivo (Spec 105)

Antes de proyectar un binding como `battery.capacity`, el provider exige que
los atributos normalizados de esa entidad declaren exactamente
`device_class=energy_storage`. La ausencia de la clase o una clase distinta
produce `HomeAssistantMappingConfigurationError` sanitizado; no se elige otra
entidad ni se continúa con la medición.

Este gate es comprobable, pero no es una prueba universal de capacidad
nominal: Home Assistant usa `energy_storage` tanto para energía almacenada como
para capacidad. Por eso `semantics=nominal_capacity` sigue siendo una
atestación explícita del operador/proveedor y no se infiere por nombre, unidad,
área, modelo o integración. La documentación oficial de Powerwall también
describe diferencias entre modelos en la disponibilidad de sensores de
Capacity/Remaining.

Contrato detallado: [`specs/105-ha-capacity-device-class-gate/contracts/home-assistant-capacity-gate.md`](../specs/105-ha-capacity-device-class-gate/contracts/home-assistant-capacity-gate.md).

### Attestation de capacidad nominal (Spec 106)

Un binding de capacidad nominal de Home Assistant exige ahora una
`NominalCapacityAttestation` no secreta con `evidence_type`, referencia,
modelo/subject, attester y `attested_at` timezone-aware. La atestación viaja
sin pérdida desde el binding a la `Measurement` `battery.capacity` y a
`BatteryCapacityEvidence`.

La frontera de `provider_measurement` falla cerrado si la medición no lleva esa
atestación; el diagnóstico es `missing_nominal_capacity_attestation`. La ruta
`provider_config` sigue siendo válida para perfiles estáticos explícitos.
Esto es provenance auditable, no autenticidad criptográfica: el runtime no
descarga la referencia ni verifica online al attester. La atestación no
autoriza comandos, no habilita `build_runtime` y no activa dispatch.

Contrato detallado: [`specs/106-nominal-capacity-attestation/contracts/nominal-capacity-attestation.md`](../specs/106-nominal-capacity-attestation/contracts/nominal-capacity-attestation.md).

### Trust policy de capacidad nominal (Spec 107)

La atestación de Spec 106 es provenance del proveedor, no identidad
autenticada. `NominalCapacityTrustPolicy` es configuración server-owned y
exige allowlists no secretas, no vacías y sin duplicados para el tipo de
evidencia, `attested_by` y la referencia exacta del documento revisado. El
runtime no descarga la referencia ni acepta prefijos de URL.

`StateStoreBatteryProvider` aplica el gate solo cuando el perfil tiene un
actuador y consume `BatteryCapacityEvidence` de tipo
`provider_measurement`. Falta de policy o cualquier mismatch produce un
diagnóstico sanitizado (`nominal_capacity_trust_required`,
`nominal_capacity_evidence_type_not_trusted`,
`nominal_capacity_attester_not_trusted` o
`nominal_capacity_reference_not_trusted`) antes de construir el estado
despachable. El perfil analysis-only puede seguir usando evidencia explícita,
y `provider_config` conserva su compatibilidad estática; ninguna de las dos
rutas crea un actuador ni habilita `build_runtime`.

Contrato detallado: [`specs/107-nominal-capacity-trust-policy/contracts/nominal-capacity-trust-policy.md`](../specs/107-nominal-capacity-trust-policy/contracts/nominal-capacity-trust-policy.md).

### Binding completo de batería despachable (Spec 108)

`DispatchableBatteryBinding` agrupa el `provider_id`, `device_id`, la
capability canónica `battery.soc`, el `BatteryProfile`, la evidencia explícita
de capacidad y la policy de trust cuando la capacidad proviene de medición.
Falla cerrado si falta el actuador, si su dispositivo no coincide, si no
declara `soc_reconciliation_capability="battery.soc"`, si la evidencia
cruza provider/device, si no coincide con `profile.capacity_kwh` o si una
medición no es `GOOD`/no lleva policy.

`StateStoreBatteryProvider.from_binding()` es la composición explícita que
acepta ese agregado y evalúa la policy antes de devolver el provider. La
disponibilidad y ambigüedad de las rutas de comandos, feedback de potencia y
SOC siguen siendo responsabilidad de `DeviceRegistry`/`validate_scenario`;
este modelo no infiere rutas por nombre, área o modelo. `build_runtime` no
instala el provider ni activa hardware automáticamente.

Contrato detallado: [`specs/108-dispatchable-battery-binding/contracts/dispatchable-battery-binding.md`](../specs/108-dispatchable-battery-binding/contracts/dispatchable-battery-binding.md).

### Binding de rutas Home Assistant para batería (Spec 109)

`HomeAssistantMappingDocument` acepta ahora opcionalmente
`battery_dispatch_bindings`. `HomeAssistantDispatchableBatteryBinding` es un
contrato específico del adapter que preserva los IDs exactos de Home
Assistant, pero mantiene los semánticos canónicos fijos:

| Dato | Semántica | Unidad |
| --- | --- | --- |
| `soc_entity_id` | `battery.soc` | `%` |
| `power_feedback_entity_id` | `battery.power` | `kW` |
| `capacity_entity_id` | `battery.capacity` | `kWh` |

Las rutas `charge`, `discharge` y `stop` declaran cada una un `entity_id` y
un `provider_command`; los nombres de comando deben ser distintos. La
capacidad debe referenciar un binding nominal existente con el mismo
`device_id`, por lo que no se puede construir accidentalmente un perfil con
capacidad de otra batería.

La validación es estática y sin efectos laterales. No hace discovery, no
consulta Home Assistant, no comprueba disponibilidad/escritura/readback y no
contiene secretos o payloads de servicio. `build_runtime` continúa sin
instalar un `StateStoreBatteryProvider` ni habilitar dispatch por la mera
presencia de este campo. El futuro wiring debe verificar registry, capability,
policy, safety y convergencia de potencia/SOC antes de crear el
`DispatchableBatteryBinding` canónico.

Contrato detallado: [`specs/109-home-assistant-battery-route-binding/contracts/home-assistant-battery-route-binding.md`](../specs/109-home-assistant-battery-route-binding/contracts/home-assistant-battery-route-binding.md).

### Validación de rutas HA contra snapshot (Spec 110)

La declaración estática de Spec 109 puede validarse contra un
`AdapterSnapshot` ya obtenido:

```python
snapshot = await provider.snapshot()
provider.validate_battery_dispatch_routes(snapshot)
```

La operación es síncrona y side-effect-free sobre ese snapshot. Exige
identidad exacta de cada entidad, SOC `battery.soc`/`%`, feedback
`battery.power`/`kW`, capacidad `battery.capacity`/`kWh` con binding nominal y
clase `energy_storage`, además de capabilities escribibles para las tres
rutas de comando. Falta, disponibilidad negativa, unidad incompatible,
capacidad incorrecta, device cruzado o comando no expuesto producen
`HomeAssistantMappingConfigurationError` sanitizado.

La validación no hace un refresh implícito, no llama `call_service`, no hace
fallback por nombre/área/modelo y no instala batería despachable. El snapshot
lo adquiere el caller mediante el límite read-only existente; el resultado
solo habilita una futura comprobación de registry/policy/safety/readback.

Contrato detallado: [`specs/110-home-assistant-battery-route-validation/contracts/home-assistant-battery-route-validation.md`](../specs/110-home-assistant-battery-route-validation/contracts/home-assistant-battery-route-validation.md).

### Gate de traducibilidad de comandos HA (Spec 111)

La validación de rutas de batería no se detiene en comprobar que una
capability sea `writable` y exponga el nombre declarado. También reutiliza el
traductor puro de `HomeAssistantProvider` con un `ProviderCommand` sintético,
sin ejecutar el comando. Si el traductor no puede producir una acción HA
soportada —por ejemplo, `set_position` sin `params["value"]`— la validación
falla con `HomeAssistantMappingConfigurationError`.

Este gate no hace discovery ni consulta servicios live, no añade nombres de
servicio arbitrarios y no crea batería despachable. Las rutas value-bearing
legacy siguen rechazándose; las rutas numéricas explícitas de Spec 114 son la
única ampliación aceptada para transportar un setpoint.

Contrato: `specs/111-home-assistant-battery-translation-gate/`.

### Rutas numéricas y smoke HIL del inversor (Spec 114)

La ampliación de Spec 114 permite que `HomeAssistantBatteryCommandRoute`
declare únicamente el servicio numérico `number.set_value` y una transformación
explícita `as_is`, `negate` o `zero`. El provider añade `battery_control` como
metadata de comandos en el snapshot, obtiene límites `min`/`max` y unidad de
la entidad numérica, y envía el `value` resultante. Una ruta sin capacidad de
representar el valor es rechazada antes de `call_service`; no se convierte un
setpoint en un `open`/`close` implícito.

El smoke físico de
`tests/integration/test_home_assistant_provider_hil_smoke.py` continúa
separado del laboratorio virtual. Está deshabilitado por defecto y exige
credenciales, mapping, perfil/evidencia, identidad canónica, probe positivo
limitado por el perfil, token de aprobación y confirmación exacta del
operador. Tras un único comando de carga o descarga, usa lecturas independientes
de potencia y SOC para comprobar estado actual, signo, tolerancia y estabilidad;
la respuesta del servicio y el `after_state` del executor no son evidencia
suficiente. Si el probe llegó a iniciarse, la parada aprobada se intenta en
`finally` aunque falle la aserción de convergencia. Sin todos los gates el test
se salta antes de construir el runtime y no escribe en ningún dispositivo.

### Composición HA a binding canónico (Spec 112)

`compose_home_assistant_dispatchable_battery_binding(...)` vive en la capa de
aplicación y une, sin I/O, un `HomeAssistantDispatchableBatteryBinding`
validado con un `BatteryProfile`, evidencia de capacidad y policy suministrados
por el host, además del `canonical_device_id` resuelto por `DeviceRegistry`.
Reutiliza la gate de rutas de Specs 110/111 y exige coherencia de
provider/device de fuente, dispositivo canónico, comandos de
carga/descarga/parada, capability writable común, feedback `battery.power`/`kW`,
reconciliación `battery.soc`, observación SOC y capacidad.

La función devuelve el `DispatchableBatteryBinding` canónico con el ID de
dispositivo que usan `DeviceRegistry`, `StateStore` y `Plan`; no copia el
`device_id` físico HA al contrato canónico. No crea
`StateStoreBatteryProvider`, no cambia `build_runtime`, no persiste y no llama
servicios. La instalación sigue siendo una decisión explícita del host a
través de `StateStoreBatteryProvider.from_binding()`.

Contrato: `specs/112-home-assistant-dispatchable-composition/`.

### Instalación explícita del provider en runtime (Spec 113)

El host que ya posee un `DispatchableBatteryBinding` completo puede pasarlo a
`build_runtime(..., dispatchable_battery_binding=binding)`. El composition root
lo convierte mediante `StateStoreBatteryProvider.from_binding()` usando el
`StateStore` de esa instancia y lo inyecta en el
`ComposedEnergyContextProvider`. El runtime expone el provider instalado en
`RuntimeComposition.battery_provider` para diagnóstico y validación de
composición.

Este parámetro es opcional y no reconstruye el binding desde settings,
discovery, nombres o mapping HA. Con `energy_live=False` se rechaza antes de
inicializar SQLite; sin binding no cambia la semántica de energía opcional.
La instalación no hace refresh ni service calls y no concede por sí misma
aprobación, policy, safety ni autoridad de ejecución física.

Contrato: `specs/113-explicit-runtime-battery-composition/`.

## Reconciliación durable de SOC después de writes físicos (2026-08-22)

La Spec 103 cierra la frontera entre el readback que obtiene `PlanExecutor` y
el estado que sobrevive a un restart. Cada `StateSnapshot` normalizado durante
un readback se guarda tanto en `StateStore` como en el
`StateSnapshotRepository` SQLite existente antes de devolver el resultado.

`BatteryActuator` puede declarar explícitamente
`soc_reconciliation_capability`. El compilador propaga esa declaración al
`CommandPostcondition`; después de confirmar el feedback de potencia, el
executor lee exactamente una vez la ruta única y disponible, verifica que la
observación sea `CURRENT` y la persiste con su `SourceRef`, unidad, valor y
timestamps.

Si la ruta falta, es ambigua, está unavailable/stale o falla la persistencia,
el write físico ya aceptado termina como `UNKNOWN`, se audita con un mensaje
sanitizado y nunca se reenvía el comando. La ausencia del campo conserva el
comportamiento anterior y no provoca selección implícita de `battery.soc`.
El runtime no estima SOC desde potencia/tiempo/eficiencia ni convierte el
snapshot directamente en `BatterySocObservation`; esa composición sigue siendo
responsabilidad del provider. Contrato detallado: [`specs/103-battery-soc-reconciliation/contracts/battery-soc-reconciliation.md`](../specs/103-battery-soc-reconciliation/contracts/battery-soc-reconciliation.md).

## Preflight de seguridad del plan (2026-08-21)

`PlanExecutor` mantiene el claim durable como barrera de concurrencia, pero
después del claim inspecciona todos los comandos antes de llamar por primera
vez a `AdapterPort.execute()`. El preflight comprueba preconditions actuales y
los límites configurados por `SafetyKernel`; si encuentra una violación,
devuelve outcomes `REJECTED` para el intento, persiste el resultado terminal y
no produce ningún write físico.

Los planes secuenciales conservan su semántica: para evaluar un precondition
posterior solo se proyectan localmente los efectos deterministas ya definidos
(`turn_on`, `turn_off`, `open`, `close` y `set_*` con valor). Esa proyección no
se escribe en `StateStore`, no es readback y no se aplica a comandos
desconocidos. Antes de cada write siguen ejecutándose los checks
just-in-time de preconditions y SafetyKernel, porque un cambio de estado
posterior al preflight todavía puede producir una saga parcial honesta.

Los bundles atraviesan la misma frontera mediante `PlanExecutor`; el bundle
no duplica ni relaja las reglas físicas. Una preflight rejection no contiene
credenciales, conserva `execution_attempt_id` y deja `adapter_request_id` en
`null`, porque nunca existió una request al adapter. Esto ordena la seguridad
conocida antes del hardware, pero no promete atomicidad física ni compensación.

## Correlación end-to-end de una ejecución física (2026-08-21)

`PlanExecutor` crea un `ExecutionContext` inmutable para cada comando que va a
llegar al adapter. El contexto contiene únicamente
`agent_request_id` (si existe), `plan_id`, `execution_attempt_id` y
`adapter_request_id`; no contiene tokens, credenciales ni el payload mutable
del comando. El mismo objeto se reenvía por `AdapterPort`, `CompositeAdapter`,
los bridges de provider y los transportes nativos.

Las llamadas directas a adapters/providers sin contexto siguen permitidas para
diagnóstico y fixtures, pero una ejecución originada por el executor siempre
debe llevarlo. El executor sigue siendo el único componente que genera
`adapter_request_id`; routing y providers no crean una segunda línea de
correlación.

Home Assistant propaga los identificadores no secretos al boundary HTTP como
cabeceras `X-DomoAI-*`. Matter, MQTT, KNX y Modbus reciben el contexto en su
boundary de transporte y lo conservan en request records/logging sin alterar
el payload o valor físico de protocolos que no ofrecen un canal de metadata.
Un transport failure conserva el mismo ID en el outcome `UNAVAILABLE` y en la
auditoría. Esto mejora la trazabilidad, pero no sustituye el claim durable,
la policy, el SafetyKernel ni la idempotencia.

## Aprobación de bundles por digest completo (2026-08-21)

El workflow energético separa ahora dos autoridades:

- `bundle_digest`: digest canónico del escenario y del bundle ordenado de
  miembros ya validados (`plan_id`, digest de validación de miembro y
  `execute_at`), que identifica lo que revisa y aprueba el operador.
- `validation_digest`: digest individual que continúa usando cada llamada MCP
  `request_approval`, `execute_plan` y `schedule_plan`; es la autoridad del
  runtime para ese `Plan` concreto.
- Cuando la aprobación pertenece a un bundle, el `ApprovalGrant` también
  conserva `bundle_digest`; el commit rechaza cualquier grant emitido para
  otro bundle, aunque el digest individual del plan coincida.

La explicación que recibe el host de aprobación incluye `bundle_digest` y el
resumen ordenado del bundle. El campo existente `validation_digest` del
resultado del workflow queda como alias compatible del digest de aprobación;
los hosts nuevos deben usar `bundle_digest`. Cambiar el orden, un miembro, su
digest o su timestamp cambia el digest y bloquea antes de emitir grants o
ejecutar. Esta corrección no promete atomicidad física ni rollback del bundle;
eso pertenece a una saga posterior.

## Tarifa de exportación como ingreso en el optimizador (2026-08-19)

Cierra `specs/044-export-tariff-revenue/`, tercer ítem de P3. Gap
verificado leyendo `cp_sat.py` completo: `context.tariffs` se lee
exactamente dos veces, ambas contra cantidades de importación
(`grid_import`/`import_kw`) — `command grep -rn "\.tariffs\b"
src/domoai --include="*.py"` lo confirma. La energía exportada tenía
valor económico cero en todo el optimizador: `minimize_energy_cost`
solo sumaba `grid_import * precio`, y el `energy_cost` reportado nunca
restaba ingresos por exportación, aunque `max_grid_export` y
`grid_export` ya existían como cantidad física.

- Nuevo campo aditivo `EnergyContext.export_tariffs: list[TariffPoint]
  | None = None`, estructuralmente idéntico a `tariffs` — reutiliza el
  mismo modelo `TariffPoint`, sin modelo nuevo. `validate_series`
  extendida para cubrirlo cuando está presente, con el mismo bucle
  genérico de cobertura-por-slot ya usado para `base_load_forecast`.
- `_objective_terms()`, rama `minimize_energy_cost`: nuevo término
  simétrico al de importación pero de signo opuesto
  (`-sign * weight * export_tariffs[slot].price_per_kwh * horas *
  OBJECTIVE_SCALE * grid_export[slot]`), añadido SOLO cuando
  `context.export_tariffs is not None` — con el campo ausente el
  término literalmente no se genera, objetivo matemáticamente
  idéntico a antes.
- Bucle de reporte tras resolver: nuevo `export_revenue` acumulado
  igual que `energy_cost` pero sobre `export_kw`; `objective_values
  ["energy_cost"]` pasa a ser neto (`energy_cost - export_revenue`,
  que es `energy_cost` sin cambios cuando `export_revenue` es `0.0`
  por defecto); nuevo `objective_values["export_revenue"]` expuesto
  por separado — el ingreso nunca queda oculto dentro de una única
  cifra combinada, sirviendo directamente la auditabilidad exigida
  por el Principio III.
- `ComposedEnergyContextProvider` gana un parámetro opcional
  `export_tariffs: TariffProvider | None = None`, con el mismo
  tratamiento ya existente para `battery` (opcional, se resuelve solo
  si está presente, se valida horizonte/frescura con los mismos
  helpers genéricos). Reutiliza el protocolo `TariffProvider` tal
  cual — sin protocolo nuevo, cualquier `TariffProvider` existente
  (incluido `StaticTariffProvider`) ya sirve para tarifa de
  exportación sin código adicional. Detalle de compatibilidad
  encontrado y corregido durante la implementación: la primera versión
  añadía siempre un segmento `export_tariff:none` a `source_revision`
  incluso sin tarifa de exportación configurada, rompiendo un test
  existente (`test_energy_providers.py::...endswith("battery:none")`)
  — corregido para solo añadir el segmento cuando la tarifa de
  exportación está realmente presente, preservando el `source_revision`
  exacto de todo escenario existente.
- Probado: una tarifa de exportación con exportación real reduce el
  coste neto reportado frente a un escenario idéntico sin ella; un
  precio de exportación más alto en un slot desplaza la exportación
  hacia ese slot cuando hacerlo es gratis; el ingreso de exportación
  se reporta como valor propio, distinto del coste neto; un escenario
  sin tarifa de exportación reporta `export_revenue == 0.0`
  exactamente; una serie de tarifa de exportación que no cubre el
  horizonte se rechaza igual que ya se rechazaba `tariffs`; los tests
  preexistentes de los tres ficheros del solver siguen pasando sin
  ninguna modificación — cero regresión. Un test de contrato MCP
  (`test_energy_context_mcp.py`) que enumeraba explícitamente el
  conjunto completo de campos de `EnergyContext` sí necesitó
  actualizarse para incluir `export_tariffs` — actualización esperada
  de un contrato que ya declaraba la forma exacta del modelo, no una
  regresión.
- **Fuera de alcance, con justificación explícita en el spec**: cablear
  una fuente de datos real de tarifa de exportación (proveedor de
  feed-in tariff concreto, API de contrato de comercializadora) — esta
  spec añade el modelo semántico y el puerto de proveedor opcional,
  mismo patrón aditivo ya usado para `base_load_forecast`; una fuente
  live puede añadirse después sin cambiar este contrato. Ningún modelo
  de precios nuevo (tarifas escalonadas, topes contractuales, umbrales
  de autoconsumo) — la tarifa de exportación usa la misma forma de
  serie por slot que la de importación, sin inventar semántica nueva.

Evidencia: `507 passed, 8 skipped` (507 → 512, 5 tests nuevos). Ruff y
mypy limpios (93 ficheros fuente). `schemas/v1/energy-context.schema.json`
y `schemas/v1/optimization-scenario.schema.json` regenerados — ambos
cambios puramente aditivos (un campo opcional nuevo cada uno).

## Incertidumbre de pronóstico solar y de carga base (2026-08-19)

`SolarForecastPoint`/`BaseLoadPoint` (`src/domoai/optimizer/energy.py`)
solo declaraban un valor puntual, sin forma de expresar confianza ni de
pedirle al solver que se cubra ante error de pronóstico (verificado:
`solar_powers`/`base_load_powers` en `cp_sat.py:126-131` leían siempre
`point.power` directamente). Alcance confirmado con el usuario vía
AskUserQuestion — ambas partes requeridas en una sola spec:

- Nuevo modelo `ConfidenceBand` (`low`, `high`) reutilizado como campo
  opcional `confidence` en ambos puntos, con cobertura todo-o-nada por
  serie (igual patrón que las series ya existentes) y validación de
  que el punto cae dentro de su propia banda. Ausente o parcial: mismo
  comportamiento de hoy o `ValidationError` en construcción.
- Nuevo flag `OptimizationScenario.conservative` (`bool`, por defecto
  `False`). Activo y con bandas presentes: el solver sustituye, en el
  único punto donde `solar_powers`/`base_load_powers` se construyen,
  el valor puntual por el extremo pesimista (`confidence.low` para
  solar, `confidence.high` para carga base) — cero código adicional en
  cada restricción consumidora, mismo patrón de "un solo punto de
  sustitución, consumidor genérico" ya usado en la spec 043 para
  `active_load`. Activo sin bandas en una serie requerida: rechazado en
  `validate_scenario` con diagnóstico `conservative_mode_requires_confidence`,
  sin intentar resolver.
- `objective_values["conservative_mode_active"]` y
  `constraint_summary["forecast_confidence"]` (`solar_bounded`,
  `base_load_bounded`) hacen el resultado auto-descriptivo sin releer
  el escenario de entrada.
- Cero regresión verificada: banda presente + modo conservador apagado
  produce plan y coste idénticos a no tener banda; flag ausente o
  `False` es idéntico a hoy independientemente de las bandas.
- **Fuera de alcance, con justificación explícita en el spec**: cómo se
  calculan las bandas (percentiles concretos, calibración estadística)
  — se tratan como límites opacos suministrados por el proveedor
  externo. Seguimiento de error de pronóstico en tiempo real o
  reoptimización disparada por desviación — no solicitado por el ítem
  de backlog. Tarifas y perfil de batería sin cambios — el ítem acota
  la incertidumbre a solar y carga base únicamente. Control por-serie
  o por-slot del modo conservador — un único flag por escenario,
  suficiente para el alcance pedido.

Evidencia: `512 passed, 8 skipped` (512 → 519, 7 tests nuevos). Ruff y
mypy limpios (93 ficheros fuente). `schemas/v1/energy-context.schema.json`,
`schemas/v1/optimization-scenario.schema.json`,
`schemas/v1/solar-forecast-point.schema.json` y
`schemas/v1/solar-forecast-series.schema.json` regenerados — los cuatro
diffs puramente aditivos (un `$defs/ConfidenceBand` nuevo y un campo
opcional `confidence`/`conservative` cada uno).

## Coste de degradación de batería (2026-08-19)

Quinto y último ítem de P3 — con esta spec, P3 queda completo. El
ciclado de batería era gratis en el objetivo del optimizador
(verificado: `_objective_terms()` solo precia `grid_import`/
`grid_export`, sin ningún término de `charge`/`discharge`), pese a que
toda batería real se desgasta con el throughput acumulado. Alcance
confirmado con el usuario vía AskUserQuestion — ambas partes
requeridas en una sola spec, mismo patrón de decisión que las specs
043/045:

- Reporte de throughput SIEMPRE presente cuando hay batería, sin
  configuración adicional: `objective_values["battery_throughput_kwh"]`
  se calcula sumando `(charge_kw + discharge_kw) * resolution_hours`
  por slot en el mismo bucle post-solve que ya calculaba
  `energy_cost`/`export_revenue` — sin variables nuevas del solver, las
  cantidades de carga/descarga ya se extraían para
  `constraint_summary["slots"]`. `0.0` cuando no hay batería.
- Nuevo campo opcional `BatteryProfile.degradation_cost_per_kwh`
  (`ge=0`). Al estar en `BatteryProfile` y no en un nivel superior, el
  requisito "no se puede configurar coste de desgaste sin batería" es
  una garantía del sistema de tipos, no una regla de validación en
  tiempo de ejecución.
- Cuando está configurado y es mayor que cero, `_objective_terms()`/
  `_solve_tiers()` reciben `charge_variables`/`discharge_variables`
  como nuevos parámetros de solo palabra clave (con valor por defecto
  `[]`, sin afectar ninguna otra rama del objetivo) y añaden un término
  proporcional al throughput dentro de `minimize_energy_cost` — el
  solver ahora prefiere ciclar menos la batería cuando ciclar no aporta
  otro beneficio, y sigue ciclando cuando el beneficio (p. ej. un
  spread de tarifa grande) supera el coste de desgaste.
- `objective_values["battery_degradation_cost"]` reportado por
  separado; `objective_values["energy_cost"]` pasa a netear
  `import_cost - export_revenue + battery_degradation_cost` (sin
  cambio cuando `battery_degradation_cost` es `0.0` por defecto),
  mismo patrón exacto que la spec 044 usó para `export_revenue`.
- Un coste de desgaste de `0.0` se trata igual que ausente — no se
  rechaza, y no distorsiona el objetivo (coeficiente cero).
- Cero regresión verificada explícitamente: ausencia del campo produce
  plan y coste idénticos a hoy; el reporte de throughput por sí solo no
  cambia ninguna decisión del solver.
- **Fuera de alcance, con justificación explícita en el spec**: fade de
  capacidad o seguimiento de estado de salud entre ejecuciones — dentro
  de un único horizonte acotado la capacidad se sigue tratando como
  constante, exactamente igual que hoy; solo se añade el coste
  económico del ciclado. Throughput de la batería del EV (spec 043) —
  la economía de desgaste de un vehículo pertenece a su propietario, no
  al optimizador doméstico. Cómo se deriva la cifra de coste por kWh
  (datos de fabricante, coste de reemplazo / vida útil en ciclos) — se
  trata como un valor opaco suministrado por la persona usuaria.

Evidencia: `519 → 526 passed, 8 skipped` (7 tests nuevos). Ruff y mypy
limpios (93 ficheros fuente). `schemas/v1/battery-profile.schema.json`,
`schemas/v1/battery-state.schema.json`,
`schemas/v1/energy-context.schema.json` y
`schemas/v1/optimization-scenario.schema.json` regenerados — los
cuatro diffs puramente aditivos (un campo opcional
`degradation_cost_per_kwh` cada uno). **P3 completo**: Specs 042, 043,
044, 045 y 046 cerradas.

## Reloj virtual del runtime (2026-08-19)

Primer ítem de un nuevo tier **P4** (plataforma/ecosistema), tras
completar P3 esta misma sesión. El control de tiempo estaba disperso e
inconsistente: 45 puntos del repositorio leían `datetime.now(UTC)`
directamente; algunos componentes ya aceptaban un `now` inyectable
por-llamada (`Scheduler.run_due`/`run_due_recurring`,
`StateStore.mark_stale`), pero otros con decisiones igual de
dependientes del tiempo no tenían ningún punto de inyección
(`PlanService.create_plan`, `PlanService.assert_executable`,
`PlanExecutor.execute`). Sin una única fuente de tiempo compartida, no
hay forma de controlar de forma determinista cuándo un plan expira,
cuándo se ejecuta, o cuándo un estado se considera obsoleto — bloqueo
duro para los ítems futuros de P4 (replay determinista, digital twin,
HIL, shadow mode), todos los cuales necesitan correr el runtime contra
tiempo controlable en vez de solo tiempo real.

- Nuevo puerto `Clock` (`src/domoai/runtime/clock.py`, `Protocol` con
  un único método `now() -> datetime`, mismo estilo que
  `AdapterPort`/el resto de puertos en `ports.py`). Dos
  implementaciones: `SystemClock` (envoltorio directo de
  `datetime.now(UTC)`, sin lógica añadida) y `FixedClock` (tiempo
  mutable, con `set()` para avanzarlo).
- `StateStore`, `PlanService`, `PlanExecutor` y `Scheduler` ganan un
  parámetro `clock: Clock | None = None` de solo palabra clave,
  guardado como `self.clock = clock or SystemClock()` — mismo patrón
  aditivo de inyección de dependencias ya usado repetidamente en esta
  sesión (`recurring_repository` en la Spec 042,
  `plan_repository`/`outcome_repository` en `PlanExecutor`). Los
  `datetime.now(UTC)` de los cinco puntos identificados pasan a
  `self.clock.now()`.
- Los parámetros `now: datetime | None` ya existentes en
  `run_due`/`run_due_recurring`/`mark_stale` se conservan intactos —
  solo cambia su fallback interno de `now or datetime.now(UTC)` a
  `now or self.clock.now()`, sin romper ningún caller existente.
- `build_runtime` (`runtime_factory.py`) construye un único `Clock`
  (`SystemClock()` por defecto) y lo pasa a los cuatro componentes —
  wiring centralizado, no configuración por componente.
- Cero regresión verificada explícitamente: sin configurar un reloj,
  comportamiento idéntico a hoy en los 533 tests. Un test combinado
  (`test_single_fixed_clock_drives_every_timing_decision_consistently`)
  prueba que un único `FixedClock` avanzado una vez es observado
  consistentemente por expiración de plan, scheduling, timing de
  ejecución y staleness de estado a la vez — durante su desarrollo se
  detectó y corrigió un fallo real de wiring: si solo se inyecta el
  reloj en el `Scheduler` pero no en el `PlanExecutor` que este usa
  internamente, el executor sigue comparando contra tiempo real,
  confirmando por qué la spec documenta el wiring correcto como
  responsabilidad del caller en vez de intentar prevenirlo
  programáticamente.
- Fuera de alcance, con justificación explícita en el spec: timestamps
  de telemetría de adaptador (`received_at`/`observed_at` en
  `StateSnapshot`, `Measurement`, muestras KNX/Modbus/Matter/
  Zigbee2MQTT) — representan momentos de observación de eventos
  reales del mundo externo, no decisiones del runtime, y siguen en
  tiempo real. El `now: Callable[[], datetime]` ya existente en
  `ComposedEnergyContextProvider` — vive en el optimizador, tiene su
  propio punto de inyección, y no se migra en esta spec. Replay
  determinista, digital twin, HIL y shadow mode en sí mismos — esta
  spec solo entrega la base de control de tiempo que esos ítems
  futuros de P4 necesitarán; ninguno se empieza a implementar aquí.

Evidencia: `528 → 533 passed, 8 skipped` (5 tests nuevos, más 2 de
`test_clock.py` ya contados en el salto previo de 526→528). Ruff y
mypy limpios (94 ficheros fuente). Sin cambio de schema — dependencia
interna del runtime, sin contrato público afectado.

## Replay determinista de planes (2026-08-19)

Segundo ítem de P4, elegido por el usuario tras completar la Spec 047
(virtual clock). Depurar la ejecución de un plan concreto exigía hoy
reproducirlo en vivo a una hora de reloj no controlada, o leer eventos
resumidos del audit trail (`plan_validated`, `command_execution_outcome`,
`plan_execution_completed`) que capturan estado/contadores pero no
bastan para volver a ejecutar el mismo plan. `PlanRepository` ya
persiste el `Plan` completo; la Spec 047 ya añadió `Clock`. Nada
combinaba ambos.

- Nuevo `PlanReplayer` (`src/domoai/runtime/replay.py`) y `ReplayResult`
  (estado repetido, `outcomes` por comando, notas de reconstrucción
  incompleta). `replay(plan_id)` carga el plan persistido, construye un
  `DeviceRegistry`/`StateStore`/`PlanService`/`PlanExecutor` totalmente
  aislados, y re-valida + re-ejecuta contra un `FixedClock` anclado al
  propio `execute_at`/`created_at` del plan — el resultado no depende
  de cuándo se pide el replay.
- **Corrección real de diseño durante la implementación**: el plan
  original proponía reconstruir el registry vía
  `DeviceRegistry.load_persisted(...)` (igual que `build_runtime`).
  La implementación descubrió, leyendo el propio docstring del método,
  que `load_persisted` deja las rutas de comando (`_routes`) vacías a
  propósito — solo `apply_snapshot` (un discovery real) las reconstruye
  — así que cualquier comando replayado habría fallado siempre con
  `route_not_found`, sin relación con si el dispositivo "existía".
  `build_runtime` nunca lo sufre porque siempre encadena
  `discovery.refresh()` contra el adapter EN VIVO justo después. Como
  el replay no puede tocar nunca un adapter real (FR-003), la
  corrección fue construir el registry vía `DiscoveryService` contra
  un `SimulatedHomeAdapter` fresco — el mismo mecanismo, pero apuntado
  al fixture determinista. Consecuencia asumida explícitamente: el
  replay opera dentro del universo de dispositivos del fixture, no
  necesariamente los dispositivos de producción originales del plan.
- Por eso el reporte de reconstrucción incompleta (`incomplete_reconstruction_notes`)
  es más importante de lo previsto: cualquier dispositivo del plan que
  no exista en el fixture se reporta explícitamente, en vez de fallar
  en silencio o fingir fidelidad que no existe.
- Aislamiento verificado como propiedad dura, no best-effort: el
  adapter, `AuditLog`, `PolicyEngine`, registry y state store del
  replay son instancias nuevas y locales a cada llamada — nunca
  comparten estado con el runtime en vivo, y `replay()` nunca llama a
  `save()` en ningún repositorio. Tres tests dedicados prueban que el
  adapter en vivo no recibe llamadas, el state store en vivo no
  cambia, y el plan persistido no se modifica.
- Replay repetible: llamar `replay()` dos veces sobre el mismo plan
  produce resultados idénticos — verificado directamente por test.
- Fuera de alcance, con justificación explícita en el spec: exposición
  vía tool MCP o CLI (esta spec entrega solo el mecanismo subyacente,
  igual que la Spec 047 no añadió CLI para `Clock`); historial de
  estado por-punto-en-el-tiempo (no existe tal histórico en la capa de
  persistencia actual — los snapshots se sobrescriben, no se
  versionan); replay masivo o programado.

Evidencia: `533 → 541 passed, 8 skipped` (8 tests nuevos). Ruff y
mypy limpios (95 ficheros fuente). Sin cambio de schema — nuevo
módulo interno del runtime, sin contrato público nuevo.

## Comparación shadow de adapters candidatos (2026-08-19)

Tercer ítem de P4, elegido por el usuario ("comparación de adapters
candidatos") tras cerrar la Spec 048. Migrar de un adapter en
producción a un candidato (p. ej. `HomeAssistantAdapter` clásico →
`HomeAssistantProviderAdapter`, ya existente como switch opt-in
`DOMOAI_HOME_ASSISTANT_PROVIDER`) exigía confiar en el candidato a
ciegas, sin forma de comparar qué observa antes de comprometerse al
cambio.

- Restricción de seguridad real que delimita el alcance:
  `AdapterPort.discover()` es una llamada de observación pura, sin
  efectos secundarios; `AdapterPort.execute()` envía comandos reales
  a dispositivos reales. Ejecutar el candidato en "shadow" junto al
  adapter de producción habría significado doble-ejecutar cada comando
  real contra el mismo hardware real — no es un shadow deployment
  seguro, es uno activamente peligroso. Por eso la comparación cubre
  SOLO el camino de lectura/discovery, nunca ejecución — límite
  permanente, no solo alcance de esta iteración.
- Nuevo `ShadowComparator`/`ShadowComparisonResult`/`EntityComparison`
  (`src/domoai/runtime/shadow.py`): `compare(production, candidate)`
  llama `discover()` en ambos y correlaciona entidades por su
  `entity_id` crudo (el identificador que el sistema real subyacente
  asigna, presente igual en el snapshot de cualquier adapter que lo
  observe) — clasifica cada una como `only_production`,
  `only_candidate`, `matches` o `disagrees` (comparando `semantic_type`
  entre ambos lados).
- **Corrección real de diseño encontrada durante la implementación**:
  el plan original proponía reutilizar
  `DeviceRegistry.apply_snapshot` (la misma máquina de fusión
  multi-adapter que usa `CompositeAdapter` en producción) para fusionar
  ambas observaciones en un `Device` canónico con dos `source_refs`.
  El primer test escrito reveló que NO fusiona: `SourceIdentity.identity_key`
  namespacea por `adapter_id` en todos los casos salvo que la entidad
  cruda declare un `canonical_id` explícito — algo que dos adapters
  independientes no tienen motivo para acordar de antemano. Confirmado
  ejecutando el test: la MISMA entidad observada por dos adapter_ids
  distintos producía dos `Device`s separados con sufijo `-2`, no uno
  fusionado. Corregido comparando directamente por `entity_id` crudo de
  cada `AdapterSnapshot.source_entities`, sin pasar por `DeviceRegistry`
  en absoluto — más simple que el plan original y, según lo
  investigado, el único enfoque que clasifica correctamente.
- **Hallazgo colateral, no de esta spec pero corregido en el mismo
  ciclo de verificación**: el test combinado de la Spec 047
  (`test_single_fixed_clock_drives_every_timing_decision_consistently`)
  empezó a fallar durante la verificación de esta spec — no por nada
  de la Spec 049, sino porque ese test dependía de que
  `DiscoveryService.refresh()` marcara `received_at` con el reloj
  inyectado, cuando en realidad `DiscoveryService` usa
  `datetime.now(UTC)` real (fuera del alcance declarado de la Spec 047,
  que cubría solo cinco decisiones concretas, no cada timestamp).
  El test era una bomba de tiempo: pasaba por coincidencia mientras la
  hora real de sesión quedaba antes del reloj fijo usado en el test, y
  dejó de pasar cuando la sesión avanzó lo suficiente. Corregido en el
  propio test guardando un snapshot manualmente con `received_at`
  anclado al reloj (no vía discovery), sin tocar código de producción.
- Aislamiento verificado como propiedad dura: cero comandos emitidos a
  ningún adapter (test dedicado), registry/state store en vivo sin
  cambios.
- Fuera de alcance, con justificación explícita en el spec: cualquier
  comparación de ejecución de comandos entre adapters (inseguro por
  construcción, no solo fuera de alcance); cutover/migración
  automatizada (esta feature produce evidencia, el cambio real sigue
  siendo un paso manual existente); nuevo mecanismo de identidad
  cross-adapter.

Evidencia: `541 → 547 passed, 8 skipped` (6 tests nuevos). Ruff y mypy
limpios (96 ficheros fuente). Sin cambio de schema.

## Digital twin para preview de planes (2026-08-19)

Cuarto ítem de P4, elegido por el usuario tras cerrar la Spec 049 — el
usuario escogió explícitamente la opción más avanzada ("el que sea más
avanzado y completo") tras advertírsele que requería diseño nuevo, no
solo reutilizar la Spec 048. La Spec 048 ya probó que la re-ejecución
aislada funciona, pero su registry siempre se construye vía discovery
contra el universo propio del fixture `SimulatedHomeAdapter`, nunca
contra los dispositivos reales — así que previsualizar qué haría un
plan contra el estado real actual de la casa no era posible.

- Nuevo `DigitalTwin`/`TwinSyncReport` (`src/domoai/runtime/twin.py`):
  `sync(live_registry, live_state_store)` lee los `Device`s y
  `StateSnapshot`s actuales del runtime en vivo, construye un
  `SimulatedHomeAdapter` sembrado con esos datos reales (bajo los
  mismos `device_id` reales, sin traducción), y reconstruye un
  registry propio y aislado vía `DiscoveryService.refresh()` — mismo
  patrón exacto de construcción aislada que `PlanReplayer` (Spec 048),
  ahora sembrado con datos reales en vez de un fixture enlatado.
  `validate_and_execute(plan)` valida y ejecuta un plan contra ese
  registry aislado, reutilizando `ExecutionSummary` sin modelo
  paralelo.
- **La pieza genuinamente nueva de esta spec**: el mapeo inverso
  `Device` + `StateSnapshot`s actuales → diccionario de entidad cruda
  que `HomeAssistantMapper`/`SimulatedHomeAdapter` puedan consumir —
  no existía en el repositorio. Alcance acotado honestamente a los
  tres dominios (`light`, `switch`, `cover`) para los que
  `SimulatedHomeAdapter._apply_command` ya implementa efectos
  simulados reales (`turn_on`/`turn_off`/`toggle`→`power`,
  `set_brightness`→`brightness`, `set_position`/`open`/`close`→
  `position`) — reflejar un dispositivo `climate`/`sensor` habría
  producido una entidad que el mapper acepta pero cuyos comandos no
  tendrían ningún efecto simulado real, un preview que parece
  funcionar sin hacer nada. La identidad se preserva usando
  `device.id` explícitamente como `entity_id`/`device_id` de la
  entidad cruda (no se re-deriva del nombre/área, que podría no
  reproducir el id original).
- Todo dispositivo no representable (tipo fuera de
  `light`/`switch`/`cover`, o con capacidades que no coinciden con la
  forma esperada) se reporta explícitamente en
  `TwinSyncReport.not_mirrored` con motivo — nunca se omite en
  silencio ni rompe el sync completo; el resto de dispositivos
  representables se sincronizan igual.
- Aislamiento verificado como propiedad dura, con tests dedicados:
  cero comandos emitidos al adapter en vivo, registry/state store en
  vivo sin cambios tras sync+preview; preview contra un twin nunca
  sincronizado maneja el caso sin fallar (dispositivo no encontrado,
  camino ya existente y probado de `PlanService`).
- Fuera de alcance, con justificación explícita en el spec: sync
  continuo en vivo (el twin es una foto fija por-demanda, no una
  réplica actualizada automáticamente); predicción de efectos físicos
  reales más allá de lo que el simulador determinista ya modela;
  dispositivos `climate`/`sensor`/`energy`/`ev_charger` — no
  previsualizables con esta pieza.

Evidencia: `547 → 555 passed, 8 skipped` (8 tests nuevos, todos
pasando en el primer intento). Ruff y mypy limpios (97 ficheros
fuente). Sin cambio de schema.

## Verificación HIL de ejecución de comandos (2026-08-19)

Quinto ítem de P4. Tras descartar "certificación de compatibilidad de
agentes" por solaparse demasiado con los tests de contrato MCP ya
existentes desde la Spec 021, el usuario eligió HIL. Verificado antes
de escribir el spec: no hay hardware real disponible esta sesión
(`dev/lab/.env` sin `DOMOAI_KNX_GATEWAY_HOST`, el propio README del
laboratorio declara que no simula KNX/ETS ni validación con hardware
físico). Pero el laboratorio Home Assistant real (contenedor Docker
separado, `dev/lab/compose.yaml`) SÍ se pudo levantar en esta sesión —
`uv run domoai-lab up --services mqtt zigbee2mqtt modbus homeassistant`
seguido de `curl http://127.0.0.1:8123` devolviendo `200` — y expone
`switch.virtual_living_room_switch`, un switch virtual sin
electrodoméstico real detrás, definido específicamente para este tipo
de prueba en `dev/lab/homeassistant/configuration.yaml`.

Gap real verificado: el smoke test en vivo ya existente
(`tests/integration/test_home_assistant_provider_smoke.py`) solo
ejercita discovery y lectura de estado contra el sistema real — nunca
emite un comando. Nada en el repositorio probaba que el pipeline
completo (`build_runtime` → `PlanService.validate` →
`PlanExecutor.execute` → comando real → lectura de estado real →
confirmación de postcondition) funcionara de extremo a extremo contra
un sistema real y separado, no un fixture en proceso. El `smoke`
determinista de `domoai-lab` excluye deliberadamente todas las
variables `DOMOAI_*` (para quedar local y determinista), así que
estructuralmente nunca puede cerrar este hueco tampoco.

- Nuevo test opt-in
  `tests/integration/test_home_assistant_provider_hil_smoke.py` —
  ningún módulo nuevo en `src/`, ya que `build_runtime`/`PlanService`/
  `PlanExecutor`/`HomeAssistantProviderAdapter` ya proveen todo lo
  necesario; esta spec compone piezas ya probadas, no introduce un
  mecanismo nuevo (a diferencia de las Specs 047-050).
- Mismo patrón de skip que el smoke test de solo-lectura ya existente:
  sin `DOMOAI_HOME_ASSISTANT_URL`/`DOMOAI_HOME_ASSISTANT_TOKEN`, se
  salta con motivo claro — nunca rompe el resto de la suite.
- Envía `turn_on` a `switch.virtual_living_room_switch` a través del
  pipeline exacto (`validate_plan`/`execute_plan` de `DomoticsFacade`,
  sin atajos), confirma `CONFIRMED_SUCCESS` +
  `after_state.value is True`, y hace una RE-LECTURA independiente
  (`adapter.read_state`, no reutilizando el `after_state` de la propia
  ejecución) para confirmar que el cambio es observable de verdad
  desde el sistema en vivo, no solo aseverado por la contabilidad
  interna del runtime. Restaura `off` en un bloque `finally`
  incondicional, sin importar el resultado de las aserciones
  anteriores — repetible y seguro sin reset manual.
- **Ejecutado de verdad contra el laboratorio en esta misma sesión**,
  no solo escrito: `uv run pytest
  tests/integration/test_home_assistant_provider_hil_smoke.py -v`
  pasó en vivo (`1 passed in 4.54s`), y se confirmó por separado vía
  `curl .../api/states/switch.virtual_living_room_switch` que el
  switch terminó en `off`. También se confirmó el comportamiento de
  skip corriendo el mismo test sin las variables de entorno.

Evidencia: `555 → 555 passed, 8 → 9 skipped` (sin credenciales en el
entorno por defecto de la suite completa, el nuevo test se salta —
correcto y esperado; se ejecutó por separado en vivo con credenciales,
pasando). Ruff y mypy limpios (97 ficheros fuente, sin cambio en
`src/`). Fuera de alcance, con justificación explícita en el spec:
hardware físico/KNX real (no disponible esta sesión, queda para
cuando haya gateway confirmado); ejecución automática en CI por
defecto (opt-in, mismo criterio que el smoke test de solo-lectura ya
existente).

## Análisis contrafactual del optimizador (2026-08-19)

Sexto y, según el usuario, último ítem acotado de P4 esta sesión.
`OptimizationScenario`/`EnergyContext` (Specs 042-046) ya son modelos
Pydantic inmutables que los propios tests de esta sesión variaban a
mano vía `model_copy(update=...)` para comparar resultados (las Specs
044/045/046 lo hicieron cada una por separado, código repetido en cada
archivo de test) — sin ningún mecanismo reutilizable para correr un
escenario base más variaciones nombradas y obtener una comparación
estructurada y determinista.

- Nuevo `CounterfactualAnalyzer`/`CounterfactualResult`/
  `VariationOutcome` (`src/domoai/optimizer/counterfactual.py`) —
  vive en `src/domoai/optimizer/`, no en `src/domoai/runtime/` como
  las Specs 047-051, porque es un concepto del dominio del
  optimizador, no del runtime: `OptimizerPort.optimize()` es síncrono
  y puro, sin adapter, sin hardware, sin ningún límite de aislamiento
  que gestionar (confirmado por grounding antes de escribir el spec).
- `compare(baseline, variations)` resuelve el baseline y cada
  variación con el mismo `OptimizerPort` inyectado por quien llama
  (nunca construye el suyo propio), y calcula el diff por clave SOLO
  sobre la intersección de claves de `objective_values` presentes en
  ambos lados — nunca inventa un valor `0.0` para una clave que un
  lado no reportó, la misma disciplina de honestidad que ya se aplica
  a resultados infactibles completos, ahora aplicada por-clave.
- Cuando una variación resulta `INFEASIBLE`/`INVALID`/`TIMEOUT`/
  `UNKNOWN`, su `diff` es `{}` — nunca una comparación numérica
  fabricada; cuando el propio baseline no es factible, NINGUNA
  variación se llega a resolver (`variations == {}` directamente, sin
  gastar cómputo en escenarios cuya comparación no tendría sentido).
- Reutiliza `OptimizationResult` completo (no un modelo paralelo
  recortado) dentro de `VariationOutcome` — mismo patrón "no inventar
  un segundo modelo" ya aplicado en las Specs 048/049.
- No hace copia defensiva de los escenarios antes de pasarlos al
  optimizador (los `StrictModel` de este proyecto ya son
  efectivamente inmutables por convención, y `CpSatOptimizer.optimize`
  solo lee) — probado explícitamente con un test de no-mutación en vez
  de añadir una copia redundante sin necesidad real detrás.

Evidencia: `555 → 563 passed, 9 skipped` (8 tests nuevos, todos
pasando en el primer intento tras un ajuste de datos de prueba: la
primera versión de los escenarios "infactibles" resultó factible por
tener solar suficiente para cubrir la carga incluso con
`max_grid_import=0`, corregido poniendo `solar_power_kw=0.0` también
en esos casos — ajuste de fixtures de test, no un hallazgo de diseño
del propio mecanismo). Ruff y mypy limpios (98 ficheros fuente). Sin
cambio de schema. Fuera de alcance, con justificación explícita en el
spec: historial persistido de comparaciones; análisis estadístico o
causal más allá de la resta simple por-variación; optimización por
lotes de las N resoluciones del solver. **Cierra P4 acotado para esta
sesión**: seis ítems (047-052).

## Verificación HIL de ejecución de comandos contra KNX real (2026-08-19)

Hermana directa de la Spec 051 (mismo concepto HIL de P4, sistema real
distinto): tras cerrar P4 con seis ítems, el usuario abrió KNX
Virtual/ETS en Windows y pidió probar HIL contra hardware real — el
bloqueo documentado tras la Spec 051 ("sin gateway KNX configurado")
era un hecho de estado de sesión, no una limitación estructural del
código: el adapter, transporte y mapping de KNX ya existían y ya
estaban validados en modo solo-lectura (`dev/lab/knx-virtual.md`,
entrada del 2026-08-18). El smoke test en vivo ya existente
(`tests/integration/test_knx_smoke.py`) es explícitamente solo-lectura
— afirma `transport.writes == []` y solo ejercita `discover()`. Nada
probaba que un comando real se ejecutara de extremo a extremo contra
el bus KNX real.

- Verificado primero con un script suelto (no persistido) antes de
  formalizar: `build_runtime(Settings(knx_gateway_host="172.26.80.1",
  knx_config_path="dev/lab/configs/knx-virtual.json", ...))`,
  `turn_on` real sobre `living_room.main_light` vía
  `DomoticsFacade.validate_plan`/`execute_plan`,
  `CONFIRMED_SUCCESS` + `after_state.value is True` — escritura real
  al bus KNX real, confirmada por lectura de postcondition real,
  restaurado a `off` después. `172.26.80.1` es la misma IP ya validada
  el 2026-08-18.
- Formalizado como spec permanente en
  `tests/integration/test_knx_hil_smoke.py`, mismo patrón exacto que
  la Spec 051 (`test_home_assistant_provider_hil_smoke.py`) — mismo
  gate de skip, misma estructura `try`/`finally` de restauración.
  Ningún módulo nuevo en `src/` — compone piezas ya probadas.
- **Única decisión de diseño genuinamente nueva** (todo lo demás es
  reutilización directa de la Spec 051): el objetivo se acota
  explícitamente a `living_room.main_light`'s capability `power` —
  NUNCA `brightness` ni `living_room.temperature`, aunque ambos están
  declarados en el mapping. Motivo verificado durante la propia
  ejecución en vivo: `GroupValueRead` dio timeout en `1/0/3`
  (brightness) y `1/1/0` (temperatura) durante discovery, mientras que
  el round-trip escritura+lectura de `power` (`1/0/0`/`1/0/1`)
  funcionó limpio — casi seguro porque el proyecto de laboratorio de
  KNX Virtual solo tiene el on/off de la luz realmente cableado para
  responder, no un defecto de DomoAI. Acotar a la única capability ya
  confirmada fiable mantiene la verificación como señal de confianza:
  un fallo aquí significa una regresión real, no ruido de una entidad
  de laboratorio nunca completamente cableada.
- Una segunda verificación de lectura independiente y suelta (fuera
  del propio test) mostró timeouts intermitentes al reconectar y leer
  inmediatamente sin una escritura previa en la misma sesión — mismo
  patrón de flakiness que el smoke test de solo-lectura original ya
  documentaba para otras entidades. El test formal en sí (que sí
  incluye su propia relectura independiente inmediatamente después de
  la escritura, dentro de la misma conexión) pasó limpio dos veces
  seguidas. Documentado en research.md como una decisión explícita:
  la relectura independiente reutiliza `device.source_refs` ya
  resueltos por `build_runtime`, nunca un `SourceRef` construido a
  mano.

Evidencia: `563 → 563 passed, 9 → 10 skipped` (sin configuración KNX
en el entorno por defecto de la suite completa, el nuevo test se
salta — correcto y esperado; ejecutado por separado en vivo contra
KNX Virtual/ETS real, pasando). Ruff y mypy limpios (98 ficheros
fuente, sin cambio en `src/`). Fuera de alcance, con justificación
explícita en el spec: otras entidades declaradas en el mapping
(`brightness`, `temperature`); hardware KNX físico no virtual (no
disponible, solo KNX Virtual/ETS esta sesión); ejecución automática en
CI por defecto.

## Certificación de compatibilidad de agentes a nivel de protocolo MCP (2026-08-19)

Forma corregida y no redundante del ítem "certificación de agentes"
descartado durante la selección de P4 de esta sesión por presunto
solape con los tests MCP existentes — esa premisa era incorrecta: los
tests MCP existentes (`tests/contract/test_domotics_mcp_contract.py`,
`test_unified_mcp_contract.py`, `tests/integration/test_mcp_client_parity.py`,
etc.) llaman a `FastMCP.call_tool(...)` directamente en proceso sobre
el mismo objeto Python, o como mucho envuelven `ClientSession` sobre
streams `anyio` en memoria conectados al mismo proceso — nunca prueban
el protocolo JSON-RPC-sobre-stdio real que un agente externo genuino
(Claude Desktop, cualquier cliente compatible con el estándar MCP)
usaría para conectarse. El punto de entrada de producción real
(`domoai-mcp`, script de consola en `pyproject.toml`, que invoca
`domoai.mcp.stdio:main` → `run_stdio()`) nunca se ejercitaba de
extremo a extremo en ningún test.

Nuevo `tests/contract/test_mcp_protocol_certification.py`: lanza el
script `domoai-mcp` real como subproceso (resuelto vía `shutil.which`,
no una ruta hardcodeada), con `DOMOAI_DATABASE_PATH` aislado en
`tmp_path` y sin variables de entorno de integración externa
(`create_adapter` recae automáticamente en `SimulatedHomeAdapter` al
no haber ninguna configurada, confirmado leyendo
`runtime_factory.py` — sin necesidad de opt-in, a diferencia de las
Specs 051/053), y se conecta usando exclusivamente el SDK cliente
público de `mcp` (`mcp.client.stdio.stdio_client` +
`mcp.client.session.ClientSession`, ya dependencia del proyecto,
`mcp>=1.27,<2`) — nunca importando módulos servidor de
`domoai.mcp.*` para actuar de cliente. Completa el handshake
`initialize`, lista el catálogo de herramientas vía `list_tools()`,
y llama `discover_devices`/`validate_command` (solo validación, sin
ejecución real) a través del protocolo de cable real.

Sin corrección de diseño necesaria: la firma exacta del SDK cliente
(`StdioServerParameters`, `ClientSession.initialize/list_tools/call_tool`)
se confirmó vía `inspect.signature` contra el paquete instalado antes
de escribir el test, no se asumió. Único ajuste durante la
implementación: `mcp.client.stdio.stdio_client` fusiona automáticamente
`get_default_environment()` (solo `HOME`/`LOGNAME`/`PATH`/`SHELL`/`TERM`/`USER`)
con el `env` explícito pasado — no hace falta reenviar `PATH` a mano.

Evidencia: `563 → 564 passed, 10 skipped` (sin salto — corre
incondicionalmente, sin gating de opt-in, por no depender de ningún
sistema externo). Test individual: `1 passed in ~11-16s` (dentro del
objetivo de <15s del spec, con variación normal de carga de
máquina). Ruff y mypy limpios (99 ficheros fuente). Sin cambio de
schema.

Fuera de alcance, con justificación explícita en el spec: certificar
contra cada producto de agente individualmente (se certifica contra el
protocolo estándar en sí, vía el SDK de referencia, como proxy
alcanzable y significativo); re-certificar cada herramienta del
servidor a nivel de cable (solo se ejercitan `discover_devices`/
`validate_command` como muestra representativa; el resto ya está
cubierto a nivel Python por los tests de contrato existentes).

## Orquestación del skill de energía

El skill portable `optimize-home-energy` declara la secuencia y el rol de la
única conexión MCP general, sin fijar nombres de servidores:

| Operación | Rol | Tool | Modo |
| --- | --- | --- | --- |
| `discover_devices` | `mcp` | `discover_devices` | lectura |
| `get_state` | `mcp` | `get_state` | lectura |
| `get_energy_context` | `mcp` | `get_energy_context` | lectura |
| `optimize_scenario` | `mcp` | `optimize_scenario` | propuesta |
| `validate_plan` | `mcp` | `validate_plan` | validación |
| `explain_solution` | `mcp` | `explain_solution` | lectura |
| `operator_approval` | `operator` | `request_approval` | aprobación |
| `execute_plan` | `mcp` | `execute_plan` | mutación |

El workflow usa una sola conexión MCP mediante un puerto semántico inyectado.
En v2 lee `get_energy_context` antes de construir la propuesta y detiene el
flujo si falta el contexto o su revisión no coincide. No es un MCP adicional,
no llama adapters ni `OptimizerPort` directamente y no
autoriza su propio plan. La validación final, revisión, digest, aprobación y
postcondiciones permanecen en el Domotics Runtime. Las entradas desconocidas,
estado obsoleto, propuestas no factibles, cambios de revisión/digest y
respuestas malformadas detienen el workflow antes de ejecutar.

Para proteger una reanudación sin añadir una tool MCP, el router del host
expone además la revisión runtime actual como metadato de solo lectura. El
workflow la compara con la revisión validada antes de solicitar `execute_plan`.

El adapter nativo Zigbee2MQTT mantiene el mismo contrato `AdapterPort` y no
añade herramientas MCP. Su contrato de topics, mapeo de capacidades,
conversión de brillo, disponibilidad e idempotencia está cubierto por los
tests de contrato.

El adapter nativo Matter Server mantiene el mismo contrato y consume snapshots
de nodos/endpoints y eventos por WebSocket. Su perfil de schema 13, mapeo de
clusters, comandos acotados, compatibilidad y límites de seguridad está
documentado en los schemas y tests de contrato del adapter.

El adapter nativo KNX mantiene `AdapterPort` y usa un mapping local estricto de
entidades, direcciones de grupo y DPTs. Su contrato v1 cubre `1.001` para power,
`5.001` para brightness, `9.001` para temperature, `9.007` para humidity y
`1.018` para occupancy. El transporte live basado en xknx queda aislado detrás
de `KnxTransport`; los tests usan `InMemoryKnxTransport`.

KNX no añade tools MCP ni permite que el agente envíe group-value reads/writes
arbitrarios. Los telegramas de direcciones no configuradas son diagnósticos, y
las mutaciones pasan por `PlanService`, `PolicyEngine`, `PlanExecutor`,
idempotencia y postcondición de readback. La especificación y el formato de
mapping están cubiertos por los schemas y tests de contrato del adapter.

El adapter nativo Modbus mantiene `AdapterPort` y usa un mapping local estricto
de unit IDs, áreas, offsets PDU y codecs. Su perfil v1 cubre coils/discrete
inputs para booleanos, holding/input registers para números, y las capacidades
semánticas de power, brightness, temperature, humidity y occupancy. El polling
se realiza dentro de `subscribe_events()` y solo emite cambios o diagnósticos;
el transporte live basado en PyModbus queda aislado detrás de
`ModbusTransport`, mientras los tests usan `InMemoryModbusTransport`.

Modbus no añade tools MCP ni permite al agente solicitar registros o function
codes arbitrarios. Las escrituras solo pueden llegar a coils de power o
holding registers de brightness declarados, después de pasar por
`PlanService`, `PolicyEngine`, `PlanExecutor`, idempotencia y postcondición de
readback. La especificación, el mapping y el contrato están cubiertos por los
schemas y tests de contrato del adapter.

## Composición de SOC reconciliado desde StateStore (Spec 104)

`StateStoreBatteryProvider` conecta explícitamente un snapshot normalizado del
cache de `StateStore` con el provider de energía:

```text
StateStore.peek(device, capability)
        ↓
Measurement canónica
        ↓
BatterySocObservation con provenance
        ↓
BatteryProfile validado
        ↓
BatteryState → EnergyContext → MCP
```

La consulta es síncrona, read-only y no llama al adapter ni a SQLite. Un
snapshot `kWh` se conserva directamente; un snapshot `%` solo se convierte
con `BatteryCapacityEvidence` explícita, positiva, `GOOD` y ligada al mismo
provider y dispositivo. `STALE` conserva calidad degradada y no puede activar
un perfil despachable; `UNAVAILABLE`, `INVALID`, identidades cruzadas, valores
no numéricos o unidades no soportadas fallan cerrado con diagnósticos
sanitizados.

El provider no se auto-instala en `build_runtime`: todavía no existe una
configuración segura y completa de binding de perfil, actuador y capacidad
nominal para habilitar batería live. Esta frontera evita convertir una entidad
ambigua de Home Assistant u otro proveedor en autorización de despacho.

## Lifecycle durable de planes MCP (Spec 115)

En el runtime configurado, `validate_plan` y `validate_command` persisten el
plan validado en `PlanRepository`. Un contexto MCP nuevo resuelve primero el
cache en memoria y, si no encuentra el ID, consulta SQLite; el cuerpo completo
del plan, su digest, dependencias, expiry y estado se recuperan desde el
registro durable. Los contextos de fixtures sin repositorio siguen siendo
deliberadamente process-local.

La validación sin `expires_at` recibe un TTL finito del servidor. Para planes
con `execute_at` futuro, el TTL cubre la ventana hasta esa ejecución; al llegar
la hora, el executor vuelve a comprobar expiry, dependencias, policy, rutas,
capability fingerprint y preflight de seguridad antes de cualquier write.

La aprobación no se reconstruye desde la proyección del plan tras restart: los
grants siguen siendo single-use y se consultan en el repository durable de
autoridad. La ruta MCP legacy usa un bearer token únicamente
si `DOMOAI_ALLOW_LEGACY_OPERATOR_TOKEN=1` está configurado, y queda destinada a
instalaciones locales/dev. El runtime también expone `OperatorPrincipal` para
que una UI/host autenticado emita grants sin introducir credenciales en el
schema consumido por el agente. La autenticación humana real sigue siendo una
responsabilidad del host confiable.

`schedule_plan` y `cancel_scheduled_plan` actualizan tanto la cola de
scheduling como `PlanRepository`; cancelar también deja el plan en estado
terminal y bloquea una ejecución directa posterior. `reschedule_plan` no
reescribe una intención aprobada: devuelve
`reschedule_requires_revalidation`, audita la decisión y conserva la fila
pendiente. La reconciliación entre schedule y execution permanece autoritativa
en los repositories y no reconstruye efectos físicos después de un crash.

## Gobernanza durable de audit y release (B09, 2026-08-30)

La autoridad y la auditoría usan lanes de cola/worker independientes y, por
defecto, ficheros SQLite distintos (`domoai.sqlite3` y
`domoai-audit.sqlite3`). `Settings` rechaza una configuración explícita que
resuelva ambos paths al mismo fichero; así la promesa de aislamiento no
depende solo de que el operador conozca el valor por defecto.

Los overrides normales de riesgo son monotónicos: solo pueden mantener o
endurecer la clasificación server-owned. El loader TOML ordinario no puede
activar una reducción privilegiada. La reducción, si una futura instalación la
necesita, debe introducir un contrato de excepción explícito, auditable y
revisable; no debe reutilizar el fichero de hardening normal.

`AuditEventRepository.list_events()` exige `limit >= 1` antes de construir la
consulta y aplica el máximo server-owned de 500. La tool MCP propaga el mismo
rechazo y nunca convierte un límite negativo en `LIMIT -1` de SQLite.

La workflow CI ejecuta el conjunto completo de gates y `Required release gates`
los agrega con semántica fail-closed. La protección estricta de `main` exige
los 15 contexts documentados en [`docs/release-governance.md`](release-governance.md).
El artifact `ci-evidence-${sha}` conserva resultados ligados al SHA; el README
no afirma un número fijo de tests.

## Versionado

- Añadir campos opcionales o nuevos valores documentados puede entrar en una
  versión menor compatible, junto con pruebas y schemas regenerados.
- Cambiar el significado, tipo, unidad, obligatoriedad o semántica de un
  campo requiere una nueva versión mayor (`v2`) y una estrategia de migración.
- No se deben sobrescribir schemas históricos: cada versión vive en su propio
  directorio (`schemas/v1`, `schemas/v2`, ...).
- Toda modificación de contrato debe actualizar `spec.md`, `plan.md`,
  `tasks.md`, pruebas de contrato y esta documentación.

## Verificación

La validación local completa está descrita en el README y en los tests del
repositorio.
Como mínimo, antes de publicar un cambio de contrato se ejecutan:

```bash
uv run pytest -q tests/contract
uv run python scripts/export_schemas.py
uv run ruff check .
uv run mypy src tests scripts
```
