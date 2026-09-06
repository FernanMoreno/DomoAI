# Fase 05 — Adaptación a la vivienda, identidad y commissioning

[Índice](README.md) · [Cobertura](fase-04-cobertura-semantica-conectores.md) · [Energía](fase-06-energia-optimizacion-explicable.md)

Estado: U-08 es un límite de diseño observado. Entrada: cobertura de fase 04 y contrato de autoridad de fase 01. No hay qualification física nueva en este documento.

## 1. Objetivo

Que DomoAI pueda incorporar una vivienda heterogénea y explicar qué ha entendido, qué necesita configurar y qué aún no puede operar con garantías. La adaptación debe continuar cuando cambia el inventario, el firmware o la función física de un relé.

Descubrir, asociar identidad, asignar área, declarar capacidades, verificar commissioning y autorizar ejecución son pasos diferentes. Una instalación automática puede preparar propuestas; no debe convertir incertidumbre en permiso.

## 2. Baseline técnica

`runtime/registry.py` conserva IDs y referencias de fuente; al restaurar inventario reconstruye rutas sólo tras evidencia viva. `discovery_service.py` normaliza snapshots. `runtime_bootstrap.py` prepara configuración bajo perfiles definidos. `application/commissioning.py` y repositorios de qualification mantienen informes/evidencias. Los modelos de garantías describen requisitos de readback, tolerancia y commissioning.

Los nombres de ejemplo como `living_room.main_light` no demuestran asociación automática universal a habitaciones. Fuentes sin área pueden aparecer como unassigned. KNX/Modbus/MQTT requieren mappings y equipos energéticos requieren perfiles/bindings.

## 3. U-08: función física de una salida

El clasificador considera seguros por defecto ciertos comandos de switch. Un switch puede representar una lámpara, una resistencia, una bomba o una puerta. El nombre del protocolo y el tipo genérico no bastan para distinguir esas consecuencias.

No se demostró un bypass físico. La propiedad que falta para una promesa universal es una caracterización fiable de la función instalada y de sus restricciones. El commissioning debe comunicar esa necesidad y permitir endurecer la política.

No resolverlo marcando todo como seguro porque el proveedor permite escribir ni suponiendo que un nombre como «luz» es una garantía de hardware.

## 4. Ciclo propuesto de incorporación

1. Detectar fuentes configuradas y su salud.
2. Descubrir candidatos sin conceder escritura adicional.
3. Identificar fuentes estables y conflictos con inventario persistido.
4. Mapear capacidades conocidas y reportar desconocidas.
5. Proponer áreas/funciones cuando exista evidencia; separar propuestas de asignaciones confirmadas.
6. Exigir mappings, perfiles o intervención de operador donde falte significado.
7. Verificar feedback, tolerancias y límites del perfil autorizado.
8. Activar la ruta conforme a policy y qualification; no mediante el texto de un informe.
9. Vigilar cambios invalidantes y retirar la evidencia afectada cuando corresponda.

Este ciclo es un criterio de producto; no afirma que todos sus pasos estén automatizados hoy.

## 5. Identidad y cambios

| Cambio | Invariante exigido |
|---|---|
| Renombrar etiqueta visible | Conservar identidad si el ancla de fuente sigue siendo la misma. |
| Cambiar entity ID con ID físico estable | Reconciliar explícitamente y preservar trazabilidad. |
| Reemplazar dispositivo manteniendo nombre | No heredar automáticamente qualification ni autoridad del antiguo. |
| Equipo visto por HA y conector nativo | Fusionar sólo con evidencia suficiente; resolver rutas de forma explícita. |
| Dos equipos con nombre igual | No fusionar por aproximación textual. |
| Fuente desaparece temporalmente | Conservar inventario útil, marcar disponibilidad/frescura correctamente. |
| Capability deja de existir | Invalidar planes/bindings dependientes según revisiones. |
| Equipo cambia de habitación/función | Reevaluar reglas por área, riesgo y consentimiento aplicable. |

Las decisiones de reconciliación deben dejar diagnóstico sanitizado. Un operador necesita saber por qué una entidad se bloqueó y qué dato resolvería el conflicto.

