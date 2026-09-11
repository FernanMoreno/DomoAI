# Auditoría integral de DomoAI

Fecha: 2026-09-10. Base Git: `b9f5094fc1a28b0f209ca7f3d4f332f8d8a789f3`, **más los cambios locales preexistentes**. El commit por sí solo no reproduce los resultados.

## Dictamen

**FAIL para operación autónoma integral y para la promesa de universalidad sin reservas.** DomoAI tiene una arquitectura sustancial y aprovechable, pero esta revisión identifica doce hallazgos reproducibles, incluida una brecha de composición energética. No es necesario reconstruirlo siguiendo el esquema inicial ni volver a separar dos MCP públicos.

La prioridad es cerrar los fallos de ejecución, administración y datos, y después completar un ciclo de ahorro medido. Añadir más protocolos antes de resolver esas fronteras multiplicaría las combinaciones que deben verificarse.

El informe anterior del 9 de septiembre declara cerrados sus 22 hallazgos. Eso puede ser cierto para aquellos casos; **no implica que el sistema carezca de otros defectos**. Los casos siguientes se comprobaron sobre el código actual, sin asumir vigentes los defectos de auditorías anteriores.

## Alcance y evidencia

Inventario: 245 archivos Python de producción, 459 archivos de pruebas y 224 especificaciones. Se revisaron las fronteras de MCP, Management, autoridad, dispositivos, conectores, estado, automatizaciones, scheduler, executor, persistencia, recuperación, optimización, despliegue y CI. Es una auditoría transversal basada en riesgo, no una inspección manual de cada línea ni una certificación física.

Método: instrucciones del proyecto, baseline Spec Kit 196 y requisitos relacionados, Graphify —10.692 nodos y 30.954 relaciones— contrastado con fuente, revisiones independientes de ejecución/energía/superficie, reproducciones aisladas y verificaciones ejecutables. El grafo orienta la navegación; no acredita que las líneas sigan siendo actuales. No se creó una especificación nueva de implementación porque el encargo es auditar, no cambiar comportamiento.

Se preservaron los cambios previos. Solo se añaden este informe y sus evidencias. Las reproducciones usan objetos en memoria, SQLite temporal y adaptadores simulados. Los controles generales pueden utilizar dependencias Docker desechables. No se acreditó actuación sobre hardware doméstico real ni ahorro económico de una vivienda.

Las reproducciones están en `reproducciones/`. Son demostraciones del comportamiento observado, no pruebas de regresión incorporadas al proyecto; algunas imprimen el fallo y terminan con código 0. Su código de salida no significa que DomoAI sea correcto.

## Qué existe realmente

| Objetivo | Estado observado | Límite |
|---|---|---|
| Una interfaz para agentes | MCP unificado, stdio y gateway HTTP, contexto compartido | Compatibilidad condicionada al transporte y autenticación del cliente |
| Runtime independiente del LLM | Registro, estado, eventos, políticas, executor y scheduler | Los fallos A-01/A-02/A-04 afectan su autonomía fiable |
| Modelo universal | Capacidades, unidades, límites, rutas, identidad y garantías | El catálogo de tipos y los mappers siguen siendo finitos |
| Home Assistant como gran adaptador | Descubrimiento, normalización, comandos y eventos | A-06 puede abortar el snapshot completo |
| Conectores ampliables | HA, Matter, Zigbee2MQTT, MQTT, KNX, Modbus, REST y recetas/SDK | Configuración o receta no equivale a cualificación física |
| Seguridad de actuación | Autoridad, consentimiento, frescura, admisión, idempotencia, readback y auditoría | Existen defectos entre capas, no basta con cada control aislado |
| Optimización | CP-SAT, DSL tipada, restricciones duras y objetivos jerárquicos | Costes y métricas dependen de la integridad del contexto |
| Energía avanzada | Solar, importación/exportación, batería con eficiencias/degradación/SOC final, EV y HVAC | Faltan composición de consumo base y verificación del ahorro real |
| Procedimientos para agentes | Nueve skills portables y flujos de propuesta/validación/commit | Una skill no sustituye autorizaciones ni supervisión del runtime |
| Operaciones | Persistencia SQLite/PostgreSQL, backup, privacidad, observabilidad, laboratorio y HIL | Hay que demostrar recuperación y cualificación en cada despliegue |

Referencias principales: `mcp/unified_server.py`, `application/runtime_factory.py`, `application/executor.py`, `runtime/ports.py`, `domain/models.py`, `optimizer/cp_sat.py`, `optimizer/scenario.py`, `persistence/backup.py`, `skills/core/`. Las rutas de código anteriores son relativas a `src/domoai`, excepto `skills/core/`.

