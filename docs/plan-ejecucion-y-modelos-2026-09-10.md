# Plan de ejecución y reparto de modelos

Fecha: 2026-09-10. Orquestación actualizada: 2026-09-11.

Estado: propuesta del usuario registrada para guiar el desarrollo. Este documento no acredita implementación ni pruebas superadas.

## Objetivo y orden de trabajo

Corregir primero los fallos técnicos, un paquete cada vez, y después construir un recorrido doméstico completo sobre esa base.

1. **Ejecución fiable:** A-02 cancelación → A-03 ledger → A-01 horarios → A-04 readiness. Empezar por A-02: una orden cancelada antes del despacho no debe actuar después. Concretar el punto de no retorno y conservar el seguimiento de un despacho ya iniciado, conforme a su especificación.
2. **Permisos y estado fiable:** A-05 a A-08, respetando las dependencias entre administración de conexiones, normalización de estados, inventario visible y capacidades efectivas.
3. **Contabilidad energética:** A-09 a A-12: demanda base, comparación pasiva, origen de los flujos solares y coherencia monetaria.
4. **Primer recorrido de producto:** conectar cliente y proveedor, descubrir la vivienda, guardar preferencias, aprobar una tarea y consultar su resultado desde otro agente. Coordinar onboarding, preferencias, consentimiento, tareas persistentes y cualificación del cliente según sus dependencias.
5. **Ahorro y autonomía:** medición, prioridad manual, avisos y replanificación; completar los paquetes de rutinas, cobertura y operación previstos en la planificación.

El [índice de paquetes](workflow-auditorias-2026-09-10/README.md) contiene las especificaciones, planes, tareas y dependencias. La planificación documental registra **24 paquetes y 336 tareas**; no equivale a trabajo implementado. Las tareas demasiado amplias deben concretarse al abordar cada paquete.

Primer paquete: [A-02 — especificación](../specs/229-queued-dispatch-cancellation/spec.md), [plan](../specs/229-queued-dispatch-cancellation/plan.md) y [tareas](../specs/229-queued-dispatch-cancellation/tasks.md).

## Ciclo obligatorio por paquete

Aplicar [AI_WORKFLOW.md](../AI_WORKFLOW.md) y las instrucciones del proyecto:

1. Inspeccionar `git status` y preservar el trabajo ajeno al paquete.
2. Revalidar especificación, plan, tareas y dependencias con Spec Kit; contrastar Graphify con la fuente actual antes de implementar trabajo significativo.
3. Para fallos, aplicar `systematic-debugging`: reproducir, localizar la primera frontera incorrecta e identificar la causa raíz.
4. Escribir una prueba de regresión que falle por el defecto identificado y comprobar ese fallo antes de corregirlo.
5. Implementar el cambio mínimo que satisfaga el contrato y preserve el comportamiento circundante.
6. Ejecutar las pruebas aplicables, incluidas arquitectura, contratos, integración y composición según el alcance. Para trabajo significativo ejecutar también:

   ```bash
   project-composition-check "$(cat .ai/project-name)"
   ```

7. Aplicar `system-composition-review` a los cambios significativos y revisar de forma independiente los cambios sensibles.
8. Revisar el diff final, decidir si procede actualizar Graphify o persistir conocimiento durable y aplicar `verification-before-completion`.
9. Registrar pruebas ejecutadas, resultados reales, limitaciones y tareas completadas. Si un gate falla, corregir su causa o informar del fallo; no declararlo superado.

Cerrar cada paquete con evidencia antes de avanzar. No hacer commits, push, merges ni publicaciones sin una petición explícita.

## Modelos para desarrollar el código

Reparto propuesto por el usuario; no constituye una evaluación comparativa propia del proyecto.

| Trabajo | Modelo propuesto | Esfuerzo inicial |
|---|---|---|
| Cancelación, concurrencia, autoridad, migraciones y contabilidad | GPT-6 Astra | High |
| Implementación cotidiana con contrato claro | GPT-5.6 Terra | Medium |
| Revisión independiente de cambios importantes | GPT-6 Astra, sesión separada | High |
| Documentación y transformaciones mecánicas acotadas | GPT-5.6 Luna | Medium |
| Documentación compleja, auditorías mecánicas amplias y consolidación de evidencia | GPT-5.6 Luna | XHigh |

