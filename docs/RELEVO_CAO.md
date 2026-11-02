# Relevo CAO — P04 en curso

## Actualización de reanudación — 2026-10-26

Esta actualización prevalece sobre la próxima acción histórica del 14 de
septiembre descrita abajo. El usuario transfiere la continuación a Claude.
No se modificó código de producto ni tests en esta reanudación.

- Estado: **292/553 aceptadas; P04 9/28**. T011/T012 siguen pendientes.
- Leer `docs/plan-ejecucion-y-modelos.md`, que sustituye al nombre fechado
  eliminado del árbol; no restaurar el archivo antiguo por conveniencia.
- `specs/243-household-consent/plan.md` y `tasks.md` contienen la corrección
  B0 del 17 de septiembre y su revisión root del 26 de octubre: `/` sigue
  inválido para MCP; B0 prueba C2 ausente incluso con pareja residente en
  Settings; el positivo configurado y la separación efectiva son B1.
- Revisión independiente del diseño corregido: **R1 PASS WITH RISKS**,
  P0/P1/P2=0. Informe completo, con FAIL previo conservado:
  `.superpowers/sdd/tasks/p04-t012-b-freeze-review-2026-10-26.md`, SHA-256
  `6ef7ee5fabc04bc9bbbd823ab3efb2e6e3aa03ae5f29d282037a4fa79e17ce9a`.
  Es aceptación de diseño, no de implementación ni de tareas.
- Próximo corte: **T012-B0 RED**, luego cambio mínimo y GREEN bajo owners
  `src/domoai/config/settings.py`, `src/domoai/mcp/gateway.py`,
  `tests/unit/config/test_mcp_gateway_settings.py`,
  `tests/unit/mcp/test_gateway.py` y
  `tests/integration/test_household_consent.py`. Revisión independiente y
  parada antes de B1; no adelantar B1/B2 ni otros paquetes.
- Baseline nueva ejecutada desde WSL Ubuntu: `uv run pytest
  tests/security/test_household_consent_security.py
  tests/integration/test_household_consent.py -k 't011_c_session or t011_b or
  t011_c_admission' -q --tb=short`: **256 passed, 93 deselected, 26.58 s**.
  Log `/tmp/domoai-resume-2026-10-26-baseline.log` en WSL. No gates globales
  nuevos. Graphify de lectura conserva índice histórico y truncamiento.
- El entorno `.venv` es Linux y funciona desde WSL Ubuntu con Python
  3.12.13; no reemplazarlo por un venv Windows. `uv`, `graphify` y
  `project-composition-check` están disponibles allí. Directorio WSL:
  `/mnt/c/Users/ferna/OneDrive/Escritorio/DomoAI`.
- Para Spec Kit usar `SPECIFY_FEATURE_DIRECTORY=specs/243-household-consent`
  al consultar prerrequisitos: `.specify/feature.json` apunta a P02. No
  modificar ese estado compartido por conveniencia.

Preservar todo el árbol sucio. No commit, push, limpieza, despliegue ni
hardware. Los resultados y límites históricos siguientes siguen vigentes
salvo las sustituciones explícitas anteriores.

Fecha de contraste: 2026-09-14. Este documento es un estado de relevo, no
una aceptación ni una autorización para continuar cambios. Se contrastó con
el árbol de trabajo, el seguimiento, `tasks.md` y la propuesta local de
alcance T012-B. No se ejecutaron pruebas ni se modificó código durante la
preparación de este relevo.

## Estado real y frontera de trabajo

- Estado operativo del programa: **292/553 tareas aceptadas; 261 pendientes**.
  Es el recuento vigente de
  `docs/seguimiento-ejecucion-2026-09-10.md`, no el histórico de 336 tareas.
- P02 permanece **parcial: 18/27 aceptadas**. P03 no se ha iniciado.
- El paquete activo es **P04 — Consentimiento comprensible**:
  **9/28 aceptadas** (`T001`–`T008` y `T010`); quedan 19 pendientes.
