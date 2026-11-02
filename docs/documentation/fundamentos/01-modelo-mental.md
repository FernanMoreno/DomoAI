# Modelo mental de un conector

Un conector es el puente entre un producto externo y el modelo común de
DomoAI. El producto puede hablar MQTT, HTTP, Modbus TCP, KNX/IP, Matter u otro
protocolo; el agente no debería tener que conocer los detalles de ese
protocolo.

## Las piezas

```text
Producto externo
      │
      ▼
Transport ── habla el protocolo físico o de red
      │
      ▼
Provider ── descubre, valida y construye el adapter
      │
      ▼
Adapter ── traduce estado y comandos al modelo DomoAI
      │
      ▼
ConnectionManager / ManagedAdapterSet
      │
      ├── DeviceRegistry
      ├── StateStore
      └── MCP
                │
                ▼
              Agente
```

### Transport

El transport sabe abrir y mantener la comunicación: conexión MQTT, petición
HTTP, sesión Modbus, túnel KNX o conexión al Matter Server. Las credenciales y
los detalles de conexión pertenecen al despliegue, no a la receta pública.

### Provider

El provider es la frontera de onboarding. Devuelve candidatos, comprueba que
un candidato corresponde al proveedor, valida la configuración y construye el
adapter. Para recetas, DomoAI proporciona esta lógica mediante
`RecipeConnectionProvider` y engines registrados.

### Adapter

El adapter transforma el protocolo en objetos normalizados: dispositivos,
capacidades, snapshots de estado y comandos. También conserva las garantías
del adapter nativo, como validación semántica, idempotencia, polling,
readback, health y errores.

### Registry, StateStore y MCP

El registry conoce qué dispositivos y capacidades existen. `StateStore` guarda
el estado normalizado. MCP ofrece al agente esa superficie semántica. El agente
ve, por ejemplo, `light.power` o `sensor.temperature`, no un topic MQTT crudo,
un registro Modbus arbitrario o un cluster Matter sin filtrar.

## Los tres niveles de authoring

| Nivel | Cuándo usarlo | Trabajo principal |
| --- | --- | --- |
| Receta declarativa | El protocolo y la semántica caben en un engine existente | Escribir YAML con mappings explícitos |
| Perfil o discovery específico | El protocolo existe, pero el producto necesita reglas adicionales | Añadir un perfil acotado o discovery bounded |
| Provider personalizado | Protocolo propietario, pairing especial, framing binario o semántica no representable | Implementar el contrato de cinco métodos y un adapter |

La elección no depende de la marca del producto, sino de cómo se comunica y
qué garantías necesita su integración.
