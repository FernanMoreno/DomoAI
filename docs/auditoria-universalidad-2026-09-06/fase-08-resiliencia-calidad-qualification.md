# Fase 08 — Resiliencia, calidad y qualification del conjunto

[Índice](README.md) · [Skills e instalación](fase-07-skills-empaquetado-instalacion.md)

Estado: evidencia de software/laboratorio disponible en la auditoría original; acceptance universal pendiente. Hallazgo propietario: U-09. Entrada: recopilar desde fase 00; cierre final tras fases 01–07 según perfil seleccionado.

## 1. Objetivo

Demostrar que los componentes funcionan conjuntamente ante fallos, reinicios, cambios de estado, concurrencia y límites de recursos. La aceptación final debe declarar el perfil validado y sus límites, no una garantía sobre cualquier casa.

Se mantienen tres cierres separados: software, dependencias de laboratorio y equipo físico. Una fase puede cerrar software mientras su qualification física permanece bloqueada por una dependencia externa identificada.

## 2. Evidencia anterior y su alcance

| Familia | Aprobadas | Omitidas |
|---|---:|---:|
| Unitarias | 1.041 | 0 |
| Contrato | 356 | 0 |
| Integración | 406 | 17 |
| Composición | 125 | 1 |
| Rendimiento | 13 | 0 |
| Total | 1.941 | 18 |

El gate global aprobó 531 casos y omitió 18; esos casos se solapan con la suite completa. Duraciones anteriores: 736,12 s y 677,73 s, con ejecución parcialmente simultánea. No son benchmark de capacidad del producto.

Pasaron dependencias Docker de broker MQTT, etcd y PostgreSQL y runners multi-host con ownership race, partición/takeover, crash/replay, pérdida de control plane, failover de primario, rotación, restore y carga acotada. Eso es evidencia valiosa de laboratorio, no qualification de radios o inversores reales.