- La última frontera real no es una implementación en curso: es el informe
  de delimitación de **T012-B**, aún sin revisión independiente, sin pruebas
  nuevas y sin aceptación. Su hash local es
  `85b743269564683f6e04ad79b40c1c7deefc8d5e877314bfe8af75dbfd720fc3`:
  `.superpowers/sdd/tasks/p04-t012-b-freeze.md`.

No se debe iniciar P03, P12, T018 ni P04 fuera de esta frontera. P01 está
aceptado **PASS WITH RISKS** y no se reabre salvo regresión causal demostrada.

## Fuentes de autoridad

- Reglas: `AGENTS.md` y las secciones de trabajo significativo, seguridad y
  TDD de `AI_WORKFLOW.md`.
- Seguimiento y contadores:
  `docs/seguimiento-ejecucion-2026-09-10.md`.
- Orden, dependencias y modelos:
  `docs/plan-ejecucion-y-modelos-2026-09-10.md`.
- Índice de paquetes:
  `docs/workflow-auditorias-2026-09-10/README.md`.
- P04: `specs/243-household-consent/spec.md`, `plan.md`, `tasks.md`,
  `research.md`, `data-model.md`, `contracts/behavior.md` y `quickstart.md`.
- Configuración del gate de proyecto: `.ai/project-name`.
- Evidencia de trabajo local (ignoradas por Git, pero disponible en este
  worktree): `.superpowers/sdd/tasks/p04-*.md`.

La tabla de dependencias vigente justifica P04 detrás de P02, A-02 y A-05.
P03 requiere P02/P04/A-02/A-03, por lo que no es un siguiente paquete
ejecutable mientras P04 esté abierto. P12 cierra operación/recuperación y
no autoriza completar los cortes de backup, journal y recovery de P04 sin su
propio alcance y dependencias.

## Objetivo y aceptación pendiente

P04 debe proporcionar consentimiento doméstico comprensible y seguro con
una frontera durable: admisión humana, sesiones/retos/decisiones, autoridad
de grants, entrega/reintentos/recuperación y composición real del gateway.
La aceptación de cada tarea exige evidencia RED causal cuando hay defecto,
cambio mínimo, GREEN focal, revisión Astra High independiente para la
frontera sensible, y los gates aplicables antes de aceptar el paquete.

Lo que aún impide cerrar P04 incluye, como mínimo:

- **T009 C–F:** fidelidad de backup, exclusión/journal externo y los flujos de
  recovery/ACK. T009 A+B solamente está verificado; T009 completo no está
  aceptado. C depende de límites que pertenecen a P12 y D–F requieren el
  alcance posterior de delivery/recovery, incluyendo T018.
- **T011:** las porciones A, B, C1 y C2 tienen evidencia interna, pero faltan
  la secuencia de revisión/decisión/challenge/assertion/grant y la aceptación
  íntegra de la tarea. No confundir sus verdes internos con una tarea cerrada.
- **T012:** A está revisada internamente, pero B debe demostrar composición
  efectiva de gateway, inventario de credenciales, propiedad, cierre y
  recarga. No basta una inyección de constructor ni una lectura del código.
- Las demás tareas P04 no marcadas en `tasks.md`, además de gates finales de
  paquete, integración/composición y verificación de artefactos protegidos.

### Próxima acción concreta (cuando se reanude)

Primero, un revisor independiente **GPT-6 Astra / High** debe contrastar
`p04-t012-b-freeze.md` contra `mcp/gateway.py`, `mcp/configured.py`,
`mcp/unified_server.py`, `management/api.py`, `management/consent.py`,
`mcp/auth.py`, `mcp/token_lifecycle.py` y `application/runtime_factory.py`,
usando Graphify de forma de solo lectura y la fuente. Debe decidir si el
alcance B0/B1/B2 tiene propietarios exclusivos y si conserva los contratos
no residentes. Hasta ese PASS no se escribe código T012-B.

