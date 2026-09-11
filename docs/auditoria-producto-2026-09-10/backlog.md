# Backlog de producto propuesto

Este documento propone entregas; no declara funcionalidades implementadas ni autoriza despliegues. P1 significa necesaria para el recorrido principal; P2, expansión posterior. Las dependencias identifican capacidades, no obligan a crear nuevos subsistemas si los existentes las resuelven.

## P-01 · P1 · Primera instalación útil

**Usuario:** responsable de la vivienda. **Resultado:** conectar un proveedor y un agente sin recorrer documentación de desarrollo.

Reutilizar catálogo, setup sessions, descubrimiento y preflight. Añadir un recorrido por sistema instalado, diagnóstico accionable y resumen final. No exigir campos de OAuth o mapping que pueda resolver un perfil compatible; mantener alternativa avanzada explícita.

**Aceptación:** una instalación vacía completa conexión, inventario, lectura y vinculación de cliente; una credencial rechazada permite recuperarse sin reiniciar todo; no se presenta simulación como instalación física. Probar móvil, navegación por teclado y lenguaje del primer mercado. **Dependencias:** cobertura P-11 y conexión de cliente P-10 en versión mínima. **Métrica:** tiempo y ayuda necesaria hasta primera utilidad.

## P-02 · P1 · Perfil compartido de vivienda

**Usuario:** habitantes y agentes. **Resultado:** entender habitaciones, nombres, equipos, horarios y restricciones sin reconstruir contexto en cada chat.

Reutilizar registro, áreas, autoridad, políticas y perfiles energéticos. Añadir preferencias domésticas editables, responsables, origen y vigencia. Mantener separados deseo del usuario, dato observado y capacidad del aparato.

**Aceptación:** dos agentes leen el mismo perfil vigente; editar una preferencia produce historial y reevalúa tareas afectadas; «no usar el termo esta noche» caduca cuando corresponde; una asociación ambigua pide resolución. **Dependencias:** P-01. **Métrica:** correcciones de contexto y preguntas repetidas por tarea.

## P-03 · P1 · Tareas domésticas duraderas

**Usuario:** quien pide «coche listo mañana» o «casa preparada al llegar». **Resultado:** objetivo seguido hasta su desenlace, aunque cambie de agente.

Reutilizar planes, programación, auditoría y bundles. Añadir identidad de tarea, solicitante, objetivo, restricciones, estado, relación con versiones de plan y motivos de cambio. Las intenciones físicas y de federación existentes no sustituyen automáticamente este contrato de producto.

**Aceptación:** cerrar el chat no pierde una tarea aceptada; otro cliente autorizado la consulta y modifica; reintentar no duplica actuación; terminar muestra evidencia o falta de confirmación. Un objetivo infactible presenta alternativas, nunca promete éxito. **Dependencias:** P-02, P-04 y gate técnico. **Métrica:** tareas completadas y rescates manuales.

## P-04 · P1 · Consentimiento y autonomía comprensibles

**Usuario:** responsable y demás habitantes autorizados. **Resultado:** comprender qué se delega y retirarlo fácilmente.

Reutilizar assertions, grants, scopes, reglas y políticas. Exponer observar/proponer/actuar, permiso por aparato o área, duración y acciones permitidas. Proporcionar una superficie humana de revisión independiente de la afirmación textual del agente.

**Aceptación:** usuario ve efectos y alcance antes de aprobar; el agente no puede aprobarse; revocar impide futuras actuaciones y tareas aún pendientes; la administración de conexiones requiere permiso propio. Renovar consentimiento recurrente no borra sus restricciones. **Dependencias:** P-02 y correcciones de autoridad/cancelación. **Métrica:** comprensión del permiso en sesiones observadas, revocaciones efectivas y aprobaciones repetitivas evitables.

## P-05 · P1 · Vista diaria y notificaciones

**Usuario:** habitante no técnico. **Resultado:** saber qué pasa y qué necesita hacer sin abrir logs o un chat específico.

