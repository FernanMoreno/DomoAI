# Seguimiento de ejecución — 2026-09-10

Estado de coordinación documental derivado del [plan de ejecución y reparto de modelos](plan-ejecucion-y-modelos-2026-09-10.md), el [workflow de auditorías](workflow-auditorias-2026-09-10/README.md) y el [catálogo de paquetes](workflow-auditorias-2026-09-10/paquetes.json). El plan original conserva 24 paquetes y **336 tareas históricas**; la concreción registrada previamente elevó el recuento refinado a **435**. Las revisiones de P-05 y P-06 añaden 20 y 18 tareas respectivamente, P-09 y P-07 añaden otras 16 cada uno y P-12 añade 18 tras su revisión, por lo que el recuento refinado validado actual es **523**. El cierre de A-11 acepta 18 tareas y el mínimo P-11 acepta 16; el paquete auxiliar 252 tiene 6 tareas aceptadas y el paquete 253 tiene 24 tareas aceptadas. Tras aceptar las 24 tareas de P-01, las 18 tareas de fundamento P-02 y T001–T008/T010 de P-04, el recuento operativo actual es **292 aceptadas de 553 combinadas** (523 refinadas + 30 auxiliares), con **261 pendientes**; no sustituye silenciosamente el histórico 336.

## Estado actual

**A-01–A-12** y **P-01** están **aceptados** con sus veredictos y límites documentados. También están aceptados los mínimos P-10/P-11, los auxiliares 252/253, el fundamento parcial de P-02 (18 tareas, no el paquete completo) y T001–T008/T010 de P-04 (9/28, no producto completo); P-03–P-09, P-12 y los cierres completos P-10/P-11 siguen pendientes. A-02 conserva **14 tareas verificadas (T001–T014)**, A-03 suma otras **14**, A-01 otras **14** y A-04 otras **14**. El baseline histórico previo a la corrección fue de 4 passed y 4 regresiones RED; esas regresiones ya no son el estado actual. `/root/a02_builder` se lanzó con `fork all` y heredó la configuración del principal, sin forzar independientemente modelo/esfuerzo; `/root/a02_review` sí realizó una revisión explícita con GPT-6 Astra / High. A-01 tuvo como escritor a `/root/a01_terra_builder`, lanzado con `fork none` y GPT-5.6 Terra / Medium explícito. A-04 tuvo como escritor a `/root/a04_terra_impl`, lanzado con `fork none` y Terra / Medium explícito; el reviewer independiente fue `/root/a04_astra_review`, Astra / High, también con `fork none`. El preflight antiguo de `/root/a04_terra_builder` permanece solo lectura.

A-03 está **aceptado**, con implementación del escritor exclusivo **GPT-6 Astra / High `/root/a03_astra_builder`**, lanzado con `fork none`. La preparación RED antigua se heredó del hilo previo; la implementación nueva usa la configuración explícita GPT-6 Astra / High.

A-01 está **aceptado**, con escritor exclusivo **GPT-5.6 Terra / Medium `/root/a01_terra_builder`**, lanzado con `fork none` explícito. A-04 está **aceptado**, con escritor exclusivo **Terra / Medium `/root/a04_terra_impl`**, lanzado con `fork none`; el reviewer independiente fue **Astra / High `/root/a04_astra_review`**, también con `fork none`. El preflight antiguo de `/root/a04_terra_builder` es solo lectura.

A-05 y A-06 están **aceptados PASS WITH RISKS**, con escritores exclusivos Astra / High `/root/a05_astra_impl` y `/root/a06_astra_finish`; la implementación parcial inicial de A06 fue Terra / Medium `/root/a06_terra_preflight`. La primera revisión independiente A06 FAIL pese a 168 verdes fue corregida con RED explícito (12 + 3 casos) y la segunda revisión focal pasó 16 pruebas. Los límites aceptados son plataforma/live HIL. Las asignaciones sensibles de cierre A05/A06, A07, A08 y A10 son Astra / High; las tareas documentales acotadas corresponden a Luna.

La evidencia global para A05+A06 es composición con código de salida 0 (**797 passed, 18 skipped, 695.54 s**, `/tmp/domoai-a05-a06-composition-final.log`), suite amplia de contratos/unidades/seguridad (**1858 passed, 125.80 s**, `/tmp/domoai-a05-a06-broad-final.log`), arquitectura **4 kept, 0 broken**, mypy **246** y Ruff **37 archivos Python**. El refresco Graphify terminó con exit 0 y la verificación final de hashes/integridad también pasó; A05 y A06 quedan aceptados **PASS WITH RISKS**.

A-07 y A-10 están **aceptados PASS WITH RISKS** tras implementación, revisión independiente y gates globales; la ejecución conjunta registró 803 passed/18 skipped y 1934 pruebas amplias aprobadas. A-10 recibió una corrección adicional de identidad tras un primer FAIL y pasó 59 pruebas focales antes de su segunda revisión. El revisor de ambos paquetes fue `/root/a10_astra_review` (nombre histórico; no escribió ninguno). A-08, A-09 y el mínimo P-10 están **aceptados PASS WITH RISKS** tras la comprobación global conjunta (847 passed, 18 skipped, salida 0), 2042 pruebas amplias, revisión independiente y verificación final. A-09 corrigió un FAIL de revalidación del proveedor; P-10 corrigió defectos de validación con Terra y un segundo FAIL del cierre SDK con Astra, con otro Astra como revisor independiente. P-10 completo sigue pendiente: la aceptación solo cubre `generic-sdk-local`. A-11 está **aceptado** con 18 tareas; el mínimo P-11 está **aceptado** con 16 tareas. A-11 queda cerrado; P-11 completo y P-10 completo siguen pendientes. P-01/P-02/P-04/P-03 tienen preparación documental validada de 24/27/28/30 tareas. P-05 y P-06 cuentan ahora con preparación documental de 34 y 32 tareas, sin aceptación de implementación. Estas preparaciones no cuentan como aceptación.

## A-02 aceptado: evidencia y límites

Según la [verificación definitiva de A-02](../specs/229-queued-dispatch-cancellation/verification.md): focal **99 passed**; contratos y unidades **492 passed**; composición global **670 passed, 18 skipped (PASS)**; revisión independiente **32 passed** más replay con SQLite real; `mypy src` sobre 245 archivos sin errores; Ruff sin errores; arquitectura **4 kept, 0 broken**. El refresco Graphify focal registró **10 748 nodos, 30 673 aristas, 0 extremos ausentes**, sobre 10 archivos y sin uso de LLM (0 tokens). El HTML quedó obsoleto y no se regeneró. El límite es la ausencia de HIL.

## A-03 aceptado: evidencia y cierre

A-03 tiene **14 tareas aceptadas**. La evidencia focal registró **85 passed**; contratos, **432 passed**; `mypy src`, **245 archivos sin errores**; Ruff sin errores; arquitectura **4 kept, 0 broken**; y composición global **726 passed, 18 skipped (PASS)** en **642.49 s**. La revisión independiente Astra quedó limpia tras corregir el desfase de timestamps/offset. El refresco Graphify focal cubrió 4 archivos, **64 nodos AST** y **169 aristas**, con grafo de **10 779 nodos, 30 725 aristas y 0 extremos ausentes**, sin uso de LLM. El post global de `/root` comprobó los 4 hashes, diff check OK y aceptación de A-03.

## A-01 aceptado: evidencia y cierre

A-01 tiene **30 pruebas focales**, **35 de revisión independiente**, **433 contract**, `mypy` sobre **245 archivos sin errores**, Ruff sin errores y arquitectura **4 pass**. La composición global posterior pasó: **737 passed, 18 skipped** en **655.89 s**, con exit 0 en `/tmp/domoai-a01-pg-composition-final.log`. Los contratos completos pasaron **433** en **55.22 s**. El post-gate comprobó **8 hashes OK** y diff check OK.

La raíz exacta del fallo anterior queda preservada: `session.unlock()` ocurría antes de `commit`, y dos hosts chocaban en `json_extract` con `UniqueViolation`; la corrección serializa con lock transaccional en el paquete 252 y deja el proxy posterior al commit. A-01 queda aceptado.

## A-04 aceptado: evidencia y límites