Si se aprueba, el escritor exclusivo Astra High hará primero los RED B0
propuestos, detendrá para revisión y solo entonces continuará por B1 y B2.
T012-B es persistencia/autoridad/recuperación: no corresponde a Terra salvo
un corte futuro, aislado y con contrato explícito que el revisor haya cedido.

## Trabajo completado y evidencia disponible

| Corte | Estado real | Evidencia principal |
|---|---|---|
| P01 | Aceptado **PASS WITH RISKS**; no reabrir | Histórico final: `project-composition-check` 1235 passed, 18 skipped; unit/contract/security 2279 passed. |
| P04 T001–T008 | Aceptadas; T008 **PASS WITH RISKS** | T008 dejó control/nonce/prepare, migración 026 y suelo de esquema 4. Revisión final P0/P1/P2=0 en `p04-t008-final-independent-review.md` (SHA `8859426133653afd2f27b479677b63950363db1aa2f54912171524df88862e65`). |
| P04 T009 A+B | Parcial verificado, **no aceptado** | Cuatro pruebas reales Testcontainers PostgreSQL; rollback/copia/reupgrade. El código de migración no se cambió en este corte (SHA de `postgres_migration.py`: `486cc122765aba0434ffc435554f29669ce1bdc01a7927ef51e4c99ea8cd94ea`). Informes writer/review: SHA `5405590036f055241bc6ba6ccc3acf8fa3b757daab0a634365e0f16e34f87f08` / `c77a3c103faf4664af2f2685ebf1cdbad65d35f09c637c60cf79051777db3f8e`. |
| P04 T010 | Aceptado **PASS WITH RISKS** como preparación RED | Revisión R1: 22 RED esperados (12 causales, 10 precursores de interfaz), 51 GREEN y 169 adyacentes GREEN; P0/P1/P2=0. Informes SHA `4703842d47b0e21b9da6ad72304d0f2f0cea23fd47965fc0d51779d2969e5361` / `1b959bc2df6a9ba1c58d01ce9d3f223c916cfe405cdb54ab159cc6ba57a1cac4`. |
| P04 T011 A/B/C1/C2 | Evidencia interna; **T011 no aceptada** | C2: 63 focales, conjunto C2+B+C1 256 GREEN, A 67 GREEN, management 111 GREEN, seguridad 303 GREEN y un precursor C conocido. Revisión C2 P0/P1/P2=0: SHA `f5e9da14e2c0111ebc5636e36dcf51d9b979aece8ab426f8a75f754703bc5e16`; writer SHA `e8387e3c9147e319e193c22e3865c021c6dfd37568318dfbe77b3131710260fa`. |
| P04 T012-A | Evidencia interna; **T012 no aceptada** | Configuración `resident_token_file`/`resident_origin` y snapshot inmutable. Revisión R1 P0/P1/P2=0, SHA `f8985b8737900a3f2bae7067a231dc8facd1f0a3855939f9ec6b14b9a7e97d6d`; writer SHA `11d7c795016d0aa7bcb8ae424cd7037ab9548b5f75a17ebf0e19e9dd861fc255`. |
| P04 T012-B | Solo planificación; **no ejecutada ni aceptada** | Informe local indicado arriba. Propone cortes B0 (transporte), B1 (instancia/inventario) y B2 (cierre/recarga), con paradas de revisión entre ellos. |

El precursor conocido de recibo C sigue siendo una ausencia de endpoint y se
mantiene explícitamente: no se eliminó, no se marcó xfail ni se sustituyó por
un GET ficticio para inflar verdes.

## Pruebas ejecutadas: comandos y resultados reales

Estos resultados son históricos del candidato descrito; no son una nueva
ejecución de este relevo.

T008, batería focal final (sin skips):

