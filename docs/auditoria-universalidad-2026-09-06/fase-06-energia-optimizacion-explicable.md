# Fase 06 — Energía y optimización explicable

[Índice](README.md) · [Adaptación](fase-05-adaptacion-identidad-commissioning.md) · [Skills](fase-07-skills-empaquetado-instalacion.md)

Estado: revisión ampliada de capacidades existentes, sin nuevos defectos reproducidos. Dependencias: fases 01, 04 y 05. Consume U-01/U-02/U-03/U-07/U-08, pero no cambia sus fases propietarias.

## 1. Objetivo

Demostrar que las propuestas energéticas usan datos coherentes, expresan límites e incertidumbre y se ejecutan exclusivamente a través del ciclo normal del runtime. El resultado del solver no debe confundirse con ahorro medido o autorización de actuador.

La fase evalúa cargas flexibles, batería, EV, confort térmico, tarifas, solar y restricciones de potencia según perfiles disponibles. No presupone que toda vivienda tenga esos componentes.

## 2. Componentes revisados en la baseline

| Componente | Responsabilidad |
|---|---|
| `optimizer/scenario.py`, `domain/energy.py` | Forma y validación del problema. |
| `optimizer/providers.py` | Contexto compuesto, horizonte, revisiones y frescura. |
| `optimizer/omie.py`, `open_meteo.py` | Datos externos específicos. |
| `optimizer/cp_sat.py` | Modelo y resolución CP-SAT; propuestas. |
| `application/process_optimization_worker.py` | Presupuesto y aislamiento del solver. |
| `application/optimization_service.py` | Composición con estado y plan service. |
| `optimizer/product.py`, `counterfactual.py` | Resúmenes y comparación. |
| `application/dynamic_safety.py`, `battery_composition.py` | Condiciones de operación y límites físicos. |
| `config/battery_profile.py`, perfiles EV/solar | Configuración explícita de equipos e instalación. |

Rutas relativas a `src/domoai/`. Las pruebas anteriores respaldan software y laboratorio; no prueban los parámetros de una batería o edificio real no identificados.

## 3. Contrato temporal y de unidades

Un contexto energético debe indicar horizonte exacto, zona horaria, resolución, timestamps, revisiones de fuentes y unidades. El solver no debe juntar una tarifa de un día con una previsión solar de otro por coincidencia de número de puntos.

Comprobar conversiones entre W/kW, Wh/kWh, potencia/energía y SOC porcentual/energía almacenada. La convención de signo de importación/exportación y carga/descarga debe ser única o traducida explícitamente.

| Incidencia de datos | Comportamiento a verificar |
|---|---|
| Falta una fuente necesaria | Escenario bloqueado o alternativa explícita que no dependa de ella. |
| Fuente vencida | No presentar propuesta como actual ejecutable. |
| Hueco en serie temporal | Diagnóstico; no inventar puntos silenciosamente. |
| Resolución distinta | Transformación definida o rechazo. |
| Cambio horario | Horizonte y slots coherentes con instantes reales. |
| Revisión cambia tras optimizar | Revalidación antes de preparar/ejecutar. |
| Internet cae | Lecturas/control local continúan según sus dependencias; optimización puede quedar bloqueada. |

## 4. Restricciones físicas y operativas

### Batería

Capacidad válida, límites de SOC, reserva, eficiencia, potencia, degradación y feedback. La batería simulada no acredita inversor real. Verificar binding, qualification y readback antes de convertir slots del solver en planes.

### Vehículo eléctrico

Conectividad, energía requerida, deadline, potencia admitida y estado de carga. Desconectar el vehículo después de optimizar debe invalidar o impedir la orden afectada. El consumo debe relacionarse con energía entregada y no sólo con duración prevista.

### Climatización

Consigna y confort dependen de un modelo térmico y de parámetros. Probar límites de temperatura, anti-ciclo, feedback y perturbaciones. Un modelo simplificado puede ser útil sin que sus predicciones sean garantía de confort real.

### Solar y tarifas

Una previsión es una entrada incierta. OMIE no es automáticamente la tarifa final de cualquier contrato: impuestos, cargos, zonas y condiciones de exportación deben representarse según el perfil elegido. No generalizar una fuente de mercado a cobertura mundial.

## 5. Semántica del resultado del solver

Separar: entrada inválida, problema infactible, timeout, solución factible, óptimo probado y resultado desconocido. No emitir recomendaciones ejecutables a partir de un resultado que no aporta una trayectoria válida.