A-04 tiene **14 tareas aceptadas**. La evidencia focal registró **37 pruebas en 18.90 s**; los contratos completos pasaron **439** en **56.75 s**; la composición global pasó **741**, con **18 skipped**, en **670.70 s**; Ruff (9 archivos), mypy sobre 245 archivos y arquitectura (**4 kept, 0 broken**) quedaron limpios. La revisión independiente Astra registró **39 passed en 18.36 s**, incluyendo dos recorridos A-02 `queued/readback composition`, PASS. El post-gate verificó los 8 hashes del estado congelado y el diff check; los hashes no describen cambios posteriores de otros agentes. Graphify focal registró 194 nodos AST, 541 aristas, grafo total 10 846 nodos, 30 896 aristas y 0 extremos ausentes. El límite es la ausencia de hardware/HIL; el fixture KNX no conectado dejó warning `pending` al abortar y no se clasifica como regresión de producto.

## Paquete auxiliar 252 aceptado

`specs/252-postgres-bootstrap-serialization` tiene **6 tareas aceptadas (PG001–PG006)**, fuera del plan original. La evidencia registra **83 focales** y **14 de revisión con PostgreSQL real**; queda documentado el riesgo de versiones mixtas. El escritor único de PostgreSQL y tests fue **Astra High `/root/pg_bootstrap_astra`**, lanzado con `fork none`. Sus tareas se contabilizan separadamente. El registro histórico de ese cierre conservaba **119 aceptadas de 370**; esa cifra queda fechada y no representa el estado actual. En el estado vigente, 252 aporta 6 aceptadas y 253 aporta 24 aceptadas, sin alterar el histórico de 336.

## Mapa de paquetes, orden y dependencias

El orden de trabajo respeta primero ejecución/autoridad/readiness, después contabilidad energética y finalmente el recorrido doméstico. Una dependencia indica que el paquete consumidor debe esperar el contrato o evidencia del paquete precedente; un paquete pendiente no se considera satisfecho por estar preparado documentalmente.

| Orden | Paquete | Alcance | Dependencias reales | Estado |
|---:|---|---|---|---|
| 1 | A-02 | Cancelación anterior al despacho | — | Aceptado |
| 2 | A-03 | Cierre durable después de ACK | —; secuenciar después de A-02 por frontera cola/ledger | Aceptado |
| 3 | A-01 | Aislamiento de ocurrencias horarias | — | Aceptado |
| 4 | A-04 | Readiness de tareas obligatorias | — | Aceptado |
| 5 | A-05 | Administración explícita de conexiones | — | Aceptado PASS WITH RISKS |
| 6 | A-06 | Estados parciales de Home Assistant | — | Aceptado PASS WITH RISKS |
| 7 | A-07 | Áreas visibles según autoridad | A-05 | Aceptado PASS WITH RISKS |
| 8 | A-08 | Disponibilidad efectiva de capacidades | A-07 | Aceptado PASS WITH RISKS |
| 9 | A-09 | Demanda base en contexto energético | — | Aceptado PASS WITH RISKS |
| 10 | A-10 | Comparación de baseline sin actuación | — | Aceptado PASS WITH RISKS |
| 11 | A-11 | Autoconsumo con origen energético | A-09 | Aceptado; 18 tareas |
| 12 | A-12 | Coherencia monetaria | A-09/A-10/A-11/253 | Aceptado; 22 tareas; revisión independiente y gates finales |
| 13 |  P-10 mínimo | Conexión y cualificación mínima de clientes MCP | A-05, A-07 | Mínimo aceptado; cierre completo pendiente |
| 14 | P-11 mínimo | Cobertura mínima con evidencia | A-08 | Mínimo aceptado; 16 tareas; cierre completo pendiente |
| 15 | P-01 | Puesta en marcha doméstica | A-05, A-06, A-08; requiere P-10/P-11 mínimos | Aceptado PASS WITH RISKS; 24 tareas; revisión independiente y gates finales |
| 16 | P-02 | Perfil doméstico durable | P-01 | Fundamento parcial 18/27; cierre P03 pendiente; P04 siguiente dependencia |
| 17 | P-04 | Consentimiento comprensible | P-02, A-02, A-05 | T001–T008/T010 aceptadas (9/28); delivery/recovery y dependencias P03/P12 pendientes |
| 18 | P-03 | Tareas domésticas persistentes | P-02, P-04, A-02, A-03 | Documentación preparada; dependencias pendientes |
| 19 | P-08 | Prioridad manual y conflictos | P-03, A-01, A-02 | Pendiente |
| 20 | P-05 | Estado diario y avisos | P-03, P-08, A-07 | Documentación preparada; 34 tareas; pendiente |
| 21 | P-09 | Rutinas por intención | P-03, P-04, P-08, A-01 | Pendiente |
| 22 | P-06 | Coste y ahorro medido | P-02, A-09, A-10, A-11, A-12 | Documentación preparada; 32 tareas; pendiente |
| 23 | P-07 | Replanificación doméstica | P-03, P-04, P-06, P-08, A-04 | Pendiente |
| 24 | P-12 | Operación, privacidad y traspaso | P-02, P-03, P-04, P-05, P-06 | Pendiente |

P-10 y P-11 tienen una entrega mínima que precede a P-01; sus cierres completos pueden continuar después. P-02 → P-04 → P-03 → P-08 permite construir el recorrido con consentimiento antes de persistir y priorizar tareas. P-05 y P-09 dependen de ese núcleo. P-06 consume toda la cadena energética A-09…A-12; P-07 integra replanificación; P-12 cierra operación y recuperación.

Las especificaciones, planes y tareas de cada paquete están enlazados desde el [índice de paquetes](workflow-auditorias-2026-09-10/README.md). El plan original conserva **336 tareas históricas**. El recuento refinado vigente es **523 tareas del plan**: parte de 435 y suma las revisiones de P-05 (+20), P-06 (+18), P-09 (+16), P-07 (+16) y P-12 (+18). Con las 6 tareas auxiliares de 252 y las 24 aceptadas de 253 son **553 combinadas**, de las que **292 están aceptadas** y **261 permanecen pendientes**. El histórico de 336 se conserva.

## Asignación de modelo y esfuerzo

La asignación sigue la tabla del plan y se puede aplicar por tipo de trabajo:

| Tipo de trabajo | Modelo / esfuerzo | Paquetes orientativos |
|---|---|---|
| Cancelación, concurrencia, autoridad, migraciones y contabilidad | GPT-6 Astra / High | A-02, A-03, A-05 cuando implique autoridad o migración, A-09–A-12 |
| Implementación cotidiana con contrato claro | GPT-5.6 Terra / Medium | Partes acotadas de A-01, A-04 y A-06–A-08, y partes de P-01–P-12, solo cuando no cambien autoridad, concurrencia, persistencia/migración ni contabilidad y el contrato esté claro |
| Cualquier cambio de A-06–A-08 o P-01–P-12 que toque autoridad, concurrencia, persistencia/migración o contabilidad | GPT-6 Astra / High | Filtrado de autoridad de A-07, frontera de disponibilidad de A-08 y cualquier parte de esos paquetes que cruce esas responsabilidades |
| Revisión independiente de cambios importantes o sensibles | GPT-6 Astra / High, sesión separada | A-02 y cualquier paquete con persistencia, autoridad, ejecución física, recuperación o contrato transversal |
| Documentación y transformaciones mecánicas acotadas | GPT-5.6 Luna / Low/Medium | Este seguimiento, índices, enlaces y actualizaciones mecánicas sin cambio de contrato |

La asignación es una recomendación operativa del plan, no una evaluación comparativa del proyecto. Medir por paquete aceptado coste, reintentos y defectos encontrados. Sol queda como alternativa para cambios complejos si se reserva Astra.

## Coordinación y regla de escritura

Debe haber un único agente escribiendo por subsistema. La propiedad se asigna antes de abrir trabajo paralelo y cubre código, migraciones, contratos, fixtures y documentación del subsistema. A-02 y A-03 se ejecutan secuencialmente al principio porque comparten cola, executor, ledger, readback y recuperación. Las revisiones independientes pueden leer el cambio mientras el escritor termina, pero no editar los mismos archivos. Los paquetes solo se paralelizan cuando sus archivos y contratos no se solapan y sus dependencias están cerradas.

## Evidencia exigida para cerrar un paquete

Cada paquete debe conservar evidencia concreta antes de avanzar:

1. `git status` y diff revisados, con trabajo ajeno preservado.
2. Especificación, plan, tareas y dependencias revalidados; Graphify contrastado con la fuente actual cuando el alcance sea significativo.
3. Para un defecto, reproducción, frontera incorrecta y causa raíz; prueba de regresión RED antes del cambio.
4. Cambio mínimo implementado y pruebas aplicables con resultado real: unitarias, contratos, integración, arquitectura y composición según alcance.
5. Para trabajo significativo, `project-composition-check "$(cat .ai/project-name)"`, más `system-composition-review` y revisión independiente cuando corresponda.
6. Revisión del diff final, decisión explícita sobre refrescar Graphify y sobre conocimiento durable, y `verification-before-completion` antes de declarar el paquete aceptado.
7. Registro de limitaciones: simulación frente a hardware, datos desconocidos, clientes no probados, gates omitidos y cualquier fallo pendiente. Un exit 0 de una reproducción de anomalía demuestra que el defecto persiste; no demuestra corrección.

Hasta que cada gate anterior tenga resultados reales, el estado es pendiente o en curso. No se debe marcar implementado por la existencia de `spec.md`, `plan.md`, `tasks.md`, reproducciones o baseline.

## Fuentes locales

- [Plan de ejecución y modelos](plan-ejecucion-y-modelos-2026-09-10.md)
- [README del workflow](workflow-auditorias-2026-09-10/README.md)
- [Paquetes en JSON](workflow-auditorias-2026-09-10/paquetes.json)
- [Diagnóstico y causas raíz](workflow-auditorias-2026-09-10/diagnostico.md)
- [Reproducción de cancelación](workflow-auditorias-2026-09-10/repro-cancelacion.txt)
- [Secuencia documental](workflow-auditorias-2026-09-10/secuencia.jsonl)

## Cierre conjunto A-08, A-09 y P-10 mínimo — 2026-09-11

Global: **847 passed, 18 skipped, 786,67 s**, salida 0; suite amplia: **2042 passed, 144,14 s**. Mypy: 248 fuentes; Ruff: 23 archivos; arquitectura: 4 contratos conservados, 0 rotos. Verificación de 44 entradas congeladas antes del cierre documental y revisión de diff contra baselines reales. Graphify actualizado sobre 23 archivos Python: 11272 nodos, 31723 relaciones, 0 enlaces colgantes, sin tokens LLM; HTML marcado obsoleto. Informe y verificación raíz: `/tmp/domoai-a08-a09-p10-post-composition-review.md` y `/tmp/domoai-a08-a09-p10-final-verification.json`.

El [informe SDK real](../specs/249-agent-client-qualification/evidence/p10-generic-sdk-local-accepted.json) conserva versión, protocolo y huellas de los conjuntos de fuentes declarados. No acredita clientes nombrados, hardware ni los recorridos completos P-10 que dependen de P-03/P-04. Los FAIL iniciales se conservan junto a sus correcciones; la ausencia de fallos finales no se atribuye retrospectivamente a candidatos anteriores.

### Seguimiento adicional de contratos — 2026-09-11

La exportación global durante A-11 detectó divergencia previa entre modelos y cinco schemas canónicos: `automation-event`, `automation-rule`, `optimization-result`, `physical-intent` y `plan`. Los snapshots anteriores a P-11 coinciden con los hashes del handoff de A-11. La exportación acotada de A-11/P-11 conserva esos cinco artefactos; esto no acredita paridad global entre todos los modelos y schemas. Antes del cierre total del programa queda pendiente una corrección separada que revalide propietarios, compatibilidad de lectores estrictos, versión y rollout, con RED y revisión independiente. La corrección se concreta en el auxiliar253: 24 tareas pendientes, contabilizadas aparte de los 24 paquetes originales; ninguna está aceptada todavía.

P-08 quedó concretado documentalmente en 32 tareas pendientes (+18): prioridad manual y conflictos comunes entre agentes, con expiración que exige reevaluación y bloqueo conservador ante procedencia desconocida. Nueve documentos y sus 23 enlaces están revisados; no se ha implementado P-08 y depende de la aceptación real de P-03 y su protocolo de despacho.

La discrepancia de schemas se reprodujo con objetos válidos de los modelos actuales: el Plan ordinario pasa v1, mientras que un Plan con deployment_id, eventos de automatización (incluido target_rule_id null), un PhysicalIntent preparado con identidad opcional y un OptimizationResult con Plan de despliegue son rechazados por los schemas v1 exportados. La reproducción no modificó fuentes ni schemas; confirma un desacuerdo de contrato que exige corrección, además del diff del generador.

## Cierre conjunto A-11 y P-11 — 2026-09-11

A-11 y el mínimo P-11 quedan aceptados tras la verificación global documentada en el [informe de verificación A-11/P-11](verificacion-a11-p11-2026-09-11.md): **977 passed, 18 skipped, 793.19 s**, salida 0; la suite amplia registró **2121 passed, 155.53 s**. Ruff cubrió **24 archivos Python**, mypy **250 fuentes** y arquitectura **4 kept, 0 broken**. Graphify registró **11401 nodos y 31630 aristas**, con **0 enlaces colgantes** y **24 hashes** verificados; el HTML permanece obsoleto. Los recuentos se basan en coincidencias reales `T[0-9]{3}` de las tareas de cada especificación; las correcciones P-11 `R001–R004` quedan excluidas y las filas auxiliares se cuentan por separado.

El cierre enlazado deja documentado un efecto lateral del artefacto de commissioning: se conoce el hash original, pero no están disponibles los bytes originales para comparar; la corrección de aislamiento está verificada con las dos pruebas PostgreSQL del archivo (2 passed,16.61s,salida0); no recupera los bytes originales. Esto es una limitación de commissioning y no cambia la aceptación de A-11/P-11. Quedan además cinco familias de schemas en seguimiento (automation-event, automation-rule, optimization-result, physical-intent y plan) en la especificación 253: 24 tareas pendientes de aceptación, con diseño revisado y cuatro correcciones incorporadas; implementación iniciada por `/root/schema253_astra_builder` (Astra High), sin aceptación anticipada; no se cuentan como aceptadas.

## Medición del reparto de modelos: muestra observada

Se conserva el modelo/esfuerzo explícito y cada revisión por paquete; no se infiere coste a partir de la duración de pytest ni de las tareas. Para el cierre A11/P11: A11 fue implementado y revisado con Astra High y no tuvo un FAIL funcional en su revisión independiente final; P11 tuvo un FAIL independiente por exposición de errores del proveedor y una segunda revisión PASS después de corregirlo, además de la corrección local previa del alias de garantías. Estas son incidencias concretas, no tasas comparables entre modelos. La corrección de aislamiento del test posterior fue Terra Medium y pasó la verificación independiente raíz.

Los tokens facturados y el coste monetario por agente/paquete no están disponibles en las herramientas usadas; permanecen **no medidos**, no cero. El coste por paquete aceptado y el número total de intentos de generación no pueden calcularse con los logs de tests. La evidencia actual permite seguir correcciones y revisiones, pero todavía no demuestra que un reparto de modelos sea económicamente superior a otro.

P-09 ha quedado preparado en 30 tareas pendientes (+16), con nueve documentos, seis FR y tres SC literales y 33 enlaces comprobados. El diseño night/vacation reutiliza el cálculo DST de recurrence.py en una rama explícita para reglas asociadas, mantiene el comportamiento legacy, y delega una única tarea P03 por ocurrencia. P03/P04/P08, el consentimiento ligado al calendario y el protocolo de admisión siguen siendo dependencias por implementar/aceptar. Evidencia documental: `/tmp/domoai-p09-root-doc-validation.json`.

## Preparación P-07 revisada — 2026-09-11

Los diez documentos de `specs/246-adaptive-household-planning` concretan 30 tareas pendientes a partir de 14 (+16), conservan 7 FR y 3 SC y tienen 41 enlaces internos válidos. El perfil inicial solo propone; cualquier reemplazo requiere autorización exacta y el protocolo real de P-03/P-04/P-08. La comparación mantiene fijo el plan restante y expresa beneficio estimado. Ninguna tarea de implementación, calibración ni aceptación de producto se cierra por esta revisión documental.

## Preparación P-12 revisada — 2026-09-11

Los diez documentos de `specs/251-household-operations` concretan 32 tareas pendientes desde 14 (+18), conservan 7 FR y 3 SC y tienen 38 enlaces internos válidos. El mínimo de mantenimiento exige una copia coherente de ambos almacenes, restauración aislada que preserve hechos posteriores y retirada comprobada de accesos en todas las superficies. Recuperar lectura autenticada se distingue de reautorizar actuación física; RPO/RTO siguen siendo objetivos hasta ensayarlos. No se han ejecutado operaciones reales de datos ni aceptado implementación por esta revisión documental.

