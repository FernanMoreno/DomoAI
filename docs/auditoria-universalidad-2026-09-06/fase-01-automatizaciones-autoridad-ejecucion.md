# Fase 01 — Automatizaciones, autoridad y veracidad de ejecución

[Índice](README.md) · [Base](fase-00-base-arquitectura-evidencia.md) · [MCP](fase-02-conformidad-interoperabilidad-mcp.md)

Estado: defectos abiertos según la auditoría original. Hallazgos propietarios: U-01, U-02, U-03. Entrada: baseline identificada. Prioridad: primera corrección funcional.

## 1. Objetivo y alcance

Lograr que una regla local y cada ocurrencia recurrente conserven la autoridad válida, representen una intención nueva cuando corresponda y comuniquen fielmente el resultado físico. El trabajo comprende productor de plan, validación, admission, executor, persistencia y proyecciones de resultados.

No implica autorizar todas las automatizaciones sensibles, cambiar el consentimiento humano ni añadir un executor. Tampoco significa que una regla ya aprobada mantenga permisos indefinidamente.

## 2. Flujo y datos que deben cruzar las fronteras

```text
regla + plantilla + consentimiento + evento
    → decisión de elegibilidad y claim
    → plan específico de la ocurrencia
    → validación actual + autoridad + admission
    → executor + intención durable + adapter
    → resultados individuales + resumen + auditoría
```

| Dato | Origen de confianza | Debe conservarse o derivarse |
|---|---|---|
| Tenant y household | Autoridad validada en la creación de la regla | Conservar y revalidar para el despliegue destino. |
| Principal, roles y restricciones | Identidad y consentimiento persistidos | No reemplazar por `system/service` como efecto de defaults. |
| Versión de regla | Definición cuyo digest se aprobó | Distinguir modificaciones de una misma regla. |
| Evento/ocurrencia | Identidad estable del trigger aceptado | Mantener al reintentar; cambiar ante otra activación legítima. |
| Idempotency key | Derivación de la intención de esa ocurrencia | Igual para replay, diferente para nueva intención. |
| Resultado físico | `ExecutionOutcome` del executor | Preservar rechazo, fallo, desconocido y éxito confirmado. |

## 3. Evidencia de los tres defectos

### U-01: autoridad

`LocalAutomationEngine._execute_claimed` crea un plan con ID, comandos y expiración. `PlanService.create_plan` no recibe ni copia autoridad. La plantilla con `audit-tenant/audit-house/audit-operator` generó `default/default/system`, rol `service`.

Se reprodujo con PlanService y PlanExecutor reales sobre fixture. No es prueba de explotación entre hogares a través de un gateway real. La consecuencia observada es la pérdida de autoridad antes de la ejecución; fencing, auditoría y restricciones pueden recibir un contexto incorrecto.

### U-02: identidad de ejecución

El ID del plan cambia por evento, pero sus comandos conservan la clave de la plantilla. La segunda activación fue rechazada por el adapter con `Duplicate idempotency key`. En un ledger durable el mismo defecto puede tratar una intención nueva como replay.

No se corrige generando un UUID nuevo en cada retry: eso permitiría duplicar una escritura cuyo ACK se perdió. La identidad debe ser determinista respecto de la ocurrencia, no de cada intento de transporte.

### U-03: resultado

El motor sólo marca como desconocidos resultados UNKNOWN/UNAVAILABLE. REJECTED y FAILED terminan como `executed/plan_executed`. El rechazo real de U-02 reprodujo esa contradicción. Una colección vacía tampoco puede demostrar éxito físico.

La ruta recurrente de `scheduler.py` muestra por inspección construcciones y proyecciones análogas. Debe reproducirse específicamente antes de afirmar que comparte todos los efectos observados en reglas locales.

## 4. Fuentes y pruebas de partida

Fuentes: `src/domoai/application/local_automation.py`, `plan_service.py`, `scheduler.py`, `execution_admission.py`, `executor.py`, `authority.py`; `domain/automation.py`; `runtime/approval_store.py`; `persistence/repositories.py` y ledger de intenciones.

Pruebas existentes:

- `tests/unit/application/test_local_automation.py`.
- `tests/unit/runtime/test_scheduler.py` y `test_recurrence.py`.
- `tests/contract/test_local_automation_contract.py`.
- `tests/integration/test_local_automation_persistence.py`.
- `tests/unit/runtime/test_executor_authority.py`.
- `tests/composition/test_physical_authority_closure_composition.py`.