La explicación debe poder reconstruir objetivo, restricciones, supuestos, datos, coste estimado y alternativas. Si se publica confianza, definir qué significa; no presentarla como probabilidad calibrada de éxito físico sin validación.

La comparación debe usar contextos compatibles y explicar qué cambió. No atribuir una mejora a la política si también cambiaron tarifas o previsión sin declararlo.

## 6. Composición de trayectoria a ejecución

Una propuesta puede generar varios miembros ordenados. Cada uno conserva autoridad y dependencias y puede quedar invalidado por estado nuevo. Confirmar que:

1. El optimizador no escribe al adapter.
2. El bundle conserva identidad de escenario y dependencias.
3. Las aprobaciones corresponden al conjunto y a su ventana.
4. Predecesores físicos requeridos tienen éxito confirmado.
5. Un fallo parcial impide asumir cumplida toda la trayectoria.
6. La siguiente ocurrencia no reutiliza una intención vieja por U-02.
7. La Skill no interpreta `executed` erróneo de U-03 como ahorro conseguido.

No exigir atomicidad física de todos los slots: definir qué se detiene, qué queda incierto y qué requiere reconciliación.

## 7. Matriz de validación propuesta

| Caso | Evidencia mínima |
|---|---|
| Tarifa estable y una carga flexible | Coste calculado contrastable manualmente. |
| Límite de potencia imposible | Infactibilidad explicada; cero escritura. |
| Batería sin qualification | Propuesta o diagnóstico conforme al contrato; ejecución bloqueada. |
| SOC cambia tras solución | Dependencia invalidada antes de actuar. |
| EV se desconecta | Ruta afectada bloqueada y resto del plan explicado. |
| Feedback HVAC no llega | No declarar cumplimiento térmico confirmado. |
| Solver agota tiempo | Worker termina/libera recursos; runtime responde. |
| Variación de escenario infactible | Comparación sin beneficio inventado. |
| Caída del proveedor externo | Caché/frescura y diagnóstico coherentes. |
| Batería acepta y siguiente miembro falla | Estado parcial durable y no replay ciego. |

## 8. Tareas propuestas

### F06-T01 — Datos y modelo

- [ ] Inventariar qué entradas requiere cada perfil de escenario.
- [ ] Probar unidades, alineación, huecos, revisiones y caducidad.
- [ ] Establecer ejemplos pequeños con resultado verificable sin confiar sólo en el solver.
- [ ] Documentar límites del modelo térmico, tarifa y forecast.

### F06-T02 — Explicación y worker

- [ ] Comparar respuesta con restricciones realmente introducidas al modelo.
- [ ] Probar estados factible/óptimo/infactible/timeout y salida sin solución.
- [ ] Verificar cancelación, colas y recuperación del pool.
- [ ] Medir presupuesto sin mezclarlo con qualification física.

### F06-T03 — Ejecución segura de propuestas

- [ ] Recorrer MCP → propuesta → preparación → consentimiento → bundle → executor.
- [ ] Introducir cambio de estado y fallo parcial entre miembros.
- [ ] Verificar autoridad por ocurrencia y proyección de resultados de fase 01.
- [ ] Contrastar feedback medido con trayectoria prevista, sin equipararlos.

## 9. Pruebas de partida

```bash
uv run --frozen pytest tests/unit/optimizer/test_energy_providers.py tests/contract/test_energy_context_contract.py tests/contract/test_energy_provider_contract.py
uv run --frozen pytest tests/integration/test_energy_optimization.py tests/integration/test_energy_skill_workflow.py tests/composition/test_energy_contract_ownership_composition.py
uv run --frozen pytest tests/performance/test_energy_optimizer_targets.py
```

Ejecutar rendimientos de forma aislada si se pretende sacar conclusiones de capacidad. Los smoke externos OMIE/Open-Meteo requieren configuración deliberada y no se convierten en PASS por estar omitidos.

## 10. Cierre

- [ ] Escenarios aceptados tienen datos coherentes y restricciones verificables.
- [ ] Ninguna propuesta adquiere autoridad física directamente.
- [ ] Explicación y resultado reflejan límites e incertidumbre.
- [ ] Cambios de estado invalidan dependencias donde corresponde.
- [ ] Fase 01 cerrada antes de aceptar automatización energética repetida.
- [ ] Ahorro estimado separado de ahorro medido y de qualification de equipos.

Salida: workflows energéticos demostrables por perfil. Riesgo residual: calibración física, tarifas reales y clima necesitan evidencia de la instalación concreta.