## Auxiliar 253 — historia de correcciones antes de la aceptación

El primer freeze local pasó 182 pruebas focales, 683 contratos y los controles estáticos, pero la revisión independiente **FAIL** reprodujo divulgación de identificadores históricos fuera del ACL, clasificación histórica de otro tenant al consultar por ID y confirmación indebida de una elevación de versión mínima SQLite cuando fallaban tanto commit como rollback. La suite amplia de raíz registró **2143 passed, 2 failed**; las dos últimas corresponden a fixtures de recuperación que construían versiones contradictorias.

El escritor exclusivo `/root/schema253_astra_builder` (Astra / High) está corrigiendo esas fronteras con RED→GREEN. `/root/schema253_astra_final_review` (Astra / High) realizará la revisión posterior. T014/T018/T022/T023 se reabren y T024 permanece pendiente de revisión y controles finales; **las 24 tareas del auxiliar253 todavía no cuentan como aceptadas**. Se conserva la evidencia del primer FAIL en `/tmp/domoai-schema253-independent-final-review.md` y `/tmp/domoai-schema253-root-broad1.json`. A-12 espera el cierre real de este paquete.

La revisión independiente del segundo freeze pasó su alcance (83 pruebas y oráculos propios); la suite amplia de raíz pasó 2145 pruebas. Sin embargo, el control global terminó **FAIL: 1032 passed, 18 skipped, 4 failed** en 864.68 s. Tres fallos eran fixtures de despacho que validaban v1 antes de añadir fencing; el cuarto reveló una recuperación que añadía deployment a un plan legado y lo dejaba incoherente con v1. Se prepara el tercer freeze: recuperación modifica solo estado/fecha y conserva identidad, versión, digest y grants; las fixtures configuran deployment antes de validar. Se preservan los resultados fallidos y no se acepta T024 anticipadamente. Evidencia: `/tmp/domoai-schema253-root-global-final.json` (el nombre «final» identifica el intento fallido, no un PASS).

## Preparación P-06 revisada — histórico registrado y dependencias

Astra contrastó el diseño con fuente local y documentación/código oficiales de Home Assistant; el perfil preparado calcula coste según un snapshot de estadísticas registradas, con `basis`, `as_of`, cobertura de registros y cobertura física desconocida explícitas. Conciliar ese registro no certifica factura, medición física ni ahorro causado. La diferencia frente al baseline permanece estimada y el ahorro causal permanece no evaluable. La primera integración propuesta es acotada a estadísticas de 5 minutos, hasta 12 horas y dos contadores explícitos, dentro de 500 registros con anclajes; no interpola huecos ni convierte datos ausentes en cero. La versión HA de investigación no es una release instalada o cualificada del usuario.

La revisión independiente inicial encontró un ciclo P06↔P12 y referencias consumidoras obsoletas. Luna / Medium alineó la documentación, y root corrigió una sustitución accidental de la dependencia A12 antes de la segunda revisión Astra / High. P06 acuerda e implementa su contrato mínimo de conservación, export y restore; P12 consume después P06 aceptado y demuestra la operación integral. P07/P12 conservan exactamente el significado y límites de la evidencia registrada.

**PASS documental**, no aceptación de producto: 14 Markdown modificados, 18 hashes revisados, 72 enlaces locales válidos, FR/SC literales y tareas P06/P07/P12 `32/30/32` todavía pendientes. Informe `/tmp/domoai-p06-optionb-independent-review2.md`; verificación raíz `/tmp/domoai-p06-optionb-root-alignment-validation.json`. No código, pruebas de aplicación, DB, hardware ni conexiones al HA del usuario por esta preparación. T003 conserva los contratos concretos y la cualificación de protocolo pendientes de ejecutar. El recuento de aceptación sigue sin cambios.

## Auxiliar253 aceptado — cierre 2026-09-11

**24 tareas aceptadas PASS WITH RISKS**, según la [verificación y Composition Review Report](../specs/253-runtime-schema-compatibility/verification.md). El freeze3 corrige recuperación sin alterar identidad legada y serializa la elevación del floor SQLite. Global raíz: **1041 passed, 18 skipped, 838.70s, exit0**; suite amplia: **2145 passed, 140.19s, exit0**; focal233/contratos683; Ruff38/mypy251/arquitectura4kept0broken. Revisión independiente Astra3:69 pruebas más oráculos SQLite/PG reales, PASS. Las revisiones/fallos previos arriba son históricos y no describen el estado actual.

Graphify AST38:11531 nodos/32473 aristas/cero extremos ausentes; HTML y reporte semántico obsoletos. Guardas192/124schemas/11datos íntegras respecto al baseline conocido. Se conservan la incidencia anterior de commissioning y el límite diagnóstico D1 de SQLite; no se cualifica hardware ni activación real de binarios antiguos. T024 cerrado; **219 aceptadas de553,334 pendientes**. A12 queda habilitado para implementación exclusiva Astra High; todavía no se acepta ninguna de sus22 tareas.

## Preparación P01 posterior a253

A12 se ejecuta por `/root/a12_astra_impl`, GPT-6 Astra / High explícito con fork none. La raíz y Astra revisan P01 en lectura; Terra / Medium prepara navegador real en un directorio temporal. No hay escritores P01 de código concurrentes.

La raíz reprodujo un límite abierto: ACL brightness devuelve también temperature por cobertura/get_state sin selector; la selección ajena explícita sí se deniega. Se integra RED/GREEN y SDK en P01 antes de aceptación, preservando el alcance histórico de P11 área/dispositivo. Informes temporales de proyección/auth y recuperación registran hallazgos; no se aceptan tareas ni se cambia219/553 por estos preparativos.

## A12 aceptado — cierre posterior a F01

**22 tareas aceptadas PASS WITH RISKS**, según [verificación y Composition Review Report](../specs/239-energy-currency-integrity/verification.md). Astra High implementó contabilidad y corrigió el lector de comparaciones; otro Astra High revisó de forma independiente. El freeze1 aprobó gates pero falló cuatro casos de comparabilidad pública. El freeze2 comparte la misma regla entre productor y lector, conserva legacy/metadata parcial y aprueba 145 pruebas independientes.

Global final: **1061 passed, 18 skipped, 893.87 s**, exit0; suite amplia: **2209 passed, 147.83 s**, exit0. Matriz focal572, Ruff26, mypy251 y arquitectura4 conservados. Schemas124 históricos +1nuevo, fuente173 y11artefactos protegidos exactos frente al baseline conocido. No se recuperaron los bytes originales del incidente commissioning anterior. Graphify AST26: 11603 nodos/33066 relaciones/cero extremos ausentes; HTML/informe semántico permanecen obsoletos. No se cualifican FX, hardware ni ahorro causal.

**241 aceptadas de553;312 pendientes.** A01–A12 cierran la secuencia técnica. P01 tiene [diseño revisado](../specs/240-household-onboarding/design-review.md) y [contrato de transferencia](../specs/240-household-onboarding/contracts/secret-transfer.md), pero sus24 tareas siguen pendientes. Backend Astra, harness/UI Terra y revisión Astra se ejecutan mediante cesiones separadas. Preparar o revisar documentos no cuenta como implementación.

## Inicio de ejecución P01 tras cierre A12

Astra High `/root/p01_astra_backend` registra T001/T002 y el baseline definitivo de propietarios. Terra Medium `/root/p01_terra_browser_prepare` implementa T003 con harness real de navegador; el backend espera su revisión antes de T004. No comparten propietarios de escritura: Astra reserva backend y fixture HA/repository común; Terra tiene harness y dependencias dev mínimas. La interfaz de producto T013–T018 espera la revisión independiente T012. Las24 tareas de P01 todavía no se cuentan como aceptadas; el total permanece **241/553**.

## P01 — T003 revisado y backend en ejecución

T001–T003 completadas localmente. Terra entregó el harness Chromium/HTTP y raíz repitió el gate: 1 passed en7.39s; siete owners y once guardas de datos exactos. T003 acredita tooling únicamente, sin aceptación de UI, accesibilidad, HA ni SDK. [Verificación P01](../specs/240-household-onboarding/verification.md) registra entrega y límites.

Astra High tiene GO T004–T011 como escritor exclusivo backend, con revisión independiente T012 antes de UI Terra. Una revisión Astra adicional comprueba el harness congelado. El total de paquetes aceptados permanece **241/553**, con312 pendientes; estas marcas parciales de P01 no elevan aceptación del programa.