```bash
uv run pytest tests/contract/test_household_consent_contract.py tests/integration/test_household_consent.py tests/composition/test_household_consent_composition.py tests/composition/test_temporal_consent_composition.py tests/unit/runtime/test_approval_store.py tests/unit/persistence/test_approval_grant_repository.py tests/unit/persistence/test_schema_migrations.py tests/unit/persistence/test_postgres_backend.py tests/integration/test_runtime_schema_compatibility.py tests/integration/test_household_onboarding.py tests/composition/test_runtime_schema_compatibility.py tests/integration/test_sqlite_postgres_migration.py -q --tb=short
```

Resultado: **492 passed**, 815.53 s. Log
`/tmp/domoai-p04-t008-final-focal-r3.log`, SHA
`f2dea12949912b7f0c855581b71b45c663576ec6833734da4a5f0b2090bd9753`.

T008, gate global:

```bash
project-composition-check "$(cat .ai/project-name)"
```

Resultado: arquitectura **4 kept, 0 broken**; **1380 passed, 18 skipped,
1 failed**, 1565.73 s. El único fallo fue ambiental: Chromium no inicia porque
falta `libasound.so.2` (exit 127), antes de ejecutar DomoAI. Log
`/tmp/domoai-p04-t008-final-project-composition-r4.log`, SHA
`61e8511c9e3fb70208970af538eff844cbcb3457e86d907683566e6cced118cf`.
También pasó `uv run lint-imports` con 4 kept/0 broken y se verificaron
11/11 artefactos protegidos contra su baseline, sin abrir bases de datos.

T009 A+B, revisión independiente:

```bash
uv run pytest tests/integration/test_sqlite_postgres_migration.py -q --tb=short -k t009
```

Resultado: **4 passed, 6 deselected**, 49.90 s, sin skips.

```bash
uv run pytest tests/unit/persistence/test_schema_migrations.py tests/unit/persistence/test_postgres_backend.py tests/integration/test_runtime_schema_compatibility.py tests/integration/test_durable_approval_store.py tests/unit/persistence/test_approval_grant_repository.py -q --tb=short -k 't008 or floor or t007_a_control_receipt or t007_b_delivery_storage_is_provisioned or t007_b_delivery_schema_upgrades_real_025 or t007_b_retry_after_expiry'
```

Resultado: **34 passed, 178 deselected**, 69.22 s. Dos mutantes de commit
temprano y uno de omisión de `consent_issuance_receipts` fallaron de forma
intencional; no constituyen GREEN de T009 completo.

Para T012-B aún **no hay resultados**: los comandos enumerados en su informe
son plan de RED/GREEN, no evidencia ejecutada.

## Cambios parciales y preservación obligatoria

El `git status --short` observado para este relevo es sucio. No hay permiso
para revertir, limpiar, commit, push, merge, desplegar, iniciar hardware ni
reparar Docker. Tratar todos los cambios no identificados con certeza como
trabajo ajeno a preservar.

Cambios de P04 visibles o potencialmente compartidos incluyen:

- Autoridad/configuración/HTTP: `src/domoai/application/authority.py`,
  `src/domoai/config/settings.py`, `src/domoai/domain/errors.py`,
  `src/domoai/management/api.py`, `src/domoai/management/auth.py`,
  `src/domoai/management/consent.py` (sin seguimiento),
  `src/domoai/mcp/auth.py`.
- Durabilidad y esquema: `src/domoai/runtime/approval_store.py`,
  `src/domoai/persistence/{repositories.py,sqlite.py,postgres.py,postgres_schema.sql,postgres_migration.py,coordination.py,contract_compatibility.py}.py`,
  `src/domoai/persistence/migrations/026_household_consent.sql` (sin
  seguimiento), y sus tests de persistencia/integración.
- Contratos y pruebas de consentimiento sin seguimiento:
  `schemas/v1/household-consent-{decision,view}.schema.json`,
  `src/domoai/{application/household_consent.py,domain/household_consent.py}`
  y `tests/{composition,contract,integration,security}/test_household_consent*.py`.
