# Discovery, probe, validate y diagnose

Estas operaciones forman el onboarding del provider. En una receta las ejecuta
`RecipeConnectionProvider` delegando en engines declarativos; en un provider
custom las implementa el autor.

## `discover`

Devuelve candidatos del proveedor. Debe ser bounded, determinista en lo
posible y libre de secretos en endpoint y metadata.

Los engines de recetas actuales ofrecen discovery manual acotada:

- Modbus: `host` y `port`, por defecto `502`.
- KNX: `host` y `port`, por defecto `3671`.
- Matter: URL `ws://` o `wss://` del Matter Server.

Discovery no activa el adapter ni prueba todas las IPs de una red.

## `probe`

Comprueba si el candidato tiene una forma compatible y devuelve estados de
transporte, inventario y producto. Una respuesta `UNKNOWN` o `NEEDS_SETUP` no
equivale a una conexión lista.

Debe rechazar candidatos de otro `connector_id` y endpoints fuera de la
política permitida.

## `validate`

Valida el `ConnectionSetup` completo y comprueba que haya engine disponible.
Aquí se aplican el schema, las allowlists, la compatibilidad del endpoint y
las reglas de readback.

La validación de una receta no hace network I/O. Si falla, `build_adapter` no
debe construir ni activar nada.

## `build_adapter`

Construye un `AdapterPort` usando la fábrica de transporte inyectada. Sólo se
llama después de `validate` y recibe un `adapter_id` estable del runtime.

En el scaffolding generado para Modbus, KNX y Matter la fábrica contiene un
placeholder que lanza un error hasta que el despliegue suministra el transport
real. Esto es intencionado: evita declarar operativo un conector incompleto.

## `diagnose`

Devuelve diagnósticos accionables para el operador: qué falló, si es retryable
y qué acción debe realizar. Los mensajes no deben revelar secretos ni datos
completos de autenticación.

## Ejemplo de diagnóstico correcto

```text
code: recipe_transport_unsupported
severity: error
retryable: false
user_action: Install or implement the declared transport engine
```

El diagnóstico explica el siguiente paso sin imprimir la configuración
sensible que provocó el fallo.