Configuración inicial: Astra para cerrar los fallos críticos, Terra para el producto y Astra para revisar cambios sensibles. Luna XHigh queda autorizado para documentación compleja, auditorías mecánicas amplias y consolidación de evidencia; usar Luna Medium en ediciones pequeñas para reducir consumo. Sol queda como alternativa para cambios complejos si se prefiere reservar Astra. La disponibilidad depende de la cuenta y del cliente.

Referencia aportada en la propuesta: [Modelos oficiales](https://learn.chatgpt.com/docs/models). Este registro conserva la recomendación recibida; no verifica de nuevo el catálogo ni la disponibilidad.

Medir coste por paquete aceptado, reintentos y defectos encontrados. Todavía no hay una evaluación propia que demuestre qué combinación funciona mejor en DomoAI.

## Coordinación de agentes

Mantener un único agente escribiendo por subsistema. Paralelizar después paquetes independientes y revisiones, con propiedad explícita de archivos y contratos.

Cancelación y ledger comparten demasiadas fronteras para modificarlos simultáneamente sin coordinación. Ejecutarlos secuencialmente al principio y revisar sus interacciones.

### Orquestación eficiente para los paquetes restantes

Aplicar estas reglas desde P-02:

1. La raíz coordina, verifica aceptación y mantiene el estado. Cada paquete usa normalmente dos subagentes: un escritor y un revisor independiente. No crear un subagente por tarea.
2. Lanzar agentes con contexto mínimo (`fork none` o solo los turnos imprescindibles) y un brief que enlace especificación, tareas, propietarios, checks y criterio de parada. La fuente y los documentos enlazados aportan el detalle.
3. Terra Medium escribe cuando el contrato está claro. Astra High escribe o toma el relevo cuando aparecen autoridad, concurrencia, persistencia, migraciones, contabilidad, recuperación o fallos causales repetidos. Astra High revisa los cambios sensibles en una sesión separada.
4. Luna Medium resuelve ediciones documentales pequeñas. Luna XHigh consolida documentación compleja, auditorías mecánicas amplias y evidencia de cierre. XHigh no se usa por defecto en cambios breves.
5. Dentro del paquete, agrupar pruebas por frontera: reproducir RED, ejecutar GREEN focal y ampliar a regresiones relacionadas. Ejecutar la suite amplia y `project-composition-check` una vez sobre el candidato final, repitiéndolos solo si una corrección posterior afecta su alcance.
6. Guardar logs, freezes e informes extensos en archivos. Los subagentes devuelven únicamente hallazgos, archivos modificados, comandos, resultados y límites; no duplican código, especificaciones ni logs completos en el chat.
7. Paralelizar solo paquetes o revisiones sin propietarios ni estado compartidos. Mantener secuenciales las dependencias P-02, P-04, P-03 y P-08; abrir paralelismo después de estabilizar ese núcleo.
8. Escalar esfuerzo por evidencia: Medium como inicio cotidiano, High para fronteras sensibles y XHigh solo ante ambigüedad real, revisión amplia o fallos repetidos. No repetir investigación ya registrada si fuente y hashes no han cambiado.
9. Medir por paquete aceptado: reintentos, defectos encontrados por revisión, duración de gates y, cuando el cliente lo exponga, tokens/coste. No inferir tokens a partir de duración o cantidad de pruebas.

Esta orquestación reduce duplicación de contexto y ejecuciones globales sin rebajar `AI_WORKFLOW.md`, las pruebas RED/GREEN, la revisión independiente ni los gates de aceptación.

## Modelos dentro de DomoAI

Mantener el runtime independiente del LLM:

- El agente interpreta la intención y propone.
- OR-Tools optimiza.
- El runtime valida y ejecuta.

El objetivo para el primer producto es funcionar con el modelo del cliente conectado, sin exigir contratar otro modelo interno. Su funcionamiento deberá demostrarse con el recorrido doméstico y la cualificación de clientes previstos.
