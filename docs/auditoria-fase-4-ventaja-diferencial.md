# Auditoría Fase 4 — Ventaja diferencial

**Objetivo:** convertir DomoAI en la plataforma de domótica agentic más completa mediante capacidades difíciles de copiar: abstracción universal, optimización explicable, commissioning verificable, aprendizaje seguro y operación local.

**Naturaleza:** evolución de producto e I+D; no debe adelantarse a las gates de seguridad de las fases 0 y 1.

## 1. Universal Device Model como activo central

El valor más defendible de DomoAI es que el agente pueda tratar una bombilla, persiana, HVAC, batería o cargador EV de forma semántica:

```text
Device
  identity
  household / area
  source references
  capabilities
  state evidence
  commands
  safety constraints
  quality / freshness
```

La abstracción debe evolucionar desde “qué comandos existen” hacia “qué garantías ofrece cada capability”:

- rango mínimo/máximo;
- unidad y resolución;
- latencia esperada;
- readback requerido;
- tolerancia de error;
- operación reversible o irreversible;
- necesidad de confirmation;
- disponibilidad local/remota;
- evidencia de commissioning.

Así el agente no solo sabrá que puede poner una temperatura: sabrá si la acción es segura, verificable y suficientemente fresca.

## 2. Adaptador universal y conectores internos

Home Assistant funciona como conector inicial y reduce drásticamente el trabajo
de drivers. La evolución correcta no es crear adapters públicos por fabricante,
sino ampliar el adaptador universal con conectores internos que traduzcan
inversores, baterías, cargadores EV, HVAC, contadores, persianas y actuadores al
mismo modelo semántico. Ningún conector debe ser visible como MCP propio ni
crear una ruta paralela de autoridad.

Cada conector puede entregar `ProviderManifest`, descriptors, measurements y
commands dentro del SDK, pero siempre detrás del contrato universal y sin
importar policy ni llamar directamente al executor.

El SDK necesita además:

- contrato de capabilities versionado;
- health y degraded state;
- identity mapping estable;
- idempotency key por command;
- source sequence/cursor;
- límites declarativos;
- simulador contractual;
- qualification matrix por hardware.

## 3. Optimización energética avanzada

La propuesta actual con CP-SAT es un buen núcleo. La ventaja diferencial será combinar optimización con evidencia y explicación.

### Entradas

- tarifas reales;
- previsión solar con confianza;
- consumo histórico;
- temperatura exterior;
- ocupación consentida;
- SOC y límites físicos;
- deadlines de EV;
- confort deseado;
- potencia contratada;
- degradación y coste de batería.

### Salidas

- plan por slots;
- coste estimado;
- importación máxima;
- autoconsumo;
- restricciones activas;
- margen de incertidumbre;
- acciones no ejecutables por falta de evidencia;
- alternativas: ahorro, confort, autonomía y solar.

### Requisito de seguridad

El optimizador solo entrega una propuesta. El flujo siempre debe ser:

```text
forecast/context
      ↓
scenario DSL
      ↓
CP-SAT proposal
      ↓
explanation + validation
      ↓
policy + approval + admission
      ↓
bundle commit
      ↓
executor + readback
```

## 4. Commissioning y confianza física

Discovery nunca debe convertir automáticamente un dispositivo en una ruta escribible. Para acercarse a calidad de plataforma se necesita un flujo de commissioning:

1. descubrir candidato;
2. identificar fuente, modelo y firmware;
3. verificar capabilities observables;
4. ejecutar lecturas no destructivas;
5. probar actuador con límite seguro;
6. comprobar readback;
7. registrar evidencia y operador;
8. asignar profile y scope;
9. habilitar producción solo tras todas las gates.

Matter y KNX virtuales validan composición, pero no equivalen a commissioning físico. Baterías, EV y actuadores críticos necesitan evidencia de hardware real.

## 5. Inteligencia local y privacidad

El sistema puede ofrecer IA sin convertir la vivienda en una dependencia de la nube:

- clasificación local de intenciones;
- detección de anomalías de consumo;
- modelos de ocupación con consentimiento;
- previsión local de carga y solar;
- explicación reproducible de decisiones;
- retención configurable de histórico;
- exportación y borrado por hogar.