Reutilizar estado, histórico, auditoría y resultados de planes. Crear una vista pequeña de ahora, próximas tareas, atención y resultados. Entregar avisos por un canal escogido —por ejemplo, el sistema doméstico ya instalado— con deduplicación, gravedad y enlace a la tarea.

**Aceptación:** una ejecución parcial indica qué equipos cambiaron; una propuesta pendiente permite aprobar o rechazar; avisos repetidos se agrupan; una incidencia resuelta cambia de estado; datos obsoletos no se muestran como actuales. **Dependencias:** P-03/P-04. **Métrica:** incidencias comprendidas sin asistencia y avisos descartados por ruido.

## P-06 · P1 · Contabilidad y ahorro defendible

**Usuario:** hogar que quiere controlar gasto. **Resultado:** entender coste y efecto atribuible a las decisiones.

Reutilizar históricos, contexto energético, solver y comparación. Completar demanda base, contrato de compra/exportación, calidad de datos y baseline. Conservar moneda, pérdidas, SOC inicial/final y costes incrementales; separar gasto variable de componentes que no cambian por mover una carga.

**Aceptación:** replay conocido concilia energía y coste; comparación sin actuación es válida; huecos de datos producen incertidumbre o «no evaluable»; cambiar tarifa no se registra como ahorro automático del algoritmo; un hogar sin flexibilidad no recibe promesas ficticias. **Dependencias:** P-02 y correcciones energéticas técnicas. **Métrica:** conciliación con contador/contrato y ahorro estimado neto con cobertura declarada.

## P-07 · P1 · Optimización adaptativa con límites

**Usuario:** hogar que ya aceptó un objetivo energético. **Resultado:** mantenerlo ante cambios de consumo, solar y disponibilidad.

Reutilizar solver, frescura, scheduler, control supervisor y executor. Añadir horizonte móvil, disparadores por desviación, umbral de beneficio, mínimo de permanencia, presupuesto de cómputo y continuidad de consentimiento. Ejecutar el control rutinario localmente.

**Aceptación:** pérdida de previsión, cambio de salida del coche y aumento de demanda producen revisión explicada; no hay oscilaciones de órdenes; no se relajan límites para obtener ahorro; sin Internet se mantiene una política local explícita; una propuesta nueva no hereda permisos incompatibles. **Dependencias:** P-03/P-04/P-06/P-08. **Métrica:** objetivos respetados, error de previsión, cambios por hora y coste del cálculo.

## P-08 · P1 · Prioridad manual y conflictos

**Usuario:** cualquier habitante autorizado. **Resultado:** el sistema no contradice silenciosamente una intervención humana.

Reutilizar scopes, cola, ownership y coordinadores batería/EV. Generalizar la política de prioridad a tareas domésticas y coexistencia con automatizaciones externas. Mostrar motivo, alcance y duración de una pausa. No prometer detectar acciones manuales que el proveedor no pueda distinguir.

**Aceptación:** dos agentes con objetivos incompatibles reciben una resolución común; cambio manual conocido suspende o replantea la tarea según política; la reanudación es visible; interacción con una automatización de HA no genera una lucha de consignas. **Dependencias:** P-02/P-03 y gate técnico. **Métrica:** órdenes contradictorias, reversiones no deseadas e intervenciones para recuperar control.

## P-09 · P1 · Rutinas por intención

**Usuario:** habitante. **Resultado:** configurar «noche», «vacaciones», «carga del coche» o «confort al despertar» mediante pocas decisiones.

Reutilizar las nueve skills, escenas, reglas y recurrencias. Añadir plantillas parametrizadas, comprobación de requisitos, vista previa y edición; enlazar cada rutina con tareas observables. No crear una skill por marca.

**Aceptación:** guardar una rutina y ejecutarla desde otro agente conserva límites; detecta aparatos ausentes; vacaciones incluyen fechas y excepciones; la UI permite pausar o cambiar una sola parte sin reconstruir JSON. **Dependencias:** P-02/P-03/P-04/P-08. **Métrica:** rutinas configuradas sin ayuda y uso posterior con pocas correcciones.

