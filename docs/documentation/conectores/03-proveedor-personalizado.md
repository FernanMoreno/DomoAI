# Proveedor personalizado y sus cinco métodos

Los cinco métodos son la extensión para protocolos o comportamientos que no
caben en una receta. No hay que implementarlos para cada producto nuevo si el
producto encaja en un engine existente.

## El contrato

El provider expone métodos asíncronos en
`ConnectionProviderPort`:

```python
async def discover(self) -> list[ConnectionCandidate]: ...
async def probe(self, candidate: ConnectionCandidate) -> ProbeResult: ...
async def validate(self, setup: ConnectionSetup) -> ValidationResult: ...
async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort: ...
async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]: ...
```

## Qué hace cada método

| Método | Pregunta que responde | Resultado |
| --- | --- | --- |
| `discover` | ¿Qué candidatos de este proveedor existen? | Lista bounded de `ConnectionCandidate`. |
| `probe` | ¿Este candidato parece alcanzable y compatible? | Estado de transporte, inventario y producto. |
| `validate` | ¿La configuración seleccionada es válida? | `ValidationResult`, sin efectos parciales. |
| `build_adapter` | ¿Qué adapter ejecutará la conexión? | Un `AdapterPort` listo sólo después de validar. |
| `diagnose` | ¿Qué debe corregir el operador? | Diagnósticos accionables y no sensibles. |

## Flujo normal

```text
discover
   ▼
candidate seleccionado
   ▼
probe
   ▼
validate
   ▼
build_adapter
   ▼
ConnectionManager / ManagedAdapterSet
   ▼
diagnose cuando hay errores o mantenimiento
```

`discover` no activa nada. `probe` no debe convertir una respuesta parcial en
una conexión válida. `validate` debe rechazar antes de construir el adapter si
falta configuración o si la semántica no es segura.

## Cuándo hace falta

Usa un provider personalizado cuando exista alguna de estas condiciones:

- protocolo propietario o framing binario;
- serial, BLE o Thread directo;
- pairing o commissioning que requiere interacción específica;
- inventario que no puede expresarse mediante discovery bounded;
- semántica de comandos que no está en las allowlists de las recetas;
- transformaciones o readbacks que necesitan una política propia.

## Qué sigue siendo obligatorio

Un provider personalizado no puede publicar secretos, saltarse
`ConnectionManager`, crear comandos arbitrarios ni devolver un adapter que no
respete `AdapterPort`. El SDK y el conformance checker aíslan entry points
rotos y mantienen el resto de proveedores operativo.

## Regla de decisión

```text
¿MQTT, REST, Modbus, KNX o Matter?
  ├─ no → provider personalizado
  └─ sí
      ¿mapping explícito y seguro?
        ├─ sí → receta
        └─ no → profile/provider específico
```
