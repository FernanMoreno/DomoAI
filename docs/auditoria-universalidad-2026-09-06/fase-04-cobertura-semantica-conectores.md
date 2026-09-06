# Fase 04 — Cobertura semántica y conectores universales

[Índice](README.md) · [Privacidad](fase-03-privacidad-ciclo-datos.md) · [Adaptación](fase-05-adaptacion-identidad-commissioning.md)

Estado: limitación U-07 demostrada por inspección de mappers. Entrada: baseline. No se presume autorización para implementar todas las familias mencionadas.

## 1. Objetivo

Convertir «soporta el protocolo» en una matriz comprobable de dispositivos, capacidades y operaciones. El resultado público debe permitir al agente saber qué puede leer/proponer/ejecutar, qué garantías tiene la ruta y qué falta.

No se busca un MCP por fabricante. Toda ampliación debe traducirse internamente al mismo modelo y pasar por policy/admission/executor. La falta de una capability no se corrige permitiendo comandos arbitrarios del proveedor.

## 2. Cobertura observada

| Conector | Lecturas/perfiles reconocidos | Escrituras reconocidas o límites |
|---|---|---|
| HA | Luces, switches, cover, climate y sensores; bindings especiales energéticos | On/off/brillo, posición y consigna térmica en mapper base; no todos los dominios/servicios HA. |
| Matter | Luces/enchufes on/off y dimmable; temperatura, humedad y ocupación | No soporte completo de clusters ni de tipos Matter. |
| Zigbee2MQTT | Luces, switches, sensores de temperatura/humedad/ocupación | No todos los `exposes`; clasificación por `state` requiere revisión con equipos no switch. |
| MQTT | Dispositivos y escalares de mapping v1 | Topics y comandos declarados; sin JSON anidado, scripting o autodiscovery arbitrario. |
| KNX | Puntos declarados de luces, sensores y batería | Direcciones/DPT acotados; no importación general de todo ETS. |
| Modbus | Puntos de luces, sensores, batería, EV, agua y thermal | Registros, escalas y operaciones definidos; no deducción del mapa de cualquier fabricante. |
| SDK | Contrato extensible y conformance | La extensión exige implementar el traductor; no aporta compatibilidad automáticamente. |