El aprendizaje nunca debe cambiar límites de seguridad automáticamente. Puede proponer un cambio de policy, que requiere revisión y aprobación separadas.

## 6. Automatizaciones de alto valor

Después de las Skills básicas, las experiencias que más diferenciarían DomoAI son:

- “prepara la casa para dormir manteniendo seguridad y confort”;
- “carga el coche usando solar sin bajar de la reserva de batería”;
- “reduce consumo esta semana explicando qué sacrificios hace”;
- “detecta si el HVAC está funcionando peor que su patrón normal”;
- “simula vacaciones sin abrir rutas de acceso inseguras”;
- “explica por qué la factura subió y qué acciones son reversibles”.

Todas deben producir un plan explicable y mostrar las acciones que requieren consentimiento.

## 7. Ecosistema abierto

El Adapter SDK puede convertirse en una ventaja de comunidad si se publica con:

- manifest y schemas;
- conformance suite;
- simulador local;
- ejemplos por protocolo;
- matriz de capabilities;
- política de compatibilidad;
- firma o revisión de plugins;
- documentación para fabricantes y usuarios.

El runtime debe aceptar un plugin nuevo sin permitir que el plugin introduzca una ruta paralela de autoridad.

## 8. Criterio de salida de la fase

La fase termina cuando DomoAI ofrece varias Skills agentic coordinadas, al menos un flujo energético completo con explicación y escenarios alternativos, un commissioning físico verificable y automatizaciones locales que mantengan la casa operativa sin conexión al agente. La cobertura adicional se incorpora mediante conectores internos, no mediante adapters públicos específicos.

## 9. Implementación realizada

La Fase 4 queda implementada en software en cuatro cortes conectados:

| Corte | Implementación | Evidencia |
| --- | --- | --- |
| Garantías UDM | `CapabilityGuarantees`, `AvailabilityMode` y `CommissioningRequirement` se conservan de forma aditiva en `Capability` y `CapabilityDeclaration`. `PlanService.validate()` exige postcondición verificable cuando `readback_required=true`. | `src/domoai/domain/models.py`, `src/domoai/adapters/sdk/manifest.py`, `src/domoai/application/capability_assurance.py`, `tests/unit/domain/test_capability_guarantees.py`, `tests/composition/test_phase4_product_composition.py` |
| Qualification | `CommissioningEvidence` está digestada y ligada a candidato, autoridad, timestamps y checks. `CommissioningService.verify_evidence()` es puro respecto a adapters, no crea autoridad y distingue simulación de hardware. | `src/domoai/domain/commissioning.py`, `src/domoai/application/commissioning.py`, `src/domoai/persistence/qualification.py`, `tests/unit/application/test_commissioning_qualification.py`, `tests/composition/test_phase4_commissioning_composition.py` |
| SDK cualificado | `ConformanceHarness` valida lifecycle, identidad, disponibilidad, estado, ejecución segura, readback, idempotencia y ahora `manifest_compatibility`. La comparación falla cerrado ante rango, escritura, readback o latencia observada incompatible. | `src/domoai/adapters/sdk/conformance.py`, `src/domoai/adapters/sdk/registry.py`, `tests/contract/test_phase4_provider_contract.py`, `tests/unit/adapters/test_phase4_conformance.py` |
| Producto y privacidad | `ProductSummary`/`summarize_solution` son proyecciones deterministas sin comandos. La privacidad tiene policy por hogar, redacción, exportación, borrado, auditoría mínima, migración/persistencia SQLite y tools MCP en el builder configurado. | `src/domoai/domain/product.py`, `src/domoai/optimizer/product.py`, `src/domoai/mcp/ortools_server.py`, `src/domoai/domain/privacy.py`, `src/domoai/application/privacy.py`, `src/domoai/persistence/privacy.py`, `src/domoai/mcp/domotics_server.py` |

La persistencia de qualification se ejecuta detrás de `SerializedRepositoryProxy`,
por lo que una verificación MCP puede dejar el resultado auditable sin abrir
una conexión SQLite paralela. Los schedules recurrentes se filtran por su
`authority_payload` dedicado; los rows legacy sin autoridad solo quedan
visibles en el hogar `default`, preservando la migración aditiva sin exponerlos
a hogares nombrados.