## P01 — hito backend T004–T007 revisado

Revisión Astra independiente del snapshot y correcciones: **160 passed en15.37s**, Chromium CSP PASS; 955 archivos efectivos verificados. F01 (errores500 sin cabeceras) y F02 (IDs status/inventory confundidos con rutas de colección) se reprodujeron y corrigieron; los informes FAIL y sus oráculos se conservan. La revisión temprana acredita autenticación/headers, despacho exacto, DTO/esquemas e instalación vacía, sin sustituir T012 ni aceptación P01.

T008 ya reproduce retirada de fuente, conexión distinta y dispositivo sin área bajo ACL de áreas; sus controles de lectura HA HTTP, timestamp, stale y vacío pasan. Los límites MCP de capacidades tienen RED separado. Astra continúa T009/T010 (credenciales, persistencia y recuperación); T011 cerrará esas lecturas/ACL. **241/553 aceptadas**, sin incremento por hitos parciales.

## P01 — persistencia y revisiones de cancelación en curso

Astra High mantiene la escritura exclusiva del backend; otros subagentes Astra High revisan copias congeladas. Los tramos de temporales, adopción atómica SQLite/PostgreSQL, floor3 y respuesta perdida tienen revisiones parciales favorables tras corregir sus hallazgos. Esto no acepta T010, T012 ni P01. La [verificación P01](../specs/240-household-onboarding/verification.md) conserva RED, GREEN, revisiones y límites por tramo.

La revisión de limpieza encontró E-F01: reentrar en una sesión tras doble cancelación liberaba el pin de otra transferencia pendiente. Su corrección pasa el oráculo original, pero la segunda revisión reproduce E-F02: una rama de recuperación permite activar mediante una sesión cancelada explícitamente. La revisión de concurrencia reproduce G-F01: cancelar durante la desconexión del adaptador antiguo publica el reemplazo desde un finally. E-F02 y G-F01 siguen pendientes de corrección y nueva revisión independiente en este hito. No se ha demostrado borrado de un archivo adoptado por E-F01 ni se han utilizado actuadores físicos.

La revisión de diseño autoriza un puntero interno nullable de procedencia en la tabla existente de conexiones para distinguir adopciones vigentes de referencias durables prestadas. Su implementación y pruebas de reinicio/copia están en curso; no altera DTO ni contratos públicos, no infiere propiedad de limpieza desde una copia de la base y no constituye integridad criptográfica de la BD. Raíz verificó 125 schemas y 22 migraciones anteriores sin cambios y 11 artefactos de datos contra el baseline conocido de esta fase.

Terra retomará T013–T018 después del gate backend T012; browser y SDK siguen pendientes. **241/553 aceptadas, 312 pendientes**. Las pruebas parciales se solapan y no se suman como una suite única; costes y tokens por paquete siguen sin medir.

## P01 — backend T001–T011 terminado localmente; T012 en revisión

El freeze final del backend contiene 987 entradas y 34 propietarios de cambios. Raíz verificó sus hashes y la igualdad de toda la fuente de producto con el candidato revisado; el cierre solo ajusta tres archivos de pruebas y dos documentos. Las revisiones independientes cierran E-F01/E-F02 con 26 pruebas, G-F01/G-F02 con 24 y F-F01/recuperación con 68 casos pertinentes. Sus informes conservan los fallos anteriores y no suman las suites solapadas.

El estado doméstico consulta los diagnósticos históricos mediante metadata scoped de la base, sin acceder a archivos de credenciales. La validación completa sigue en arranque/diagnóstico de conexión. Lectura y proyecciones MCP aplican límites de capacidades, área y fuente efectiva. La sustitución cancelada conserva recursos pendientes de limpieza hasta poder reintentarla, sin publicarlos como adaptadores activos.

La regresión backend produjo 252 PASS y 2 FAIL por expectativas antiguas de floor; corregidas esas expectativas, las seis pruebas de migración pasan. Una regresión adicional produjo 66 PASS y 1 FAIL por el nuevo campo privado de persistencia; el archivo corregido pasa sus cuatro pruebas. Además pasan 169 controles MCP/ACL/cobertura, Ruff sobre 24 archivos, mypy sobre 12 archivos y cuatro contratos de arquitectura. Son ejecuciones distintas; no se afirma una única corrida final de 254 PASS.

T012 está asignada a un subagente Astra High independiente sobre el freeze final. El escritor backend queda detenido hasta feedback causal; Terra aún no inicia T013–T018. El [registro de verificación](../specs/240-household-onboarding/verification.md) documenta 11 tareas locales completadas de 24, con revisión final, browser, SDK y gates globales de P01 pendientes. El programa mantiene **241/553 aceptadas y 312 pendientes**.

## P01 — T012 aprobado; interfaz cedida a Terra

La [revisión independiente final del backend](../specs/240-household-onboarding/evidence/t012/independent-review-pass.md) aprueba freeze2 y cierra T012-F01. Respecto al primer cierre, el código del backend existente solo añade una línea de limpieza en attach cancelado; no se ha construido otro backend. Se verificaron los oráculos originales y las fronteras pendientes de HTTP real, lectura HA, permisos MCP e historial: 28 casos finalmente positivos y cuatro contratos de arquitectura, con fallos de arnés documentados por separado.

T001–T012 están completadas localmente. `/root/p01_terra_ui`, Terra Medium, tiene la escritura exclusiva de la interfaz existente y sus pruebas T013–T018; el backend queda estable. El harness conservará el gateway vivo para el posterior cliente SDK de T019. El programa sigue en **241/553 aceptadas y 312 pendientes**: P01 requiere terminar interfaz, SDK y gates conjuntos antes de aceptarse.

## P01 aceptado — cierre T013–T024

P-01 queda aceptado **PASS WITH RISKS** con sus 24 tareas. El recorrido real de ensayo enlaza navegador Chromium, API HTTP loopback, proveedor Home Assistant de prueba y el mismo cliente SDK: consulta catálogo y estado del hogar con una credencial de lectura distinta y demuestra que la ejecución queda denegada. Todo se integró en el backend existente; no se creó una segunda implementación.

La revisión independiente final corrigió una fuga de recursos de Home Assistant cuando `connect` fallaba o se cancelaba antes de publicar el adaptador. El gate de composición terminó con **1235 PASS/18 SKIP** e Import Linter 4/0; la suite amplia unit/contract/security terminó con **2279 PASS**. Mypy sobre 253 fuentes, Ruff, sintaxis Node y revisión de diff también pasan. Los 18 skips son pruebas live/HIL opt-in sin endpoints, credenciales, mappings o hardware y no se cuentan como PASS.

Graphify quedó actualizado en modo AST local con **11948 nodos, 35337 relaciones, 873 comunidades y cero enlaces colgantes**. Quedaron fuera 24 archivos SQL por faltar el parser opcional; tampoco se regeneraron el informe semántico ni el HTML. Los 11 artefactos protegidos permanecen byte a byte según comprobación exclusiva por hash.

El estado total pasa a **265/553 tareas aceptadas y 288 pendientes**. Pendientes por paquete: P-02 27, P-03 30, P-04 28, P-05 34, P-06 32, P-07 30, P-08 32, P-09 30, P-12 32, cierre P-10 3 y cierre P-11 10. Se detiene la ejecución antes de P-02 por instrucción del usuario. No hubo commit, push, despliegue ni ejecución sobre hardware; el coste y los tokens por paquete no se midieron desde las herramientas del repositorio.

## P02 — fundamento aceptado con riesgos, 2026-09-11

Al cierre inicial, P02 aceptó **16 tareas** (`T001–T009`, `T011–T015`, `T017–T018`) con escritor y revisor independientes **GPT-6 Astra / High**, ambos con contexto mínimo y propietarios exclusivos. La [verificación P02](../specs/241-household-preferences/verification.md) conserva RED/GREEN, los tres defectos importantes de la primera revisión y su re-revisión PASS, la corrección posterior del filtro por alias y los hashes de evidencia. Se ejecutaron contratos, integración, composición, seguridad, arquitectura, Graphify AST, revisión de diff y 11/11 artefactos protegidos.

Los gates amplios finalizan con **2025 passed, 18 skipped y 1 failed** (contrato/integración/composición) y `project-composition-check` con **1257 passed, 18 skipped y 1 failed**. El fallo único, reproducido por `ldd`, es externo a P02 y P01: Chromium/Playwright no puede cargar `libasound.so.2` del host. La carrera multi-host v2 que falló una vez antes del candidato final pasó aislada y en ambas corridas finales; se registra como intermitencia de P10/P11, no como regresión P02.

