# Auditoría de producto de DomoAI

Fecha: 2026-09-10. Código de referencia: `b9f5094fc1a28b0f209ca7f3d4f332f8d8a789f3` más el árbol de trabajo local. Esta evaluación complementa la [auditoría técnica](../auditoria-integral-2026-09-10/README.md); no vuelve a enumerar sus defectos ni los considera corregidos.

## 1. Dictamen

**DomoAI tiene más desarrollo de infraestructura que desarrollo de experiencia doméstica.** Hay suficientes componentes para construir un producto diferenciable: modelo semántico, conectores, herramientas MCP, políticas, programación, optimización y evidencia de ejecución. Falta convertirlos en un recorrido continuo que una persona pueda instalar, comprender, delegar y evaluar sin preparar escenarios técnicos cada vez.

La promesa recomendada es:

> Cualquier agente compatible puede entender y ayudar a gestionar tu casa mediante un mismo contrato. Tú decides qué puede hacer; la casa conserva sus reglas y comprueba el resultado. Cuando hay flexibilidad energética, propone y mide mejoras de coste y confort.

«Compatible» debe tener una matriz verificable. «Universal» debe expresar extensibilidad y semántica común, con cobertura publicada por capacidad. «Ahorrar» debe depender de las cargas, la tarifa y los datos disponibles; no todas las viviendas tienen el mismo potencial.

**La siguiente entrega debería cerrar una experiencia completa sobre capacidades existentes.** Otro protocolo o diez tools adicionales aportan poco si conectar una vivienda, aprobar una tarea o comprobar su resultado sigue requiriendo intervención técnica.

## 2. Para quién y para qué

Hipótesis de segmentación; no proceden de entrevistas ni de ventas:

| Usuario | Trabajo que necesita resolver | Prioridad |
|---|---|---|
| Responsable técnico de una vivienda | Conectar su instalación, limitar agentes y automatizar sin perder control | Primer usuario de instalación |
| Habitante no técnico | Pedir algo, saber qué ocurrirá, cambiarlo y entender el resultado | Usuario diario desde el piloto |
| Hogar con contador y cargas flexibles | Reducir coste manteniendo plazos, confort y reservas | Primer caso energético demostrable |
| Desarrollador o instalador | Añadir un dispositivo o preparar una vivienda sin rehacer todo | Segundo canal, apoyado en SDK y recetas existentes |
| Operador de muchas viviendas | Administración, soporte y aislamiento a escala | Expansión posterior; no motor del siguiente incremento |

No conviene exigir batería, paneles y coche para usar DomoAI. Deben existir dos recorridos complementarios: **control doméstico útil desde el primer día** y **optimización energética cuando haya datos y actuadores adecuados**.

Ejemplo de hogar sin gran flexibilidad: control por habitación, escenas, horarios y diagnóstico pueden justificar el producto, aunque no pueda atribuirse un ahorro económico significativo.

## 3. Qué conserva valor propio frente a lo existente

La comparación es documental y acotada, no un benchmark de rendimiento, seguridad ni ahorro:

| Referencia | Función documentada | Implicación para DomoAI |
|---|---|---|
| Home Assistant MCP | Expone herramientas de control y contexto para entidades seleccionadas | Conectar un agente a luces ya no diferencia por sí solo. [Fuente](https://www.home-assistant.io/integrations/mcp_server) |
| Home Assistant Energy | Presenta flujos energéticos y seguimiento del hogar | Otro panel de consumo genérico tiene poco valor incremental. [Fuente](https://www.home-assistant.io/home-energy-management/) |
| EMHASS | Optimización anticipada y control predictivo que reajusta planes con observaciones | Tener un solver tampoco es una ventaja suficiente. [Fuente](https://emhass.readthedocs.io/en/latest/main_core_concepts.html) |
| Predbat | Planificación de batería con previsión solar y consumo histórico, además de comparación de tarifas | El ciclo de previsión y revisión tiene referencias prácticas con las que compararse. [Fuente](https://springfall2008.github.io/batpred/what-does-predbat-do/) |

La oportunidad propuesta —todavía no una superioridad demostrada— está en combinar **intenciones compartidas entre agentes, control semántico de varios sistemas, consentimiento verificable y resultados domésticos comprensibles**. Su defensa debe basarse en recorridos y perfiles cualificados, no en una lista de tecnologías.

Se compararon tres enfoques:

1. **SDK/MCP para desarrolladores.** Aprovecha lo construido y reduce trabajo de interfaz; limita adopción a personas capaces de montar el resto de la experiencia.
2. **Runtime universal con recorridos domésticos preparados. Recomendado.** Mantiene MCP y SDK, aprovecha Home Assistant cuando esté presente y añade una superficie pequeña para permisos, tareas, resultados y optimización. Permite probar utilidad sin sustituir toda la domótica.
3. **Plataforma doméstica completa que reemplaza el sistema existente.** Exige replicar instalación, paneles, automatizaciones y soporte de equipos; amplía mucho el esfuerzo antes de demostrar la diferencia.

El enfoque recomendado conserva la independencia de Home Assistant mediante conectores nativos. HA sería el primer recorrido de instalación más aprovechable, no una dependencia obligatoria del modelo universal.

## 4. Estado real de las capacidades de producto

«Implementado» significa presencia verificable en código y, donde se indica, pruebas. No significa experiencia validada con usuarios ni funcionamiento acreditado en cualquier hardware.

| Capacidad | Estado | Brecha de producto |
|---|---|---|
| MCP único y neutral respecto al modelo | Implementado | Instalación y autenticación verificadas por cada cliente/host, no solo por SDK |
| Descubrimiento, capacidades, estado e histórico | Implementado | Explicar cobertura de la vivienda, duplicados, áreas y datos que faltan |
| Control, planes, escenas y recurrencias | Implementado | Guardar, editar y usar rutinas mediante lenguaje y controles cotidianos |
| Consentimiento y permisos | Implementado en backend | Flujo humano completo de aprobación, revocación y autonomía graduada |
| Automatizaciones locales | Implementado | Catálogo cotidiano, gestión de conflictos y resultados visibles; pendiente gate técnico |
| EV, batería, solar y HVAC | Implementado en modelos y flujos | Configuración doméstica, adaptación continua y pruebas por equipo |
| Resumen y comparación de propuestas | Implementado | Traducir evidencia técnica a coste, plazo, confort y alternativas comprensibles |
| Nueve skills portables | Implementado | Evaluar tareas reales con agentes; no confundir contrato validado con comprensión del usuario |
| Control plane de conexiones | Implementado | Recorrido de instalación guiado y experiencia diaria fuera de conexiones |
| Consumo y ahorro posterior a ejecución | Parcial: histórico y objetivos del solver reutilizables | No se encontró un recorrido completo de ahorro atribuible con baseline y calidad de datos |
| Objetivos y preferencias de hogar compartidos | Parcial: planes, reglas, políticas y perfiles específicos | No se encontró un registro general de objetivos domésticos con prioridad, vigencia y excepciones |
| Avisos domésticos y bandeja de oportunidades | No localizados como producto completo | Hay eventos/auditoría; falta entrega al habitante, prioridades y seguimiento |
| Actualización, backup y privacidad | Componentes existentes | Procedimiento sencillo y verificable para mantener, recuperar y entregar una vivienda |

Evidencias de fuente y alcance de las pruebas: [evidencia.md](evidencia.md).

## 5. Auditoría del recorrido del usuario

### Descubrir si le sirve

La presentación inicial debe responder: qué sistemas están soportados, qué puede hacer en su casa, qué necesita instalar y qué seguirá funcionando si desconecta el agente.

El README actual arranca por entorno de desarrollo y configuración de servidores. Es útil para ingeniería; falta una entrada por resultado: «controlar mi casa», «programar una rutina» o «evaluar ahorro». Añadir ejemplos separados para vivienda sencilla, EV y solar/batería, con requisitos y límites reales.

### Instalar y conectar

La interfaz actual muestra conectores, campos de endpoint/credenciales y configuración OAuth. Es una base de instalación técnica. Los estados `connected`, `degraded` o `remove` aparecen como etiquetas; que el test encuentre esas palabras no demuestra un recorrido operable de recuperación o retirada.

La experiencia requerida es: elegir sistema, conectarlo, ver dispositivos reconocidos, resolver lo desconocido, probar una lectura y una acción permitida, conectar el agente y finalizar con un resumen de cobertura. Mostrar únicamente los campos pertinentes y separar configuración avanzada. Para el primer mercado español, priorizar lenguaje claro en español y uso móvil; hoy el HTML inspeccionado declara inglés.

### Entender la vivienda

Un inventario de entidades no contiene por sí solo intención doméstica. Hace falta distinguir «termo que puede esperar», «coche que sale a las 8», «habitación que debe estar confortable» y «aparato excluido de automatización».

Crear un perfil editable de vivienda: habitantes autorizados, habitaciones, nombres preferidos, horarios, tarifa, cargas flexibles y límites. Conservar procedencia y vigencia de cada preferencia. El agente propone asociaciones ambiguas; la persona confirma las que cambian actuación. No deducir autoridad física de un nombre de dispositivo.

### Pedir y delegar

Las tools actuales permiten planificar, pero el flujo energético recibe un `OptimizationScenario` ya estructurado. El agente todavía debe completar bastante trabajo de interpretación y modelado.

Añadir un concepto de **tarea doméstica duradera**: objetivo, quién lo pidió, restricciones, vigencia, plan vigente y resultado. Así «coche preparado a las 8» sobrevive a cambiar de agente o cerrar el chat. No requiere un segundo LLM ni una base vectorial por defecto; requiere un contrato compartido, persistencia y resolución de conflictos.

Los modos de producto deberían ser «observar», «proponer» y «actuar dentro de lo autorizado». El permiso debe indicar dispositivos, acciones, condiciones y caducidad. Una aprobación humana de una propuesta concreta no se convierte implícitamente en autonomía permanente.

### Vivir con el sistema

La vista diaria debe contestar cuatro preguntas: **qué ocurre, qué está previsto, qué requiere atención y qué cambió por decisión mía o del sistema**. Un panel de conexiones no cubre eso.

Ofrecer pausa por tarea/área/dispositivo, control manual prioritario y reanudación explícita o con plazo conocido. Explicar conflictos: una orden de otro habitante, una regla de HA y un plan energético pueden competir por el mismo equipo. Los leases de batería/EV son una base técnica, no una experiencia general de prioridad manual para toda la vivienda.

Los avisos deben llegar por un canal elegido y agruparse por importancia: aprobación pendiente, objetivo que ya no se puede cumplir, aparato sin feedback y resumen de resultados. Eventos internos y mensajes de log no equivalen a notificaciones domésticas.

### Comprobar valor y mantenerlo

Tras una tarea: mostrar completada, parcial, no confirmada o cancelada, y qué se observó. Tras un periodo energético: separar ahorro previsto, estimación posterior y evidencia medida. Si faltan datos, decir «no evaluable», no cero ahorro ni éxito inventado.

La baja también forma parte del producto: revocar agentes, detener futuras tareas, explicar qué automatizaciones quedan en otros sistemas, exportar preferencias y restaurar control local. No prometer una reversión física genérica de acciones ya realizadas.

## 6. Funcionalidades necesarias

El [backlog de producto](backlog.md) define doce entregas con aceptación y dependencias. Las piezas centrales son:

- **Instalación por resultado y perfil de vivienda.** Reducen el trabajo técnico y dan contexto común a todos los agentes.
- **Tareas duraderas y rutinas editables.** Convierten herramientas aisladas en objetivos que se siguen hasta terminar.
- **Centro humano de permisos, pausa y actividad.** Hace comprensible y revocable la autonomía.
- **Contabilidad energética completa.** Integra contador, consumo base, contrato, pérdidas y costes relevantes; diferencia energía de potencia y ahorro de desplazamiento de consumo.
- **Ciclo de observación y replanificación.** Reutiliza el solver con datos actualizados, límites de cambio y prioridad humana; funciona sin consultar un LLM por cada evento.
- **Compatibilidad cualificada.** Clientes probados, dispositivos/perfiles publicados y explicación de capacidades no soportadas.

El ejemplo del usuario debe ser un recorrido de aceptación: coche listo, temperatura deseada y electricidad evitada cuando es cara. El sistema debe detectar primero si los tres objetivos son compatibles, explicar compromisos, pedir la autorización necesaria y comprobar después qué se cumplió.

## 7. Mejoras convenientes y límites de alcance

Después de lo esencial: detección de standby y anomalías, aprendizaje conservador de demanda y parámetros térmicos, recomendaciones de horarios, análisis de tarifas y perfiles nuevos con demanda demostrada. Para agua, añadir medición y alertas donde existan sensores; riego inteligente solo cuando sus actuadores y restricciones estén definidos.

No priorizar ahora otra aplicación de chat, un MCP por protocolo, herramientas por fabricante, un marketplace amplio, administración masiva de viviendas o más complejidad multi-host. Parte de esa infraestructura ya existe: la propuesta es no ampliar su alcance hasta validar los recorridos domésticos. Tampoco replicar un dashboard generalista de HA; integrar o reutilizar lo que la vivienda ya tiene.

Los aparatos programables necesitan semántica propia: una lavadora que no reanuda tras recuperar alimentación no debe presentarse como controlable solo por estar conectada a un enchufe. «Compatible» debe abarcar la tarea, no únicamente la escritura de una propiedad.

## 8. Roadmap recomendado

| Entrega | Resultado visible | Dependencia de salida |
|---|---|---|
| R0 · Base fiable | El piloto no hereda los bloqueos conocidos de actuación | Cierre de los hallazgos técnicos pertinentes |
| R1 · Casa comprendida | Conectar una vivienda y un agente; saber qué puede hacer | Perfil, instalación y cobertura verificadas |
| R2 · Control cotidiano | Pedir, aprobar, seguir y pausar una tarea sin editar JSON | Tareas, permisos, actividad y conflictos |
| R3 · Ahorro evaluable | Ejecutar una mejora energética y explicar su resultado | Datos completos, baseline y medición posterior |
| R4 · Autonomía supervisada | Adaptarse a cambios dentro de límites autorizados | Replanificación, avisos y prioridad manual |
| R5 · Universalidad ampliada | Repetir el recorrido con otros clientes y equipos | Matriz de compatibilidad, recetas y mantenimiento |

Son entregas por resultado, no estimaciones de semanas. La planificación de implementación debe usar Spec Kit por incremento y reutilizar los servicios actuales. Los recorridos de lectura y evaluación pueden avanzar mientras se cierran defectos; la actuación autónoma debe esperar su gate correspondiente.

## 9. Cómo validar el producto

Propuesta de piloto, aún no ejecutado: entre 3 y 5 viviendas, incluyendo una sin solar/batería, una con carga flexible y otra con solar/batería si se dispone de equipos cualificados. Un piloto pequeño identifica fricciones; no permite afirmar ahorro generalizable al mercado.

Separar una fase de observación, otra de propuestas revisadas y una de actuación limitada. Para energía, comparar periodos o replays equivalentes y documentar cambios de ocupación, clima, tarifa y SOC; no atribuir al software el ahorro de instalar hardware ni extrapolar directamente un mes al año.

| Métrica | Definición y uso |
|---|---|
| Tiempo hasta primera utilidad | Desde inicio de instalación hasta lectura útil o actuación confirmada; separar intervención del instalador |
| Cobertura doméstica efectiva | Tareas solicitadas que pueden realizarse con los equipos presentes; no contar protocolos anunciados |
| Tareas completadas | Confirmadas / tareas válidas aceptadas; separar bloqueos, fallos y resultados desconocidos |
| Intervenciones humanas | Correcciones y rescates por tarea/semana; distinguir aprobaciones voluntarias |
| Cumplimiento de preferencias | Plazos, confort y restricciones respetados, incluidos cambios manuales |
| Ahorro estimado neto | Diferencia frente al baseline, descontando costes incrementales y declarando incertidumbre |
| Calidad de datos | Cobertura temporal, frescura, huecos y conciliación con contador |
| Portabilidad del agente | Mismo conjunto de tareas y permisos resuelto por los clientes cualificados |
| Coste operativo | CPU, almacenamiento, llamadas al agente y tiempo de soporte por vivienda |

Antes de cada piloto deben fijarse umbrales apropiados a su instalación. Un criterio obligatorio es cero actuaciones fuera de autorización en los escenarios ejecutados; alcanzarlo en una muestra no prueba ausencia de defectos. No fijar porcentajes de ahorro comerciales sin evidencia.

## 10. Veredicto de producto

**Dirección recomendada: continuar con un runtime universal y cerrar la experiencia de una vivienda real de extremo a extremo.** Las prioridades son comprensión de la casa, control humano, seguimiento de tareas y valor medido. La energía es un caso principal de diferenciación; el control cotidiano debe seguir aportando utilidad a hogares que no pueden optimizar mucho su factura.

La auditoría no acredita encaje de mercado, adopción familiar ni ahorro real. Esas tres hipótesis requieren pilotos. El código permite preparar esos pilotos sin reconstruir la arquitectura.