No se repitieron estas ejecuciones para dividir la auditoría en fases. Su origen está en la [sección de pruebas del informe integral](../auditoria-integral-universalidad-2026-09-06.md#10-pruebas-y-resultados-de-esta-auditoría).

## 3. U-09: gates que pasan con cobertura externa omitida

El pipeline exige jobs exitosos, pero pytest puede devolver éxito con skips. El helper global `project-composition-check` busca `tests/contracts` en plural; el proyecto usa `tests/contract`, por lo que ese helper seleccionó composición e integración sin incluir la carpeta singular. La suite completa adicional sí la incluyó.

No se debe modificar a ciegas un helper global que sirve a otros proyectos. Primero determinar si la cobertura se completa mediante comando de repositorio, configuración o corrección del helper bajo alcance explícito.

El cierre con evidencia exige declarar qué dependencias son obligatorias para cada gate. El job de software ordinario puede omitir hardware; un job cuyo objetivo es cualificar un broker o perfil no puede cerrar esa qualification omitiendo sus escenarios esenciales.

## 4. Superficie de resiliencia

| Frontera | Estado propietario | Fallo que debe observarse |
|---|---|---|
| Conector → CompositeAdapter | Conexión, colas y reconexión | Pérdida de fuente sin paralizar otras. |
| Evento → StateStore | Cursor, versión, snapshot | Duplicado/desorden sin regresión de estado. |
| StateStore → SQL | Commit y metadatos | Fallo durable sin publicar estado como confirmado en memoria. |
| Plan → claim | Estado de plan e intención | Dos callers no ejecutan la misma intención dos veces. |
| Claim → escritura | Lease/fencing | Propietario viejo no escribe tras perder autoridad. |
| Escritura → readback | Resultado físico | ACK perdido/feedback ausente no se convierte en éxito. |
| Resultado → audit/outbox | Evidencia durable | Fallo de audit no oculta efecto incierto. |
| Scheduler → recovery | Ocurrencia y status | Reinicio no duplica acción ni descarta incertidumbre. |
| Backup → restore | Snapshot y ownership | Integridad y autoridad válidas después de restaurar. |
| Gateway → cierre | Sesiones y tareas | Cancelación drena recursos de forma acotada. |

## 5. Matriz de fallos de extremo a extremo

### Antes de una escritura

Probar estado stale, capability eliminada, policy cambiada, aprobación vencida, claim ocupado, cola llena y pérdida de lease. Resultado esperado: rechazo/espera explícitos con cero invocaciones no autorizadas al actuador.

### Durante una escritura

Probar timeout, desconexión, cancelación del cliente y crash del proceso. No asumir que el timeout significa ausencia de efecto. Registrar intención, intento y evidencia suficiente para decidir reconciliación.

### Después de una escritura

Probar fallo de readback, error de persistencia y caída de auditoría. La próxima sesión debe poder distinguir éxito conocido, rechazo conocido e incertidumbre. No emitir un nuevo comando sólo para que el estado interno parezca completo.

### Durante un plan compuesto

Un miembro modifica el mundo y otro falla. Conservar orden, resultado por miembro, predecesores y estado del agregado. Una compensación, si existe para ese perfil, es otra acción con autoridad y riesgo; no un rollback SQL del dispositivo.

## 6. Persistencia, backup y recuperación

Fuentes principales: `src/domoai/persistence/sqlite.py`, `serialized.py`, `repositories.py`, `audit_outbox.py`, `backup.py`; `application/recovery.py` y `runtime_ownership.py`.

Comprobar migraciones desde la versión soportada, reinicio con WAL, locks, latencias del worker y comportamiento ante disco lleno. Las pruebas con SQLite en memoria no sustituyen fallos de filesystem y reapertura.

Para backup/restore, definir conjunto de bases y configuraciones necesarias, consistencia temporal, cifrado, integridad y custodia de claves. Probar restauración sobre destino temporal, no sobrescribir el estado doméstico para verificar el mecanismo.

El restore debe evitar reactivar leases o aprobaciones consumidas como si fueran nuevas. Revisar interacción con la semántica de privacidad de fase 03: restaurar una copia antigua puede reintroducir datos legítimamente presentes en ella.

## 7. Concurrencia y multi-host

Multiagente en un gateway y multi-host son perfiles distintos. Compartir un proceso simplifica ownership; distribuirlo exige coordinación y protección de la escritura en toda la ruta.

Verificar epoch, lease, pérdida de quorum, particiones y procesos rezagados. Un token de fencing comprobado sólo al inicio no basta si una cola entrega la orden después de perderlo. El alcance exacto de egress/proxy/adapters debe quedar documentado para el perfil usado.

El laboratorio multi-host anterior prueba escenarios concretos. No habilitar active-active de producción basándose en esos resultados sin satisfacer los gates del despliegue y su qualification. Tampoco introducir multi-host como requisito para una casa que no lo necesita.

## 8. Métricas y capacidad

Mediciones propuestas: latencia p50/p95/p99 de discovery/lectura/preparación/ejecución; lag de eventos; profundidad de colas; drops/coalescencia; ocupación de workers; tiempo SQL; memoria; solver queue wait; reconnects; estados stale; intenciones unknown y rechazos de fencing.

Definir antes del experimento tamaño de vivienda, número de fuentes, frecuencia de eventos, número de clientes y concurrencia. Registrar hardware, duración, warmup y carga de fondo. No inventar un SLO numérico ni deducirlo de los 13 tests de rendimiento anteriores.

Una cola acotada puede rechazar trabajo para preservar estabilidad; la aceptación debe evaluar si ese rechazo es observable, seguro y recuperable. Eliminar límites para que un benchmark no rechace solicitudes no es una mejora demostrada.

Sólo considerar un bus externo si las mediciones muestran una necesidad no resuelta por el diseño local y se especifican orden, replay, backpressure y operación. No poner un LLM en el camino de cada evento.

## 9. Qualification física por perfil

El perfil debe identificar hardware/modelo/firmware, protocolo, conector, mapping, cableado o radio relevante, límites, feedback y operador responsable. Cada prueba registra precondición, comando, resultado esperado, observación y criterio de interrupción.

Categorías de evidencia a mantener separadas:

1. Contrato/modelo: prueba de forma y validación.
2. Fixture: respuesta controlada, sin equipo externo.
3. Dependencia real: broker/base de datos/proceso en laboratorio.
4. Simulación o gemelo: dinámica modelada, no entorno físico.
5. HIL atendido: equipo identificado y condiciones controladas.
6. Perfil cualificado: conjunto de evidencia vigente y límites de uso.

Las 18 omisiones anteriores corresponden a opt-in/configuración/credenciales de KNX, HA/HIL, batería, EV, multi-adapter live, Matter, Modbus, OMIE, Open-Meteo, bootstrap y Zigbee2MQTT. Registrar cada bloqueo y su requisito para levantarlo. El tiempo transcurrido no convierte ausencia de evidencia en aprobación.

## 10. Tareas propuestas

### F08-T01 — Gates verificables

- [ ] Definir gate de software, de dependencia real y de perfil físico.
- [ ] Asegurar inclusión explícita de `tests/contract`.
- [ ] Registrar skips por caso y fallar la qualification si falta un escenario obligatorio.
- [ ] Vincular resultados a SHA/worktree, versiones y entorno.

### F08-T02 — Recuperación y concurrencia

- [ ] Inyectar fallos antes/durante/después de escribir.
- [ ] Probar replay, doble caller y reinicio con intención incierta.
- [ ] Comprobar outbox, restauración y propiedad exclusiva.
- [ ] Revisar todos los resultados consumidos por reglas, bundles, Skills y MCP.

### F08-T03 — Operación y capacidad

- [ ] Definir perfil de carga representativo y SLO acordado.
- [ ] Medir latencias, memoria, colas, drops y estado stale de forma aislada.
- [ ] Probar cierre bajo cancelación y fuentes desconectadas.
- [ ] Documentar límites en lugar de extrapolar escala no medida.

### F08-T04 — Qualification atendida

- [ ] Preparar perfil y procedimiento de prueba por equipo.
- [ ] Obtener alcance concreto para las acciones físicas necesarias.
- [ ] Ejecutar y archivar resultados y límites de cada prueba.
- [ ] Definir cambios de equipo/mapping/firmware que invalidan la evidencia.

## 11. Comandos de cierre futuro

Ejecutar con el entorno de pruebas aislado de credenciales y endpoints domésticos:

```bash
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python scripts/check_architecture_contracts.py
uv run --frozen lint-imports
uv run --frozen python scripts/check_runtime_contract_docs.py
uv run --frozen pytest tests/unit tests/contract tests/integration tests/composition tests/performance -rs
project-composition-check "$(cat .ai/project-name)"
git diff --check
```

Estos comandos son instrucciones futuras, no resultados nuevos. Evitar repetir pruebas amplias innecesariamente: ejecutar gates acordados y ampliar sólo por cambios, fallos o riesgos no resueltos. Los runners Docker crean y limpian recursos temporales; revisar su aislamiento antes de usarlos.

## 12. Entregable final y aceptación

El informe de cierre debe incluir subsistemas cambiados, vecinos revisados, invariantes, escenarios ejecutados, dependencias reales, fallos/causas, riesgos residuales y veredicto PASS / PASS WITH RISKS / FAIL para un alcance concreto.

- [ ] Fases propietarias de defectos funcionales cerradas con regresión.
- [ ] No quedan éxitos inventados ni autoridad perdida en productores de planes.
- [ ] Catálogo, cobertura, instalación y privacidad coinciden con el comportamiento.
- [ ] Arquitectura y contratos pasan en la revisión entregada.
- [ ] Dependencias obligatorias verificadas; omisiones identificadas.
- [ ] Restore, fallos parciales, retries y cierre de lifecycle probados.
- [ ] Hardware cualificado sólo cuando existe evidencia física vigente.
- [ ] Dictamen final expresa perfil y límites, sin «cualquier vivienda» sin condiciones.

La aceptación puede ser PASS para un hogar/perfil concreto y seguir siendo parcial para el objetivo universal amplio. Esa distinción debe mantenerse en README, programa, informes y recursos MCP.