En ese cierre inicial, P02 completo tenía **11 tareas** pendientes: T010 (backup/restore nativo PostgreSQL de P12), T016 (evidencia directa de privacidad audit/log) y T019–T027 (integración real P03/FR-006). Por ello P04/P03 no se iniciaron entonces. El estado era **281/553 aceptadas y 272 pendientes**. No hubo commit, push, despliegue ni hardware real; no se infirieron tokens/coste desde duraciones.

## P02 — T016 aceptada tras revisión independiente, 2026-09-11

T016 queda aceptada como tarea de evidencia: **17/27** tareas P02 aceptadas y **282/553** combinadas (**271 pendientes**). Astra High escritor y revisor independiente hicieron una ronda de corrección: la revisión exigió que privacidad atravesara restore de servicio, su replay y `history_unavailable` posterior al erase. La re-revisión aprobó sin cambio de producción. Root verificó **67 passed** y Ruff/format PASS sobre los dos archivos propietarios. La prueba usa un centinela literal en fixture SQLite real y comprueba que no queda en respuestas, errores, cursor paginado, logs, stdout/stderr ni outbox/audit local del fixture.

Al cierre de T016 no se repitieron Graphify ni el gate global: aquel candidato añadía sólo pruebas y no cambiaba código estructural. Entonces persistían T010 backup/restore nativo PostgreSQL, P03/FR-006, efectos/fencing, colectores externos y borrado físico; el estado posterior de T010 se registra a continuación. No hubo commit, push, despliegue, hardware real ni estimación de tokens/coste por duración.

## P02 — T010 aceptada, mínimo PostgreSQL de P12, 2026-09-12

T010 queda aceptada **PASS WITH RISKS**: P02 pasa a **18/27** tareas y el programa a **283/553 aceptadas, 270 pendientes**. Astra High escritor y revisor independiente trabajaron con propietarios exclusivos. El mínimo crea/restaura un archive lógico PostgreSQL real hacia un destino nuevo y aislado, lo mantiene en cuarentena durable hasta verificarlo y rota las generaciones antes del bootstrap. No marca tareas P12 ni acredita su operación completa.

La revisión independiente encontró y cerró un P2 real: un destino con `COLLATION`, text-search, operador o esquema/tabla usuario preexistente podía pasar el predicado de vacío. Los RED/GREEN corren con `postgres:16-alpine`, `pg_dump` y `pg_restore` reales; el control final de root fue **19 passed, cero skips**. La batería transversal terminó **2266 passed, 18 skipped, 1 failed** y el `project-composition-check` **1278 passed, 18 skipped, 1 failed**, ambos sólo por el conocido Chromium sin `libasound.so.2`; Import Linter fue 4 kept/0 broken. R2/R3 sólo aplicaron formato y anotaciones `cast`, comprobadas por otra revisión Astra, Ruff/formato/mypy y batería nativa, sin repetir gates globales no semánticos. Graphify AST quedó en 12157 nodos/35792 aristas/852 comunidades y los 11 artefactos protegidos siguen exactos por hash.

El siguiente paquete por dependencia es P04; P03 y T019–T027 de P02 no se inician aquí. Persisten límites: P02 aún no tiene FR-006, consumidor/reevaluación/fencing real, corte multi-store, retención/RPO/RTO, archive autenticado/cifrado ni reautorización física. No hubo commit, push, despliegue, hardware real ni inferencia de tokens/coste desde duración.

## P04 — T001–T008: preparación, DTO y prepare durable, 2026-09-13/14

P04 pasa a **8/28 tareas aceptadas** y el programa a **291/553 aceptadas, 262 pendientes**. Un escritor **GPT-6 Astra / High** con `fork none` revalidó dependencias, Graphify y fuente, concretó el protocolo R/E/C/F y congeló nueve artefactos; dos revisiones independientes Astra High, también de contexto mínimo, revisaron el diseño. Se reservó documentalmente `src/domoai/persistence/migrations/026_household_consent.sql`; T006 es código puro de DTO/proyección, no persistencia ni migración.

La primera revisión independiente rechazó T003 por un P1 de recuperación: una copia antigua puede olvidar un receptor B añadido y retirado después de la copia; ACK del inventario restaurado no demuestra su corte. La corrección R1 exige receipt/journal P12 externo a los datos reemplazados, exclusión positiva de autoridades antiguas de emisión, `D_max` y cotas de reloj, cuarentena durable y ACK de rutas actuales. La segunda revisión dio **PASS WITH RISKS documental**. La espera de cuarentena sólo impide nuevas entradas de permits cached: no confirma C histórico ni cambia E/UNKNOWN anterior. Sin esas pruebas durante T007/T009/T018, actuación P04 permanece `unavailable`.

Evidencia: Graphify actual `/tmp/domoai-p04-t003-graphify-query-2026-09-13.log`; escritor y manifiesto de nueve hashes `.superpowers/sdd/tasks/p04-t003-protocol-fix-r1.md`; aceptación raíz con hashes finales `.superpowers/sdd/tasks/p04-t003-root-acceptance.md` (contrato `behavior.md` SHA-256 `50c42f7d58f86eeacb32c7d8b41f54fce23330ee35eb548c437433c5b126b326`); FAIL inicial `.superpowers/sdd/tasks/p04-t003-protocol-rereview.md`; PASS R1 `.superpowers/sdd/tasks/p04-t003-recovery-review.md`. T004 añade dos archivos de fixture (`eb3ee3d985edb97d2a692c10a056a55c688ec132f1c56eda8a147a903ec05642`, `e9655015c23b9b4c9eec2fefeb0ff30fa18abecb57ed4d6a75045b8172923dfd`), revisados en `.superpowers/sdd/tasks/p04-t004-{writer,review}.md`; raíz ejecutó **4 passed en 20.37s** más Ruff formato/check. T005 añade el contrato RED después ampliado a 51 casos `347c487e357c12d82a7758a157d37f0f088e0dbfb4a20c996431c091a0b27147`; las revisiones R1 cerraron dos defectos de cobertura. T006 cerró **51 passed en 10.19s**, Ruff/formato, compilación y mypy de módulos nuevos; sus tres RED y hashes se conservan en `.superpowers/sdd/tasks/p04-t006-{writer,fix-r1,r1-review}.md` (domain `1a00ac383bbaa725e91a54626fc5cd45c9740204e8490221a12f92c8913080ee`). T007, solo RED durable, recibe **PASS WITH RISKS** de la revisión Astra R2: 82 casos nuevos (74 RED causales, 8 controles GREEN) y 6 legados GREEN en SQLite/PostgreSQL Testcontainers; root reprodujo los **27 RED SQLite** de B por fronteras ausentes y los seis controles. El reporte/hashes finales están en `.superpowers/sdd/tasks/p04-t007-red-{writer,r2-review}.md` (I `722f7246c42494934d9b19bbd3875a4e3f547c9b6d663021bf509330d86a0164`; R `ac0a1d9533c4c773f157a181a480306d5e3a246f5089c2447fc8563d730483ba`). Riesgos que T008 debe convertir en GREEN: APIs control/prepare/P02 helper aún ausentes, caché R1, scope mixto, nonce y carreras PostgreSQL; no se acepta T008/T021a, E/F/C, delivery, restore, P03 ni hardware. Los 14 errores mypy del exporter son iguales a HEAD y no se presentan como PASS. PostgreSQL fue Testcontainers efímero, no el lab. No se ejecutaron gates globales/hardware ni se infieren tokens/coste. El siguiente tramo permitido es T008 bajo Astra High; P03 no se inicia.

Hito interno T008 A/C (2026-09-14): **PASS WITH RISKS**, no aceptación de tarea ni cambio del total (**7/28**, **290/553**). Astra High corrigió R/control, nonce, caché y concurrencia de batch, añadió 026 y elevó floor a 4. La primera revisión halló y reprodujo que 026 podía quedar durable con floor 3; R1 publica floor 4 en la misma transacción y una revisión Astra fresca dio P0/P1/P2=0. Root verificó **44 passed**, Ruff y `git diff --check`. Evidencia/hashes: `.superpowers/sdd/tasks/p04-t008-ac-{writer,r1-review}.md`; 026 `821751779c3ccbbcbb725be630eddfa36aee2803605198c56525bcf11f2bb000`. Próxima dependencia exclusiva: T021a helper P02 real; B54/prepare, receiver, factory, recovery y gates globales siguen pendientes.