La [reproducción original](../auditoria-integral-universalidad-2026-09-06.md#11-reproducción-mínima-de-los-defectos-de-automatización) aísla la frontera posterior al claim. Las regresiones finales deben recorrer también registro, consentimiento, claim persistente y activación por scheduler/evento.

## 5. Requisitos de autoridad por ocurrencia

- El plan conserva household, tenant, principal y restricciones de la autoridad que corresponde a la regla.
- La pertenencia al hogar destino se comprueba antes de reclamar permiso de escritura.
- Copiar un contexto histórico no sustituye la decisión explícita sobre revocación y expiración del consentimiento persistente.
- Una actualización de regla cambia el digest y exige la autoridad correspondiente a su nueva definición.
- La expiración durante una cola o entre validación y escritura vuelve a evaluarse donde el contrato lo exija.
- La auditoría puede relacionar regla, aprobación, evento, plan e intento sin exponer tokens.
- Los registros legacy deben tener una política de compatibilidad explícita: no promover automáticamente una autoridad ausente a privilegio general.

Decisión a concretar en Spec Kit: relación entre revocación de un token de cliente y vigencia de una aprobación de automatización. Son autoridades distintas; no asumir ni cancelación automática de toda regla ni continuidad ilimitada.

## 6. Requisitos de idempotencia

La identidad propuesta debe incorporar al menos el ámbito de hogar, regla, versión de definición, ocurrencia y miembro del plan. Es un requisito lógico; la representación exacta y su hash se definirán al implementar.

| Situación | Comportamiento exigido |
|---|---|
| Dos eventos distintos tras cooldown | Dos intenciones, aunque ordenen el mismo valor. |
| Reenvío del mismo evento | Una intención; no repetir efecto físico. |
| Reinicio después del claim | Recuperar la identidad de esa ocurrencia. |
| ACK perdido tras escritura | Estado incierto y reconciliación; no clave nueva automática. |
| Misma regla con nueva definición | Identidad separada de la versión anterior. |
| Dos hogares con IDs locales iguales | No colisionar en persistencia ni fencing. |
| Hora repetida por cambio horario | Política explícita de ocurrencia, estable tras reinicio. |

La clave de idempotencia no prueba que el valor siga siendo físicamente correcto. Readback y freshness continúan siendo gates independientes.

## 7. Requisitos de proyección de resultados

No fijar nuevos enums sin revisar consumidores y esquemas. La tabla define significado exigido:

| Resultados de miembros | Significado que debe comunicarse |
|---|---|
| Todos confirmados y conjunto no vacío | Éxito confirmado. |
| Rechazado antes de escribir | No ejecutado; causa de rechazo. |
| Fallo conocido | Fallo; preservar evidencia individual. |
| Un miembro desconocido | Incertidumbre visible, aunque otros hayan funcionado. |
| Éxitos y rechazos/fallos | Resultado parcial, no éxito total. |
| Cancelación antes de escritura | Cancelado sin efecto, si se demuestra. |
| Cancelación después de posible escritura | Parcial/desconocido según evidencia. |
| Sin outcomes | No hay evidencia de éxito; diagnosticar contrato incompleto. |

Comparar motor local, recurrencias, bundles, escenas, MCP y Skills. Un consumidor no puede reinterpretar un rechazo como cumplimiento del objetivo por el hecho de que el coroutine terminó sin excepción.

## 8. Paquetes de trabajo propuestos

### F01-T01 — Reproducciones de composición

- [ ] Convertir los tres casos originales en regresiones con expectativas de comportamiento correcto.
- [ ] Usar repositorios temporales, servicios reales y adapter controlado.
- [ ] Añadir hogar nominal y restricciones por dispositivo/área/capability.
- [ ] Repetir desde una ocurrencia recurrente y registrar diferencias.

### F01-T02 — Materialización con autoridad

- [ ] Definir el contrato común que reciben los productores de planes.
- [ ] Preservar autoridad y rechazar combinaciones incompatibles de regla/consentimiento.
- [ ] Probar expiración, modificación y replay de definiciones antiguas.
- [ ] Verificar el mismo contexto en plan durable, admission, intención y auditoría.

### F01-T03 — Identidad por ocurrencia

- [ ] Derivar identidades estables entre retries y distintas entre eventos legítimos.
- [ ] Probar crash antes/después del claim y después de una escritura incierta.
- [ ] Verificar que no se debilita la protección antirreplay del adapter.
- [ ] Documentar cómo se tratan reglas antiguas con claves de plantilla ya usadas.

### F01-T04 — Resumen y consumidores

- [ ] Definir una proyección exhaustiva y comprobar colecciones vacías/mixtas.
- [ ] Revisar contratos MCP y esquemas si se amplían estados.
- [ ] Mantener detalles por miembro y referencias de auditoría.
- [ ] Comprobar que el scheduler no avanza o reintenta una ocurrencia contradiciendo su resultado.

## 9. Matriz mínima de aceptación

1. Regla de encendido, hogar nominal: dos eventos aceptados generan dos intenciones y dos resultados coherentes.
2. Replay del segundo evento: no tercera escritura.
3. Regla fuera del scope: rechazo antes de invocar el adapter.
4. Consentimiento vencido: cero escrituras y causa visible.
5. Reinicio entre primera y segunda activación: se conserva el aislamiento entre ocurrencias.
6. Cambio de inventario entre validación y dispatch: revalidación o rechazo coherente.
7. Adapter rechaza: regla no figura como éxito.
8. Escritura aceptada y readback perdido: desconocido, sin retry físico ciego.
9. Plan de dos miembros con fallo parcial: efectos y no efectos quedan identificados.
10. Regla recurrente durante cambio horario: comportamiento definido y reproducible.

## 10. Comprobaciones futuras y cierre

```bash
uv run --frozen pytest tests/unit/application/test_local_automation.py tests/unit/runtime/test_scheduler.py tests/unit/runtime/test_recurrence.py tests/unit/runtime/test_executor_authority.py
uv run --frozen pytest tests/contract/test_local_automation_contract.py tests/integration/test_local_automation_persistence.py tests/composition/test_physical_authority_closure_composition.py
```

Estos comandos ejecutan las pruebas existentes; no sustituyen las regresiones nuevas. Tras una corrección significativa, ejecutar también arquitectura, contratos afectados, suite aplicable y `project-composition-check "$(cat .ai/project-name)"` en un entorno aislado de la vivienda.

Cierre: U-01/U-02/U-03 reproducidos y resueltos; recurrencias revisadas; compatibilidad documentada; cero éxito inventado; consentimiento y fencing preservados; revisión de composición con resultados actuales. Esta fase no cualifica hardware ni resuelve toda la cobertura semántica.