La readiness consolidada confirma que la evidencia del gemelo digital y del
laboratorio de proceso pasa, pero la qualification física/HIL sigue abierta.
Consultar [`evidence/production-readiness-latest.md`](evidence/production-readiness-latest.md)
antes de habilitar actuadores de riesgo en producción.

## 10. Contratos y schemas

Se regeneraron los schemas públicos v1: 73 documentos en `schemas/v1/`.
Los contratos de Fase 4 cubren `CommissioningCheck`, `CommissioningEvidence`,
`CommissioningQualification`, `HouseholdDataPolicy`, `PrivacyExport`,
`PrivacyDeletion`, `ProductAlternative` y `ProductSummary`, además de la
extensión de `Capability` y `AdapterManifest`.

La documentación de contrato se actualizó en:

- [`docs/contracts.md`](contracts.md): tools, garantías, qualification,
  conformance y privacidad.
- [`docs/adapter-sdk.md`](adapter-sdk.md): check
  `manifest_compatibility` y límites de qualification.
- [`docs/unified-mcp.md`](unified-mcp.md): `verify_commissioning`,
  `summarize_solution`, exportación y borrado.
- [`specs/186-phase4-ventaja-diferencial/`](../specs/186-phase4-ventaja-diferencial/):
  spec, plan, modelo, contratos, quickstart y tareas.

`uv run python scripts/check_runtime_contract_docs.py` devuelve
`runtime contract documentation is coherent`.

## 11. Evidencia ejecutable

Gates ejecutadas en este workspace:

```text
uv run ruff check src tests scripts                         PASS
uv run mypy                                                PASS (159 source files)
uv run python scripts/export_schemas.py                    PASS (72 schemas)
uv run python scripts/check_runtime_contract_docs.py        PASS
project-composition-check "$(cat .ai/project-name)"        PASS (490 passed, 18 skipped)
```

La suite de Fase 4 incluye unit, contract, integration y composition para
garantías, readback, digest/expiración, conformance, resumen, privacidad MCP,
scope entre hogares, schedules recurrentes y rows legacy. La suite completa
se ejecutó después de actualizar las listas exactas del catálogo unificado con
`summarize_solution` y terminó con `1743 passed, 18 skipped`.

Los 18 skips del check de composición corresponden a dependencias que no están
disponibles en este entorno, no a tests marcados como fallidos. En particular,
no se convierte una simulación en evidencia física.

## 12. Composition Review Report

### Subsystems changed

- Modelo canónico UDM y contratos Pydantic v1.
- Registry/routing y validación de planes.
- Commissioning, qualification y persistencia SQLite.
- Adapter SDK, registry de compatibilidad y conformance harness.
- Optimizer MCP y proyección de producto.
- Policy de privacidad, persistencia SQLite, auditoría y superficie MCP.
- Runtime factory, configuración de retención y builder configurado.

### Neighbors reviewed

Se revisaron los vecinos upstream/downstream reales: mappers y
`DeviceRegistry`, `PlanService`, `PlanDependencies`/fingerprints, executor y
admission, `CommissioningService`, `AdapterManifest`/`AdapterSnapshot`,
`OptimizationResult`, `DomoticsMcpContext`, `RuntimeComposition`,
`SerializedStorageExecutor`, repositories SQLite, `AuditLog` y los contratos
MCP unificado/OR-Tools.

### Contracts and invariants checked

- Defaults legacy de capability son compatibles y no equivalen a qualification.
- Readback requerido falla antes de generar validación ejecutable sin
  postcondición.
- Cambiar garantías cambia el fingerprint de ejecución.
- Qualification exige digest, scope, vigencia y checks completos; nunca crea
  approval, lease, route ni autoridad.
- La conformance no permite que un provider amplíe el contrato declarado y sus
  diagnósticos no contienen el payload observado.
- El resumen no contiene comandos y no invoca adapters.
- Export/delete comprueban tenant, hogar, rol y categorías; redacting elimina
  claves de credencial; el borrado nunca toca la lane de auditoría.
- La autoridad de recurrentes se lee de su columna dedicada y la persistencia
  usa la cola SQLite serializada del runtime.

### Architecture and scenarios

Import Linter pasó 4 contratos y 0 roturas. `project-composition-check` pasó
490 tests y 18 skips. Las pruebas cruzadas ejecutadas cubren:

- provider manifest → snapshot → registry → plan validation/readback;
- candidate report → evidence digest → qualification MCP sin write físico;
- conformance compatible y conformance degradada por rango observado mayor;
- optimizer result → `ProductSummary` sin llamadas al adapter;
- privacy export/delete de dos hogares, redacción y schedule recurrente;
- migración/round-trip de qualification sin secretos;
- tool catalog unificado y paridad de clientes tras añadir la nueva tool.

SQLite disposable fue utilizado para las fronteras de persistencia. Los
adapters se usan como fixtures deliberados en qualification y conformance
porque el objetivo de esas pruebas es demostrar ausencia de actuation; el
bus KNX y el HIL deben verificarse en una gate física separada.

### Failures and root causes found

La primera suite completa encontró dos listas de catálogo que no incluían la
tool nueva `summarize_solution`; se corrigieron
`tests/contract/test_unified_mcp_contract.py` y
`tests/integration/test_ortools_mcp_parity.py`. También se corrigió el filtro
de autoridad de `recurring_schedules`, que no vive dentro de
`template_payload`.

Graphify se refrescó con 7.183 nodos, 22.701 edges y 469 comunidades. Emitió
el warning conocido de 14 migrations SQL sin `tree_sitter_sql`; el análisis
Python/contratos sí terminó y la ausencia del parser SQL no cambia la
ejecución de migrations ni se trata como pass físico.

### Residual risks and verdict

- KNX Virtual/ETS/knxd está operativo en el laboratorio de proceso, pero la
  qualification de una instalación KNX física real queda
  `blocked_external_dependency`.
- No se dispone de batería/EV HIL real; las simulaciones solo prueban la
  composición y nunca producen evidencia `hardware`.
- No se ejecutó qualification de fabricante/provider contra hardware real.
- La retención es una policy configurable y su enforcement temporal/garbage
  collection queda fuera de este corte; export/delete sí aplica el scope y
  las categorías declaradas.

**Veredicto: PASS WITH RISKS.** La implementación software y sus contratos de
Fase 4 están completos y verificados; las únicas pendientes son gates físicas
externas que el código clasifica explícitamente como bloqueadas.

## 13. Gate de gemelo digital para todos los sistemas

Para que los simuladores aporten evidencia de funcionamiento del sistema y no
sean sólo fixtures aislados, se añadió una planta virtual única y determinista:

- `VirtualHomePlant` monta los simuladores existentes de batería, EV, térmico
  y agua, y mantiene además luz, interruptor, persiana, clima, ambiente,
  potencia y solar.
- `VirtualProtocolAdapter` proyecta esa misma planta por los seis perfiles que
  consume el runtime (`fixture`, Home Assistant, KNX, Modbus, Matter y
  Zigbee2MQTT). Cada perfil pasa por `DiscoveryService`, `DeviceRegistry`,
  `PlanService`, `CompositeAdapter`, `PlanExecutor`, readback y auditoría.
- El reloj virtual, la semilla, las revisiones y los digests hacen que la
  ejecución sea reproducible sin `sleep`, Docker ni hardware.
- La matriz inyecta indisponibilidad, stale, rechazo, retraso, parcialidad,
  duplicados y eventos fuera de orden; verifica límites físicos, cero writes
  inseguros, idempotencia, recuperación, scheduler, automatización,
  optimización, resumen de producto, privacidad, identidad y auditoría.

La evidencia ejecutable se guarda en
[`docs/evidence/digital-twin-latest.md`](evidence/digital-twin-latest.md) y se
regenera con:

```bash
uv run python -m domoai.lab.cli twin --seed 187 \
  --report docs/evidence/digital-twin-latest.md
```

La gate digital prueba el circuito de software y la evolución de los modelos.
Las suites específicas de adapters siguen cubriendo codecs/mappers y los
servicios virtuales de `dev/lab`; la ejecución KNX/ETS/knxd, radios,
firmware, cableado y HIL de batería/EV siguen requiriendo observaciones físicas
independientes. Un pass del gemelo nunca crea commissioning ni autoridad.

### Resultado de la gate

En esta revisión, la matriz determinista produce `passed`, cubre 6 adapters,
10 dominios y 14 checks transversales, y dos ejecuciones con semilla 187
producen el mismo `plant_digest`, `trace_digest` y JSON canónico. La prueba
está integrada en `LabRunner.smoke()` mediante
`tests/integration/test_digital_twin_matrix.py`.