La separación correcta ya existe internamente: el optimizador propone, el runtime decide si puede ejecutar. Mantener un MCP público reduce divergencia de registros, aprobaciones y autoridad. Separar procesos solo tendría sentido ante una necesidad demostrada de aislamiento o escala.

## Hallazgos y correcciones completas

P1: bloquea una propiedad central de operación o seguridad. P2: defecto funcional, privacidad o evidencia importante. No se afirma un exploit físico P0 ni ejecución real entre viviendas.

### A-01 · P1 · Una regla horaria dispara otras reglas de otra hora

**Reproducido.** A las 12:00, con reglas para 12:00 y 18:00, se entregan ambos planes al ejecutor.

Origen: `application/local_automation.py:181` envía el evento de una regla a `handle_event()`, que recorre todas. `_matches_trigger()` en `:327` acepta el tipo temporal sin comprobar destinatario y horario.

Impacto: calefacción, riego, persianas o cargas pueden activarse fuera de su programación. Varias reglas simultáneas también requieren comprobar que no se multipliquen ejecuciones.

Corrección: enrutamiento explícito por regla, comprobación de hora/zona y clave idempotente por ocurrencia. Cierre: reglas con horas distintas, misma hora, distintas zonas, cambio de horario estacional y evento duplicado; solo las ocurrencias debidas deben ejecutarse una vez.

### A-02 · P1 · Una ejecución cancelada en cola todavía actúa después

**Reproducido con PlanExecutor, SQLite y adaptador simulado.** Al cancelar: `plan=unknown, calls=0`; al liberar la cola: `plan=unknown, calls=1`.

Origen: `application/executor.py:560` espera la cola; la cancelación termina el flujo de seguimiento, pero `application/household_queue.py:99` invoca el trabajo aunque su Future ya esté cancelado.

Impacto: una acción física simulada ocurre después de que el flujo solicitante haya terminado, sin actualizar correctamente el resultado del plan. No significa que cancelar pueda deshacer una orden ya enviada: el defecto demostrado ocurre **antes de iniciar el despacho**.

Corrección: diferenciar pendiente, despachando y enviado; retirar el trabajo cancelado antes del envío. Revalidar autoridad, expiración y seguridad tras la espera. Cierre: cancelación antes/durante/después del envío, apagado del runtime, timeout y retry; ninguna escritura posterior para un trabajo retirado antes del despacho y resultado fiel cuando el envío ya comenzó.

### A-03 · P2 · ACK impide persistir el resultado final de la intención

**Reproducido.** `DISPATCHING`, seguido de `ACKNOWLEDGED` y `CONFIRMED`, queda en `ACKNOWLEDGED`; recuperación con epoch superior lo convierte en `UNKNOWN`.

Origen: `persistence/coordination.py:131` y `:142` solo permiten actualizar desde `DISPATCHING`; el executor guarda ACK en `application/executor.py:568` y posteriormente intenta cerrar el readback en `:781`.

Impacto: incoherencia entre ejecución, ledger y recuperación; un resultado confirmado pierde su condición terminal.

Corrección: tabla explícita de transiciones, con CAS por propietario, intento y epoch, permitiendo terminar desde ACK. Cierre: confirmación, rechazo y timeout de readback; recuperación no degrada terminales. Verificar SQLite y PostgreSQL; la reproducción adjunta prueba SQLite.

### A-04 · P1 · Readiness devuelve 200 con scheduler detenido

**Reproducido con runtime real sobre fixture.** Antes: 200. Después de cancelar la tarea `domoai-scheduler`: `scheduler_done=true`, tres tareas restantes, HTTP 200 y `status=ready`.

Origen: `mcp/health.py:113` solo exige al menos dos tareas vivas; `application/runtime_lifecycle.py:61` crea tareas sin supervisión de reinicio en esta clase.

Impacto: supervisión externa puede creer que las programaciones siguen funcionando. `/healthz` es deliberadamente liveness; este hallazgo corresponde a `/readyz`.

Corrección: verificar identidad y estado de cada tarea obligatoria, registrar su excepción y definir recuperación o cierre seguro. Cierre: matar individualmente scheduler, consumidor, refresher y tareas obligatorias configuradas; readiness debe responder con causa específica mientras no se restablezca el servicio.

### A-05 · P1 · Permiso de dispositivo se convierte en administración global

**Reproducido en ASGI con manager de registro.** Un token `mutate`, rol `operator`, limitado a `light.allowed` y área `allowed`, puede enviar `POST /api/v1/connections/<UUID>/disable`; obtiene 200 y se invoca `manager.disable`.