Hito interno T021a (2026-09-14): **PASS WITH RISKS**, sin aceptación de tarea ni cambio del total (**P04 7/28; programa 290/553**). Astra High implementó y otro Astra High revisó el handoff de snapshot P02 sobre la conexión real del llamador; el público conserva rollback y el helper no abre, cierra ni controla la transacción. Pasan **8** casos SQLite/PostgreSQL de handoff/error y **56** regresiones P02, junto a Ruff/formato/mypy focal y `git diff --check`. La revisión comprobó además que mutantes en memoria con commit o rollback interno hacen fallar los ocho oráculos. Hashes: `household_preferences.py` `6c9f0f8e93573a29c9da392b5e4b27e4ffdd61297e1f70b47a3a8242c421551a`, test owner `67763e8a310d25b0dea94d02989bb78771fdd6f4c35baa36cc3f5b34f6e85c6b`; evidencia `.superpowers/sdd/tasks/p04-t021a-{writer,review}.md`. El helper no aporta locks, wiring de factory ni prepare/autoridad: queda B54 y T021b; PostgreSQL INERROR exige rollback exterior. Ahora puede comenzar exclusivamente el source B de T008, sin iniciar P03 ni otro paquete.

Hito interno T008 B (2026-09-14): **PASS WITH RISKS** tras corrección y re-revisión Astra High, sin aceptación de tarea ni cambio del total (**P04 7/28; programa 290/553**). El primer review rechazó dos P1 (la expiración/identidad original de Plan no limitaba el primer prepare) y un P2 (un trigger podía corromper la proyección del intent y aun devolver `prepared`); R1 los reprodujo en SQLite/PostgreSQL y los cerró sin cambiar P02, factory, receptor, red ni migración. La revisión fresca pasa **111** B/T021a, **43** A/C+legados, **74** migración/compatibilidad, **5** ledger y seis probes; formatos de owner/Ruff/mypy/diff pasan. Hashes R1: `approval_store.py` `5bb81fbe5d068ff24295232ab4a1a1398c53e5bfdfbb7a5b796106ba4d3d871d`, `repositories.py` `a0f5bc23e5febb1d4e7e26d9d0e0e22057a3399997bde1fe1971383e89773dc6`, integration `c2b2908a062ab8b3be0a3e7056ff82738a652df6b373501cd7a64a4d270bc5b0`; informes `.superpowers/sdd/tasks/p04-t008-b-{writer,review,fix-r1,r1-review}.md`. Persisten formato global ajeno, conexión approval separada de factory y límites R/E/C/receptor/D_max/recovery/P03. Siguiente paso exclusivo: gates del candidato T008; no habilita entrega física.

## P04 — T008 aceptada PASS WITH RISKS, 2026-09-14

T008 completa control/R/nonce+grant y prepare durable, incluido el handoff real T021a, la migración 026/floor 4 y la copia PostgreSQL catalogada, pero **no** entrega física. La primera revisión final Astra encontró un P1 de límites temporales: `DeliveryProtocolLimits.model_copy`/`model_construct` podía evitar la validación anidada y preparar un permit con incertidumbre negativa. RED real: **4 failed** SQLite/PostgreSQL; la reparación mínima `revalidate_instances='always'` rechaza antes de leer/escribir autoridad. R1 Astra ejecutó 20 casos y trazas SQL de rechazo previo, con P0/P1/P2=0; writer confirmó **121 passed** B/T021a.

Raíz ejecutó la matriz focal final: **492 passed en 815.53 s** (SHA-256 log `f2dea12949912b7f0c855581b71b45c663576ec6833734da4a5f0b2090bd9753`). `project-composition-check` final: arquitectura **4 kept/0 broken** y **1380 passed, 18 skipped, 1 failed en 1565.73 s** (log `61e8511c9e3fb70208970af538eff844cbcb3457e86d907683566e6cced118cf`). El único fallo es externo y repetido: Playwright/Chromium termina exit 127 por faltar `libasound.so.2` en el host, antes de ejecutar DomoAI; `ldd` lo confirma. Los **11/11 artefactos protegidos** conservan SHA-256 y tamaño exactos frente al baseline histórico; no se abrieron bases protegidas. `git diff --check` pasa.

Graphify se reconsultó y contrastó contra fuente (`/tmp/domoai-p04-t008-final-graphify-query-2026-09-14.log`); el índice AST existente no contiene aún los símbolos nuevos de prepare y no se forzó un refresco global porque detectó 231 cambios, mayoritariamente documentos ajenos. Es riesgo documental explícito, no una afirmación de grafo actualizado. Informes: `.superpowers/sdd/tasks/p04-t008-{limits-writer,final-independent-review}.md` (writer `4280fcaef2286c777962b6c383b6953751c65cbf44ceb2c991d6843b262c412d`; revisión R1 `8859426133653afd2f27b479677b63950363db1aa2f54912171524df88862e65`). La subida de floor expuso causalmente una aserción P01 de equality; se reparó con revisión Astra como compatibilidad `floor >=3`, sin reabrir ni contar P01. Sin commit, push, merge, despliegue ni hardware.

Riesgos/límites aceptados: factory todavía no compone una conexión común de approval, no hay receptor/red, F/E/C ni ACK/inventario, `D_max`/reloj productivos, backup/restore/journal/exclusión de issuer, T021b, P03 ni FR-006. Por tanto prepare sigue `unavailable` para entrega y el siguiente trabajo permitido continúa dentro de P04 (T009), no P03 ni otro paquete.

## P04 — T009 A+B verificado, no aceptado, 2026-09-14

Tras concretar T009 y revisión Astra High independiente del corte, sólo A+B era ejecutable sin invadir P12/T018: copia P04 SQLite→PostgreSQL con fuente fixture quiescente, reapertura/reupgrade, y rollback/conflicto/retry de la rutina existente. El escritor Astra añadió cuatro oráculos permanentes con expected independiente; primer selector: **4 GREEN, 0 skips** con PostgreSQL Testcontainers real. No hubo RED causal ni cambio de producción: `postgres_migration.py` mantiene SHA-256 `486cc122765aba0434ffc435554f29669ce1bdc01a7927ef51e4c99ea8cd94ea`. Su regresión focal dio **58 passed, 0 skips**; Ruff/formato/mypy focal y diff pasan.

La revisión Astra fresca ejecutó **38 passed, 0 skips**, P0/P1/P2=0, y verificó que dos mutantes locales son detectados: omitir `consent_issuance_receipts` y confirmar la copia antes de la tabla tardía. El test candidato tiene SHA-256 `79d60ac9a59d5aeaee604ecfa359c7ef2810441cc31037de72dd4ddeb60bfe58`; informes `.superpowers/sdd/tasks/p04-t009-ab-{writer,review}.md` (review `c77a3c103faf4664af2f2685ebf1cdbad65d35f09c637c60cf79051777db3f8e`). **T009 no se marca aceptada y el total permanece 291/553; P04 8/28.** C requiere cesión P12; journal externo, exclusión de issuer, boot cerrado, B11, reloj/cotas y ACK/receptor siguen bloqueados por P12/T018. No se ejecutaron gates globales ni se reclama recuperación operacional, entrega disponible, P03, Docker lab ni hardware.

## P04 — T010 aceptada PASS WITH RISKS como RED, 2026-09-14

T010 fija la red de seguridad del adaptador residente sin implementar el adaptador: **22 RED** esperados (12 causales de `AuthorityPolicy`, 10 precursores de rutas/interfaces aún ausentes), **51 GREEN** y **169 regresiones adyacentes**, sin skips. El primer review Astra encontró un P2 de cobertura del scope humano: `scope` y `operations` inválidas coexistían. R1 separó el caso owner+operación válida+único `consent:human`; un mutante que abre ese scope devuelve 201 donde el control requiere 403 y fue detectado. La revisión R1 fresca deja P0/P1/P2=0; Ruff/formato/mypy focal y diff pasan. Evidencia `.superpowers/sdd/tasks/p04-t010-red-{writer,review}.md` (writer R1 `4703842d47b0e21b9da6ad72304d0f2f0cea23fd47965fc0d51779d2969e5361`, review R1 `1b959bc2df6a9ba1c58d01ce9d3f223c916cfe405cdb54ab159cc6ba57a1cac4`).