## P-10 · P1 · Conectar clientes y evaluar agentes

**Usuario:** quien cambia de aplicación de IA. **Resultado:** conservar vivienda, permisos y tareas.

Reutilizar gateway, probe y pruebas de protocolo. Publicar recetas de conexión versionadas y matriz de transporte/autenticación; incorporar autorización estándar donde la requieran los clientes elegidos. Evaluar lenguaje natural además del protocolo.

**Aceptación:** cada cliente anunciado completa lectura, propuesta, aprobación humana, ejecución permitida y revocación en un entorno cualificado; otro cliente continúa la misma tarea. En fixtures, probar peticiones ambiguas, falta de datos, instrucciones hostiles en nombres de entidades e intentos de ampliar permisos. Nunca incluir secretos en ejemplos. **Dependencias:** la conexión y lectura mínimas reutilizan el gateway/probe y no dependen de P-01/P-03; P-03/P-04 son necesarios para el recorrido completo de tareas y aprobación. **Métrica:** éxito por cliente/versión y resultado de cada escenario, sin extrapolar a «cualquier agente».

## P-11 · P1/P2 · Cobertura y expansión cualificadas

**Usuario:** instalador o propietario. **Resultado:** saber antes de activar una función si su aparato puede realizarla.

Reutilizar capabilities, coverage, commissioning, recetas y conformance. Publicar operaciones por equipo/perfil/firmware, con lectura, actuación, feedback y nivel de evidencia. P1 es describir honestamente lo existente; P2 es ampliar cobertura.

**Aceptación:** una ruta ausente explica el requisito pendiente; dos conexiones al mismo equipo no se presentan como dos actuadores independientes; importar una receta no eleva cualificación. Priorizar capacidades por tareas demandadas: iluminación completa, enchufes medidos, persianas, HVAC y cargas realmente programables. **Dependencias:** la cobertura mínima reutiliza registro y catálogo, sin depender de P-01; P-01 integra después su presentación en el instalador. La actuación exige el gate físico correspondiente a cada perfil. **Métrica:** tareas cubiertas y tiempo de cualificación de una nueva instalación.

## P-12 · P1/P2 · Operación, privacidad y entrega de vivienda

**Usuario:** responsable o instalador. **Resultado:** actualizar, recuperar y retirar el servicio conservando control.

Reutilizar preflight, backups, restauración, exportación/borrado y métricas. Preparar procedimientos de actualización reversible, comprobación de salud, exportación de diagnóstico sin secretos y traspaso/revocación de accesos. P1 es el procedimiento para el piloto; P2, su simplificación para terceros.

**Aceptación:** restaurar en una instalación desechable conserva preferencias y tareas, sin repetir efectos antiguos; retirar un cliente revoca su acceso; desinstalar explica qué queda en HA u otros controladores; políticas de datos son visibles y exportables. **Dependencias:** capacidades previas y gates de recuperación. **Métrica:** éxito de restauración, tiempo de mantenimiento y horas de soporte.

## Orden y criterio de cierre

R1 combina P-01/P-02 con cobertura y conexión básicas de P-10/P-11. R2 completa P-03/P-04/P-05/P-08/P-09. R3 entrega P-06 y el procedimiento mínimo de P-12. R4 incorpora P-07. R5 amplía clientes, perfiles y operación de terceros.

La conexión MCP mínima y la matriz de cobertura pueden existir antes de completar tareas compartidas; sus recorridos completos se cierran después. Esto evita un ciclo artificial de dependencias entre onboarding y tareas.

La entrega se acepta por el recorrido observado y sus contratos, no por cantidad de endpoints, archivos o checks de un documento. Cada incremento necesita su propia especificación y pruebas de composición cuando cambie comportamiento; este backlog no sustituye esos artefactos.