Origen: `management/api.py:124` autoriza `execute` sin dispositivo/área objetivo; `:308` despacha la conexión. `management/auth.py:103` y `:124` equiparan aliases de mutación con administración.

Impacto: un permiso destinado a ejecutar órdenes acotadas permite alterar conectividad global. Los tests actuales admiten `mutate` global deliberadamente; falta distinguir ese caso del usuario con ACL restringida. No se demuestra eludir el executor ni una aprobación física.

Corrección: permiso administrativo explícito, separado de actuación; comprobar pertenencia y alcance de la conexión en cada operación. Cierre: matriz de lectura, dispositivo, área, propietario y administración para create/update/disable/delete/repair/credenciales; token acotado no administra conexiones globales.

### A-06 · P1 · Un brillo nulo aborta el snapshot de Home Assistant

**Reproducido.** Un switch válido y una luz apagada con `brightness: null` provocan `TypeError` en `_load_snapshot()`.

Origen: `adapters/home_assistant/provider.py:1283` evalúa `float(None)`; la normalización global en `:326` no aísla el fallo de la entidad.

Impacto: una entidad con valor ausente impide descubrir o refrescar el resto del conjunto. No hace falta un protocolo nuevo para afectar la instalación principal.

Corrección: distinguir ausente, nulo, desconocido, no disponible y cero; aislar entradas malformadas con diagnóstico por entidad. Cierre: lote mixto con entidades válidas/nulas/inválidas, snapshot y eventos; preservar las válidas y nunca fabricar un valor numérico para las otras.

### A-07 · P2 · El recurso de inventario revela áreas no autorizadas

**Reproducido.** Con `devices=[]`, `inventory_snapshot()` devuelve también el área `private / Private room`.

Origen: `mcp/domotics_server.py:1723` filtra dispositivos, pero `mcp/resources.py:29` serializa todas las áreas del registro.

Corrección: aplicar el alcance a áreas, jerarquía y referencias de origen; revisar recursos hermanos. Cierre: token sin visibilidad, token de área y token de dispositivo; ninguna metadata fuera de la política. La reproducción comprueba la proyección real; no se afirma filtración de estados o credenciales.

### A-08 · P2 · Coverage anuncia activa una ruta no disponible

**Reproducido.** Ruta con `available=False` y proveedor MQTT activo produce `status=active`.

Origen: `mcp/resources.py:145` comprueba actividad del proveedor, no disponibilidad de la ruta. `runtime_snapshot()` sí calcula disponibilidad por ruta.

Corrección: separar soporte semántico, disponibilidad efectiva, permiso y cualificación. Los campos `read/write` pueden representar soporte y no son por sí solos el fallo. Cierre: dispositivo desconectado con proveedor activo, varias fuentes, estado rehidratado y reconexión; los recursos deben ser coherentes.

### A-09 · P1 · El contexto energético compuesto omite consumo no controlable

**Brecha reproducida.** Contexto compuesto de dos horas, tarifa 0,20 y solar cero: coste calculado 0. Incorporando una previsión base de 1 kW, el mismo modelo calcula 0,40.

Origen: `optimizer/providers.py:476` y `:569` no componen `base_load_forecast`; `optimizer/cp_sat.py:158` sustituye su ausencia por cero. El solver sí admite esa previsión si se suministra por otra vía.

Impacto: la configuración automática puede subestimar coste e importación doméstica. Una restricción satisfecha en el modelo incompleto no acredita el límite de la vivienda. No se demuestra que el validador de seguridad física permita sobrepasarlo.

Corrección: proveedor de demanda base con procedencia, frescura e incertidumbre; descontar cargas controlables para evitar doble conteo. Si falta información, declararlo y usar una reserva conservadora o bloquear promesas de ahorro/límite. Cierre: carga doméstica más EV/HVAC/batería, saltos de demanda y dato ausente.

### A-10 · P2 · La comparación descarta un baseline válido sin actuación

**Reproducido con CP-SAT.** Baseline pasivo con coste 0,40 y `no_action_required`; una alternativa solicitada termina en `variations={}`.

Origen: `optimizer/counterfactual.py:17`, `:68` y `:89` excluyen `NO_ACTION_REQUIRED` de los resultados válidos.

Corrección: separar validez del resultado y existencia de plan ejecutable. Cierre: baseline pasivo, alternativa pasiva, infactible y timeout, tanto en API síncrona como asíncrona; mantener diferencias económicas válidas y diagnósticos cuando no se pueda comparar.

### A-11 · P2 · Exportar batería reduce indebidamente el autoconsumo solar