- También están modificados `scripts/export_schemas.py`,
  `connection_repository.py`, `household_preferences.py`,
  `tests/integration/test_household_onboarding.py` y otros tests de esquema,
  gateway y conexión. Son particularmente probables cambios P02 o ajenos;
  no asumir propiedad P04.
- `docs/seguimiento-ejecucion-2026-09-10.md` ya estaba modificado. Este
  relevo no reconcilia ni sobrescribe ese seguimiento.

La lista exacta de rutas debe volver a obtenerse con `git status --short`
antes de cualquier edición. La única modificación realizada para este relevo
es este archivo.

## Preparación para CAO u otro worktree

1. Ejecutar `git status --short` y leer los artefactos de autoridad anteriores
   antes de tocar una ruta. No usar `reset --hard`, `checkout --`, `clean`,
   rebase, commit ni cherry-pick para "normalizar" este árbol.
2. Un worktree nuevo desde `HEAD` **no contiene** los cambios P04 activos,
   que son sin commit e incluyen archivos sin seguimiento. No puede validar
   este candidato por sí solo. Hace falta una transferencia coordinada,
   no destructiva, del diff rastreado **y** de la lista/contenido hasheado de
   archivos sin seguimiento, aprobada por el propietario. Alternativamente,
   continuar en este worktree compartido con propietarios exclusivos.
3. Instalar/sincronizar dependencias según el proyecto (`uv`); para T009 se
   necesita Docker funcional para Testcontainers PostgreSQL. No se necesita
   ni se autoriza hardware real. T012-B usa sockets/Uvicorn y SQLite locales,
   no Docker, como especifica su alcance.
4. No intentar corregir la infraestructura durante P04. El diagnóstico local
   `/tmp/domoai-docker-diagnosis-2026-09-13.md` deja constancia de que Docker
   Engine 29.7.2 funciona, pero `domoai-lab-mqtt-1` termina con exit 13 por
   faltar `/mosquitto/auth/passwd` en `dev/lab/compose.yaml` y faltan
   `DOMOAI_MQTT_USERNAME/PASSWORD`; `domoai-lab-zigbee2mqtt` también está
   unhealthy. No se reinició ni reparó nada. El host además carece de
   `libasound.so.2`, que explica el fallo externo de Chromium del gate global.
5. Repetir Graphify solo de modo incremental/lectura si cambia la fuente de
   T012-B. El grafo conocido ronda 12 157 nodos y quedó desactualizado frente
   a cambios posteriores de T008; las conclusiones sensibles se contrastaron
   contra código fuente. No regenerar el corpus global por conveniencia.

## Riesgos, incertidumbres y límites abiertos

- El gate global de T008 no es PASS completo por la dependencia del host
  Chromium; no atribuir ese fallo al producto ni ocultarlo como skip.
- La composición real de gateway, la ruta de producción, el inventario de
  credenciales, el cierre y la recarga de T012-B siguen sin prueba. Las
  construcciones C1/C2 aisladas no demuestran esas propiedades.
- T011 y T012-A tienen revisiones internas limpias pero aún no cumplen sus
  tareas completas ni gates de paquete; el conteo 9/28 no debe incrementarse.
- T009 A+B no acredita backup/restauración, journal externo, recovery o ACK.
- No hay prueba HIL ni se debe ejecutar hardware.
- No se han inferido tokens, costes ni calidad desde la duración de pruebas.

Al reanudar, registrar en `tasks.md` y en el seguimiento solo resultados
reales, riesgos, hashes y tareas formalmente aceptadas. Tras un candidato P04
final, aplicar contratos, integración, composición,
`project-composition-check "$(cat .ai/project-name)"`, revisión Astra
independiente, `git diff --check`, diff final y verificación de artefactos
protegidos antes de declarar el paquete cerrado.