El siguiente bloque de validación de proceso quedó iniciado y registrado en
[`docs/evidence/process-lab-latest.md`](evidence/process-lab-latest.md): el
smoke local pasó `80 passed, 1 skipped`, la composición multi-adapter live de
proceso pasó `7 passed` en dos repeticiones y los recorridos KNX Virtual/Home
Assistant etiquetados HIL pasaron `2 passed, 1 skipped`. Estos resultados no
se presentan como evidencia de hardware físico.

La conexión y operación desde un cliente MCP externo al proceso quedaron
registradas en [`docs/evidence/mcp-live-operation-latest.md`](evidence/mcp-live-operation-latest.md):
32 tools, 8 resources, 25 dispositivos, cinco adapters conectados, 13
comandos confirmados con readback y una ventana de estabilidad de 90 segundos.
Durante esa validación se corrigió además la recuperación de estado tras
reconexión de un child adapter; el único bloqueo sostenido observado fue la
qualification física de batería.

### Verificación reproducible del corte

El informe generado contiene `plant_digest=837c82b7d9071ba4782d2201918a07b1c60dd6a956a3b6ebb8e87fbab02ca98c`
y `trace_digest=be7598b1360c40ae2f4e39d0c2720af1ea41b11b9b1cecaa60a4e5972428d7ff`.
La ejecución CLI pasó con 15 dispositivos, 34 rutas y readbacks exactos,
158 eventos de auditoría, 31 snapshots recuperados y cero violaciones de
invariantes.

Las puertas ejecutadas para este cierre fueron:

- `uv run pytest -q`: `1771 passed, 18 skipped`.
- `project-composition-check "$(cat .ai/project-name)"`: `500 passed, 18 skipped`,
  Import Linter `4 kept, 0 broken`.
- `uv run ruff check src tests scripts`: sin errores.
- `uv run mypy src`: sin errores en 162 archivos fuente.
- `uv run python scripts/check_runtime_contract_docs.py`: contrato coherente.
- `uv run python scripts/export_schemas.py`: 73 schemas exportados.
- `git diff --check`: correcto.

La revisión estructural Graphify acotada a los módulos afectados produjo 316
nodos y 748 aristas. El refresco incremental del árbol completo se detuvo
durante la extracción AST porque OneDrive bloqueó un `pathlib.stat()`; Graphify dejó
visible además que 14 migrations SQL requieren `tree_sitter_sql`. Esta
limitación del índice no afecta a la ejecución ni a las puertas anteriores.

## 14. Criterio de salida y mantenimiento

La salida software de la fase requiere todos los checkboxes de
`specs/186-phase4-ventaja-diferencial/tasks.md`, schemas regenerados, contrato
MCP coherente, suite completa verde, Import Linter, mypy, Ruff,
`project-composition-check` y este review. La salida física requiere repetir
la qualification con evidencia `hardware` actual, bus KNX/ETS/knxd accesible y
HIL battery/EV; esos artefactos no se deben fabricar en CI ni marcar desde el
simulador.

## 15. Addendum — ventaja diferencial demostrable en el mundo físico — 2026-09-05

### 15.1 Qué demuestra ya el producto

El activo diferenciador software está presente: modelo universal, garantías
de capabilities, Adapter SDK, conformance harness, commissioning tipado,
optimization DSL explicable, Skills portables, privacidad, automatización
local y gemelo digital reproducible.

El gemelo digital demuestra que el circuito runtime → adapter projection →
planta → readback → estado/auditoría funciona bajo fallos controlados. No
demuestra la respuesta de un firmware, radio, bus, relé, inversor, cargador o
instalación eléctrica reales.

### 15.2 Cualificación física que falta

Para convertir la ventaja en una garantía de producto hay que ejecutar un
programa de commissioning con evidencia independiente:

1. Inventariar hardware, modelo, firmware, instalación, bus y configuración.
2. Asociar cada dispositivo real a una identidad canónica y a sus
   capabilities observables.
3. Ejecutar lecturas no destructivas y confirmar disponibilidad/frescura.
4. Ejecutar mutaciones limitadas y reversibles con operador presente.
5. Confirmar readback, tolerancia, latencia y límites físicos.
6. Inyectar desconexión, stale, rechazo, delayed response, duplicate,
   out-of-order, restart y partial failure.