## 6. Evidencia de commissioning

Un registro útil identifica dispositivo físico, fabricante/modelo si existen, firmware, conector y mapping, capability, ruta de comando, ruta de feedback, unidad, rango, tolerancia, latencia y fuente de la prueba. También identifica responsable, instante y condiciones de validez.

La evidencia observacional no equivale a aprobación. Comprobar que `inspect_commissioning` y `verify_commissioning` no crean bindings ejecutables, leases ni grants como efecto secundario inesperado.

Para batería/EV/HVAC, incluir capacidad, signo de potencia, SOC, reservas, límites y condiciones de conexión. Cambiar un perfil energético invalida supuestos del optimizador aunque el ID semántico permanezca estable.

## 7. Experiencia del agente y del operador

El diagnóstico debería responder: qué dispositivo/capability está afectado, si es legible o escribible, qué dato falta, qué acción administrativa puede resolverlo y qué acciones siguen disponibles.

Evitar exponer credenciales, registros MQTT completos o payloads crudos como explicación. No inventar una API universal de commissioning de radios: Matter, KNX y Zigbee pueden requerir herramientas externas al runtime.

Para una casa parcialmente configurada, el sistema debería seguir ofreciendo discovery y lecturas válidas de las fuentes sanas. La ausencia de batería o EV no debe convertir toda la vivienda en un perfil energético incompleto por defecto.

## 8. Tareas propuestas

### F05-T01 — Informe de incorporación

- [ ] Enumerar fuentes, candidatos, unsupported, conflictos y bindings faltantes.
- [ ] Distinguir sugerencias de áreas/funciones de asignaciones autorizadas.
- [ ] Publicar diagnósticos por capability con acciones administrativas concretas.
- [ ] Verificar que el informe no modifica la autoridad física.

### F05-T02 — Identidad a través de cambios

- [ ] Probar renombre, sustitución, doble fuente y eliminación/reaparición.
- [ ] Verificar persistencia y reconstrucción después de reinicio.
- [ ] Invalidar dependencias cuando cambian rutas o garantías.
- [ ] Evitar que los fallbacks de nombre fusionen identidades distintas.

### F05-T03 — Riesgo por instalación

- [ ] Definir qué información de función instalada requiere revisión humana.
- [ ] Aplicar el riesgo resultante a policy sin aceptar etiquetas del cliente como autoridad.
- [ ] Probar un relé con función sensible y otro de iluminación ordinaria.
- [ ] Documentar límites de detección automática y requisitos de puesta en servicio.

### F05-T04 — Caducidad de evidencia

- [ ] Identificar cambios de firmware, mapping, equipo o perfil que exigen revisión.
- [ ] Bloquear sólo las rutas afectadas cuando sea posible.
- [ ] Conservar diagnóstico y evidencia anterior como histórica, no vigente.
- [ ] Revalidar planes energéticos y automatizaciones dependientes.

## 9. Pruebas de partida

```bash
uv run --frozen pytest tests/unit/runtime/test_registry_reconciliation.py tests/unit/runtime/test_registry_persistence.py
uv run --frozen pytest tests/contract/test_commissioning_contract.py tests/integration/test_hardware_commissioning_runtime.py tests/composition/test_phase4_commissioning_composition.py
```

El nombre `hardware_commissioning` de un archivo de tests no acredita por sí mismo equipo físico. Registrar qué dependencia usa cada escenario y qué resultado demuestra.

## 10. Cierre

- [ ] Un equipo desconocido permanece identificado como desconocido sin escritura inventada.
- [ ] Renombre no cambia identidad arbitrariamente; sustitución no hereda qualification.
- [ ] Ambigüedades de identidad/ruta se bloquean y explican.
- [ ] Función física y riesgo no dependen exclusivamente del nombre visible.
- [ ] Commissioning no crea una segunda autoridad.
- [ ] Cambios invalidantes se propagan a planes, bindings y cobertura.

Salida: perfil de vivienda verificable que pueden usar energía e instalación. Riesgo residual: commissioning físico y protocolos propietarios pueden exigir intervención externa y no deben ocultarse como «autoadaptación completa».