**Reproducido con CP-SAT.** Primer tramo: solar 1 kWh consumida íntegramente por carga base. Segundo: sin solar, exportación de 1 kWh desde batería precargada. Resultado: autoconsumo solar 0, aunque el primer tramo consumió 1 kWh solar.

Origen: `optimizer/cp_sat.py:565` resta toda la exportación de la producción solar sin distinguir origen.

Corrección: contabilidad explícita de flujos y procedencia, incluida energía inicial de batería. Cierre: exportación solar, exportación de batería precargada, carga de batería desde red/solar y horizonte sin producción. La métrica no debe atribuir a solar energía de otro origen.

### A-12 · P2 · Los costes suman monedas distintas

**Reproducido con CP-SAT.** Dos tramos de 1 kWh, uno a 0,20 EUR y otro a 0,20 USD, generan `energy_cost=0.4`, sin rechazo ni conversión.

Origen: `optimizer/energy.py:130` valida series, pero no homogeneidad monetaria; `optimizer/cp_sat.py:513` opera con los números.

Corrección: una moneda canónica por contexto, incluyendo compra, exportación y degradación; conversión explícita y fechada si se admite. Cierre: monedas mezcladas entre tramos/proveedores, monedas homogéneas y presentación del resultado con unidad monetaria.

## Funciones necesarias para cumplir el producto

### 1. Ahorro real, no solo coste de una solución

Completar la cadena contador/telemetría, consumo base, precio de contrato, plan, ejecución, consumo observado y comparación con baseline. Publicar por separado ahorro previsto, estimación posterior y ahorro verificado, indicando datos faltantes e incertidumbre.