Fuente: mappers y configuraciones bajo `src/domoai/adapters/`; [cobertura anterior](../adapter-coverage.md) y [auditoría original](../auditoria-integral-universalidad-2026-09-06.md#6-cobertura-real-de-conectores). Esta tabla hereda la fecha de la auditoría.

## 3. Modelo semántico actual

Tipos canónicos: `light`, `switch`, `cover`, `climate`, `sensor`, `energy`, `ev_charger`, `unsupported`. La batería está representada por capabilities/perfiles energéticos, no por un enum independiente `battery`.

No agregar un tipo sólo por un sustantivo comercial. Determinar si necesita un contrato de interacción diferente, comandos, riesgo, estado y garantías que los tipos existentes no pueden expresar correctamente.

Una capability debe describir nombre, tipo de valor, unidad, rango/enumeración, lectura/escritura, comandos, fuente, disponibilidad y garantías. La normalización debe preservar el significado físico: porcentaje de apertura, SOC porcentual y energía almacenada no son magnitudes intercambiables.

## 4. Registro de cobertura propuesto

Cada fila debería identificar:

| Campo | Pregunta que responde |
|---|---|
| Tipo/función instalada | ¿Qué hace este equipo en la vivienda? |
| Capability y operación | ¿Qué acción semántica exacta se representa? |
| Conector/perfil/versión | ¿Quién traduce la operación? |
| Ruta de lectura | ¿De dónde sale el estado? |
| Ruta de escritura | ¿Existe control o sólo observación? |
| Unidad, escala y rango | ¿Qué significa el valor para el agente y el actuador? |
| Readback y tolerancia | ¿Cómo se confirma el resultado? |
| Riesgo/commissioning | ¿Qué preparación requiere, sin conceder permiso? |
| Nivel de evidencia | ¿Fixture, broker, proceso, equipo físico? |
| Restricción conocida | ¿Qué parte de la familia no se cubre? |
| Cambio invalidante | ¿Qué firmware/mapping requiere nueva evidencia? |

La fila describe cobertura; no reemplaza autorización del cliente, aprobación ni lease. El resource `domotics://coverage` no debe convertirse en un grant.

## 5. Familias que requieren una decisión de alcance

La auditoría detectó huecos en funciones habituales, no una orden de implementarlas todas:

- Iluminación: color, temperatura de color, efectos y transiciones además de brillo.
- Climatización: modo HVAC, ventilación, consigna dual y estados operativos además de temperatura.
- Acceso: cerraduras, puertas y alarmas, con consecuencias físicas específicas.
- Persianas: posición, movimiento, orientación/inversión y bloqueo por condiciones externas.
- Sensores: estados binarios, contacto, humo, agua y calidad del aire, según la representación elegida.
- Otros equipos: ventiladores, aspiradores y multimedia requieren justificar prioridad y semántica.

Priorizar por viviendas objetivo, operaciones frecuentes y evidencia disponible. No publicar una familia como completa si sólo funciona una operación de un perfil.

## 6. Riesgos específicos por conector

### HA

La presencia de una entidad no garantiza que su servicio sea soportado. Verificar supported features, unidades reportadas, valores unavailable/unknown, cambio de entity ID y múltiples entidades de un dispositivo físico. Los bindings de batería/EV son contratos adicionales, no inferencias de nombres.

### Matter y Zigbee2MQTT

Probar endpoints/exposes desconocidos y parcialmente soportados. Una entidad con propiedad `state` puede expresar movimiento u otro enum, no encendido binario. Rechazar conversiones incompatibles y conservar diagnóstico en lugar de inventar una capability writable.

### MQTT declarativo

Validar topic, codec, retained state, disponibilidad y feedback. No equiparar un payload aceptado por el broker con readback físico. Los límites de 256 dispositivos y 64 capabilities por dispositivo son límites por mapping observados, no capacidad global medida del runtime.

### KNX y Modbus

Comprobar DPT/codec, endianness, signos, factor de escala y unidad del equipo. Una lectura de registro correcta en bytes puede ser incorrecta en significado. Las direcciones de comando y feedback deben corresponder al mismo actuador y perfil.

## 7. Tareas propuestas

### F04-T01 — Inventario sin sobreafirmaciones

- [ ] Extraer operaciones realmente reconocidas por cada mapper/dispatch.
- [ ] Contrastar documentación y coverage resource con esas operaciones.
- [ ] Registrar unsupported y soporte parcial como resultados visibles.
- [ ] Detectar filas que confunden cobertura del proveedor con cobertura DomoAI.

### F04-T02 — Contrato de cada ampliación elegida

- [ ] Definir input, output, unidad, límites y riesgo de la capability.
- [ ] Elegir tipo existente o justificar uno nuevo.
- [ ] Especificar lectura, comando, idempotencia y confirmación.
- [ ] Revisar clientes, schemas, policy, Skills y compatibilidad antes de implementar.

### F04-T03 — Conformance y composición

- [ ] Probar formato válido, inválido, límite y desconocido.
- [ ] Probar reconexión, duplicados y pérdida de feedback.
- [ ] Ejecutar la operación por el MCP común y executor, no sólo adapter directo.
- [ ] Asociar la evidencia a versión de conector y perfil.

## 8. Escenarios mínimos

| Escenario | Aceptación |
|---|---|
| Capability leíble sin ruta de escritura | Visible como lectura; comando rechazado. |
| Equipo parcialmente soportado | Capacidades conocidas útiles y faltantes explicadas. |
| Dos fuentes para misma función | Routing único o ambigüedad bloqueada. |
| Valor fuera de rango o unidad incorrecta | Rechazo antes de escritura. |
| Nuevo firmware cambia payload | Invalidación/diagnóstico, no interpretación silenciosa. |
| ACK sin feedback | Resultado no confirmado, según contrato. |
| Desconexión de un conector | Los demás continúan sin inventar estado de la fuente caída. |

## 9. Comprobación futura

```bash
uv run --frozen pytest tests/contract/test_knx_adapter.py tests/contract/test_zigbee2mqtt_adapter.py tests/contract/test_provider_sdk.py
uv run --frozen pytest tests/integration/test_multi_adapter_runtime.py tests/composition/test_generic_mqtt_broker_composition.py
```

Seleccionar además los tests específicos del conector ampliado. Conformance de SDK no equivale a cobertura completa de un protocolo.

## 10. Cierre

- [ ] Matriz fuente/documentación/resource consistente.
- [ ] Cada ampliación elegida tiene operación y aceptación explícitas.
- [ ] No hay rutas vendor-specific en el MCP público.
- [ ] Lo no soportado se explica y no recibe autoridad de escritura.
- [ ] Evidencia clasificada por entorno, no sólo por cantidad de tests.

Salida hacia fase 05: capacidades instalables y límites. Riesgo residual: ningún inventario finito permite afirmar compatibilidad literal con toda domótica presente y futura.