7. Registrar evidencia firmada y enlazarla al binding de commissioning.
8. Repetir para batería, EV, HVAC y actuadores críticos.
9. Invalidar la qualification cuando cambien firmware, cableado, mapping o
   capability guarantees.

### 15.3 Ecosistema y adapters

El SDK permite incorporar conectores internos sin crear una ruta de autoridad
paralela, pero una plataforma abierta necesitará además:

- paquetes versionados y firmados o revisados;
- matriz pública de capabilities y garantías;
- conformance suite ejecutada contra implementaciones reales;
- simulador contractual equivalente al hardware soportado;
- compatibilidad y migración documentadas;
- contratos de traducción internos cuando Home Assistant no ofrezca la latencia
  o semántica necesaria, siempre detrás del adaptador universal.

Hue, Shelly, Tuya, inversores y fabricantes de batería/EV no deben entrar como
adapters públicos ni por llamadas vendor-specific desde MCP; deben aparecer
mediante HA o conectores internos del SDK y seguir atravesando el runtime,
policy y executor.

### 15.4 Criterios de aceptación de la fase completa

- La matriz digital permanece verde y reproducible.
- Cada adapter declarado tiene al menos una ejecución real con command,
  readback y evidencia archivada.
- Los actuadores de riesgo tienen HIL firmado y qualification vigente.
- `/readyz` solo pasa cuando todas las capabilities necesarias están
  cualificadas para la operación solicitada.
- Una pérdida de conectividad o evidencia expirada degrada a `UNKNOWN` o
  bloquea, nunca concede autoridad.
- Las Skills explican si una acción es simulada, propuesta, programada o
  físicamente confirmada.
- Ninguna comparación de escenarios, métrica o demo digital se presenta como
  prueba de hardware.

### 15.5 Pendientes externos trazables

La tarea de HIL de batería permanece abierta en [Spec 133](../specs/133-battery-hil-certification/tasks.md).
Las pruebas reales por dependencia se activan según commissioning en [Spec 140](../specs/140-real-composition-tests/tasks.md)
y los contratos de proveedores independientes en [Spec 141](../specs/141-provider-contract-tests/tasks.md).
La evidencia actual y el bloqueo deliberado de readiness están consolidados
en [`production-readiness-latest.md`](evidence/production-readiness-latest.md).

## 16. Addendum — preflight reproducible de los residuales — 2026-09-06

Se añadió el comando:

```bash
uv run domoai-lab preflight \
  --seed 187 \
  --report docs/evidence/phase4-preflight-latest.md \
  --json-report docs/evidence/phase4-preflight-latest.json
```

El preflight reutiliza el runner existente del gemelo digital y el smoke del
laboratorio de proceso. No llama adapters, no crea approvals/leases/routes y
no lee secretos productivos. La ejecución real produjo:

- gemelo digital: **PASS**, seis adapters, diez dominios, catorce checks;
  `plant_digest=837c82b7d9071ba4782d2201918a07b1c60dd6a956a3b6ebb8e87fbab02ca98c`;
- laboratorio de proceso: **80 passed, 1 skipped**;
- batería/EV HIL: **BLOCKED_EXTERNAL_DEPENDENCY**;
- commissioning de protocolos físicos: **BLOCKED_EXTERNAL_DEPENDENCY**;
- provider independiente desplegado: **BLOCKED_EXTERNAL_DEPENDENCY**;
- resultado global: `blocked_external_dependency`, con entorno
  `local_preflight`.

La evidencia machine-readable está en
[`phase4-preflight-latest.json`](evidence/phase4-preflight-latest.json) y la
versión operativa en
[`phase4-preflight-latest.md`](evidence/phase4-preflight-latest.md). El
resultado bloqueado es intencional: demuestra que las puertas de software
están verdes sin convertirlas en qualification hardware ni habilitar
readiness productivo.

La regresión posterior al preflight quedó verificada con `1906 passed, 18
skipped`; el laboratorio multi-host Docker pasó 8/8 escenarios y 20/20
carreras tras corregir el arranque concurrente documentado en el
[`ledger multi-host`](evidence/multihost-lab-v2-bug-ledger.md) como LAB-013.