OMIE aporta mercado mayorista; el contrato del hogar puede añadir componentes o una estructura diferente. La CNMC distingue facturación por energía, peajes y cargos, y contempla precio fijo: [comercialización y suministro](https://www.cnmc.es/en/node/406175). Optimizar OMIE sin representar el contrato no prueba reducción de factura. También deben contabilizarse coste del servicio/agente y degradación para hablar de ahorro neto.

Aceptación propuesta: replay con contador y contrato conocidos, informe por dispositivo y vivienda, reconciliación de energía, ausencia de doble conteo y ninguna etiqueta «verificado» cuando faltan mediciones o baseline defendible. No comprometer un porcentaje de ahorro antes del piloto.

### 2. Optimización iterativa supervisada

Ya existen scheduler, validaciones y reparación de conexiones. Falta demostrar un controlador energético de horizonte móvil que observe desviaciones, actualice previsiones y proponga sustituciones de plan conservando consentimiento e idempotencia.

OMIE v1 exige un día local completo de 15 minutos (`optimizer/omie.py:254`); para un horizonte móvil debe desacoplarse la descarga diaria de la extracción del tramo solicitado. Prever cambios DST, cruces de fecha y falta de precios de mañana.

Incluir umbral de beneficio, histéresis, mínimo de permanencia, límite de replanificaciones y prioridad del control manual. Aceptación: caída de solar, llegada tardía del EV, cambios de tarifa, corte de red y override humano; el runtime debe seguir seguro sin LLM ni Internet. El ajuste de modelos debe cambiar previsiones, nunca relajar automáticamente políticas de seguridad.

### 3. Universalidad comprobable por capacidad

El catálogo `domain/models.py:134` contiene light, switch, cover, climate, sensor, energy, ev_charger y unsupported. Tener un transporte Matter/KNX/Modbus no cubre todas sus funciones. Las recetas amplían posibilidades, pero necesitan perfiles, semántica y evidencia.

Extender según demanda real: iluminación con color/temperatura, ventilación, enchufes medidos, persianas, modos HVAC, electrodomésticos programables; después cerraduras, alarmas, aspiradores y multimedia cuando sus contratos y autorizaciones estén definidos. Mantener comandos semánticos, sin tool por fabricante.

Cada capacidad nueva debe incluir lectura, escritura si aplica, unidades, rangos, estado desconocido, frescura, feedback, error, cancelación y criterio de cualificación. El éxito debe medirse por combinaciones dispositivo/firmware/perfil probadas, no por número de protocolos listados.

### 4. Puesta en marcha y diagnóstico utilizables

Ampliar el control plane existente con un recorrido único: conectar proveedor, descubrir, resolver duplicados/áreas, identificar cargas medibles, introducir contrato y restricciones, probar lectura, verificar actuación consentida y activar autonomía limitada.

La interfaz debe explicar «qué puedo controlar», «qué falta» y «por qué está bloqueado» sin exigir entender el SDK. Cierre: una instalación nueva termina con inventario y evidencia exportables; una entidad desconocida no paraliza el resto y nunca se convierte automáticamente en actuador.

### 5. Compatibilidad real entre clientes y permisos graduados

El gateway entrante usa bearer estático; la documentación local lo reconoce. Probar clientes mediante negociación, discovery, recursos, errores, expiración/revocación, streaming y cancelación. No basta con que un cliente anuncie compatibilidad MCP.

Si se quieren clientes que exigen autorización OAuth, integrar un servidor de autorización y metadata de recurso protegido, o una capa externa explícitamente soportada. La especificación detalla ese descubrimiento: [MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization). Esta carencia es una limitación de interoperabilidad, no ausencia total de autenticación.

Separar lectura, propuesta, ejecución acotada, programación y administración. Usar permisos por capacidad/área y ventanas de consentimiento comprensibles, con revocación efectiva incluso tras esperar en cola.

### 6. Confiabilidad operativa demostrada

Aprovechar backup, restauración, privacidad y métricas existentes. Añadir alertas accionables sobre tarea detenida, dato obsoleto, plan no ejecutado, divergencia de readback y ahorro no verificable. Probar restauración de vivienda completa, pérdida de proceso/energía y reconciliación sin repetir actuadores.

La CI tiene gates bloqueantes de lint, tipos, contratos, arquitectura, dependencias y un smoke Docker obligatorio. Sin embargo, `scripts/ci/run_mandatory_docker_composition.sh` prueba un broker, valida el YAML y la configuración de Caddy; eso no equivale a arrancar y recorrer todo el stack de producción. Añadir un recorrido desechable completo con gateway autenticado, proveedor, persistencia, programación y reinicio. Mantener aparte una matriz HIL real con firmware/perfiles identificados.

## Mejoras convenientes después de lo esencial

- Calibración térmica a partir de observaciones, con validación fuera de muestra y límites conservadores; no confundir parámetros declarados con modelo aprendido.
- Detección de consumo anómalo, equipos encendidos fuera de horario, standby y fugas, con recomendaciones explicables antes de automatizar cortes.
- Gestión de llegada/salida del EV, prioridades de varios habitantes y presupuestos por aparato o periodo.
- Escenarios comparativos de tarifa, batería o paneles, usando costes completos e incertidumbre; solo asesoramiento cuando la evidencia no permita decidir automáticamente.
- Catálogo de recetas versionado y cualificado por firmware, con actualización reversible y diagnóstico de incompatibilidades.
- Métricas de calidad del producto: instalación completada, actuación confirmada, falsos bloqueos, horas sin intervención, desviación de previsión y ahorro neto verificable.

## Orden de ejecución recomendado

| Tramo | Entrega | Criterio para cerrarlo |
|---|---|---|
| 1. Seguridad y coherencia | A-01 a A-08 | Repros convertidas en regresiones, contratos y composición aprobados |
| 2. Datos y contabilidad | A-09 a A-12, demanda base, precio de contrato | Balance energético y monetario consistente; baseline pasivo comparable |
| 3. Piloto de ahorro | Medición posterior, horizonte móvil, overrides | Periodo representativo con errores e incertidumbre reportados y sin efectos no autorizados |
| 4. Instalación y clientes | Onboarding, permisos, matriz MCP | Vivienda nueva y varios clientes completan el mismo recorrido verificable |
| 5. Expansión | Nuevas capacidades y recetas | Demanda real, pruebas de contrato y cualificación por perfil |

Aplicar Spec Kit por entregas con criterios de aceptación. Evitar otra expansión transversal antes de cerrar las reproducciones. Los archivos grandes —runtime factory 1.896 líneas, executor 1.422, CP-SAT 1.335— justifican extracciones posteriores por responsabilidad, precedidas por regresiones; su tamaño es un riesgo de mantenimiento, no prueba de un defecto.

## Verificación y revisión de composición

Resultados finales y limitaciones: [verificacion.md](verificacion.md).

Fronteras revisadas: identidad/Management/manager; ACL/recursos; payload HA/snapshot; evento/regla/plan; cancelación/cola/adapter; executor/ledger/readback/recuperación; lifecycle/readiness; proveedor/contexto/solver/comparación; configuración/CI/despliegue.

Invariantes: pertenencia y alcance, una actuación por ocurrencia, ausencia de despacho tras retirada previa al envío, resultados terminales durables, disponibilidad fiel, datos parciales aislados, unidades y monedas coherentes, energía conservada y separación entre soporte, autorización y evidencia física.

No se modificaron subsistemas de ejecución. La auditoría no marca corregidos los defectos ni sustituye pruebas físicas. **Dictamen final: FAIL para la aceptación integral solicitada, con arquitectura válida para continuar y prioridades concretas de cierre.**