Aceptación limitada al RED: los diez precursores no acreditan sesión, nonce, expiración, origin residente ni separación de credenciales humana; T011–T014 han de alcanzarlos sin provider sintético. No cambió producción, Docker/hardware ni se ejecutaron gates globales. P04 queda **9/28** y el programa **292/553 aceptadas; 261 pendientes**. T009 sigue incompleta en C–F y no habilita restore/entrega.

## P04 — hito interno T011-A policy, 2026-09-14

El primer corte de T011 restringe sólo las tres operaciones residentes en `application/authority.py`: operación literal explícita, ACL/scope/purpose humano y rechazo fail-closed de omisión, lista vacía, operación ajena o `*`; la semántica heredada queda intacta fuera de ellas. RED previo: **15 failed/52 passed** tras añadir los vectores de wildcard; GREEN writer/revisor: **67 passed** en A, **10 precursores RED/77 GREEN** en T010 completo y **230 regresiones** sin skips. Cuatro mutantes y 5.760 vectores legacy fueron detectados/preservados en la revisión Astra PASS WITH RISKS (P0/P1/P2=0). Ruff/mypy/diff pasan; formatter falla únicamente por tres bloques preexistentes idénticos a HEAD y no se reformatearon. Evidencia `.superpowers/sdd/tasks/p04-t011-a-{writer,review}.md` (writer `50d12d298e618767d8bf42541b4adebae53d8b2a981d5cf297a78701806c4fff`; review `ffc7f50b52402f582483896c1a273410dd46734d6296c4c28c3f5499cf9b1761`).

No es aceptación T011 ni habilita rutas, adaptador, sesión, challenge, credencial, emisión, grant o delivery; el siguiente corte requiere RED HTTP funcional y costura/errores explícitos. El ledger sigue **292/553; P04 9/28**.

## P04 — hito interno T011-B unavailable HTTP, 2026-09-14

T011-B añade sólo el rechazo seguro previo a host residente: los tres POST exactos responden 503 tipado o 429 por cuota compartida antes de verifier/auth/audit/Origin/body/manager; no acceden a autoridad ni crean estado. Resultado final: **111 GREEN**, T010 **2 RED C–D/85 GREEN**, 169 regresiones. El primer review Astra encontró dos P2 de cobertura: lecturas `manager.list()` y `request.body()` podían sobrevivir. R1 hace que accesos manager/audit/get fallen y usa stream de último chunk retenido para demostrar respuesta antes de materializar el cuerpo sin afirmar cero lecturas físicas. 35 recorridos stream consecutivos no dejan tareas; los cuatro mutantes fallan seis veces cada uno y la restauración devuelve 111 GREEN. R1 PASS WITH RISKS P0/P1/P2=0; estáticos/diff pasan. Informes `.superpowers/sdd/tasks/p04-t011-b-{writer,review}.md` (writer `57f93a0dfe485c3f8e1cbbf1cf33e2654d8ef10cadc061fbeb610f20fa20160d`; review `fec8f8203913769717da4c85ec6d5cc434db071c5d8263f5fe4e69bcf1841bd9`).

No configura credencial/origen, adaptador, sesión, challenge, emisión/grant ni delivery, por lo que T011 continúa pendiente. El ledger sigue **292/553; P04 9/28**.

## P04 — hito interno T012-A settings/snapshot, 2026-09-14

T012-A añade sólo configuración por pareja y snapshot efectivo profundamente inmutable: **67 GREEN** A, T011-B conserva **111 GREEN**, regresión **280 GREEN** con un precursor C conocido. El primer review Astra halló que una subclase mutable de `datetime` podía compartirse y que el test no detectaba publicación records/procedencia mezclada sin lock. R1 materializa fecha base UTC y añade barrera reload/snapshot determinista; ocho mutantes fallan sin errores de harness y la restauración conserva cinco SHA. R1 PASS acotado P0/P1/P2=0; Ruff/source mypy pasan; formato/tipos de tests sólo arrastran baseline previo. Evidencia `.superpowers/sdd/tasks/p04-t012-a-{writer,review}.md` (writer `11d7c795016d0aa7bcb8ae424cd7037ab9548b5f75a17ebf0e19e9dd861fc255`; review `f8985b8737900a3f2bae7067a231dc8facd1f0a3855939f9ec6b14b9a7e97d6d`).

No prueba inventario completo de peers activos, separación vigente, reload D2, sesión/HTTP positivo, emisión/grant o delivery; T011/T012 no se aceptan y el ledger se mantiene **292/553; P04 9/28**.

## P04 — hito interno T011-C1 admisión residente, 2026-09-14

T011-C1 implementa admisión local con ficheros y verificadores reales, snapshots de peers y origen/host canónicos, sin callback/flag sintético: **82 GREEN** C1, B 111, A 67, legacy 42 y seguridad **279 GREEN / 1 precursor C**. El primer review halló P2 bilateral de hashes inactivos; R1 añade el espejo peer disabled/revoked/expired con residente válido y procedencia distinta. Filtros mutados de residente y peer generan tres fallos causales cada uno y restauran 82 GREEN. R1 Astra PASS WITH RISKS P0/P1/P2=0; estáticos/diff pasan. Informes `.superpowers/sdd/tasks/p04-t011-c1-{writer,review}.md` (writer `03d387d379f651d35bcd0438fc9f632f9a18b2f460509d1490087ebed4847643`; review `c96aa89034d348f3ebf329cd66cf0f6caf37f993bf5a848b738c34d133f24abc`).

No activa HTTP, sesión, review, challenge, assertion, grant o delivery y no acredita inventario productivo completo/reload; T011/T012 siguen pendientes. El ledger se mantiene **292/553; P04 9/28**.

## P04 — T011 C2–C7 y C8 (confirmación por chat), 2026-11-02

Este apartado se reconstruye del historial git, no de los informes SDD (`.superpowers/sdd/tasks/` no está en el repositorio). No cambia el ledger **292/553; P04 9/28**: T011–T014 y P04 siguen sin aceptación formal.

**C2–C7** (`ddf2657`): consentimiento del hogar con sesión residente, revisión con challenge de un solo uso, decisiones aprobar/rechazar/revocar, recibos durables idempotentes, cabeza de control durable, listado de aprobaciones activas y huella de revocación, composición y despacho en el gateway (`management/consent.py`, `mcp/gateway.py`, `mcp/consent_review_source.py`), migración `026_household_consent.sql`, esquemas JSON `household-consent-{view,decision}` y tests unitarios, de integración, contrato y seguridad. Verificado el 2026-11-02: 2225 tests pasan con `-k "not postgres"`, y tras arrancar `dockerd` en el entorno la suite `-k postgres` completa pasa (**245 passed, 0 fallos**, Testcontainers efímero, no el lab). Un test de migración (`test_t009_copy_reopen_reupgrade_preserves_consent`) fallaba por un `ORDER BY 1, 2` con empates distintos entre SQLite y PostgreSQL; se corrigió el test con orden total, sin cambios de producción.

**C8** (`e544d6c`, `d8d368c`, PR #26 en `main`): confirmación humana por el chat del cliente MCP mediante elicitation (`mcp/chat_confirmation.py`, `mcp/domotics_server.py`), extracción de `application/client_tokens.py` desde `mcp/auth.py`, ajustes CI de composición Docker y documentación en `docs/unified-mcp.md`. El grant emitido lleva `approved_by` `chat:<client>`, `authentication_context` `mcp_elicitation`, un solo uso y `expires_at`; el nuevo test `test_the_issued_grant_is_attributed_single_use_and_bounded` (`d8dc230`) lo comprueba. Los 36 tests de seguridad de C8 pasan.

**Revisión RED de C8 (veredicto FAIL previo):** los dos P0 (store reemplazado, tenant del cliente HTTP) ya no se reproducen en el código actual; los P1 de formularios laxos y de `revoke_approval` están cubiertos en `test_chat_confirmation_hardening.py`. Cerrados después: el test de "código fresco" era vacuo (el cooldown cortaba la segunda llamada) y ahora comprueba que el segundo diálogo se pregunta con un código distinto; se añadió el test de `_meta` falsificado (se ignora y la persona sigue siendo consultada). Sin comprobar: el nombre del setting `chat_confirmation_timeout_seconds` frente al freeze, que no está en el repositorio.

**Pendiente:** revisión independiente del source posterior a C7, verificación manual con un cliente MCP real, y aceptación de T011–T014.
