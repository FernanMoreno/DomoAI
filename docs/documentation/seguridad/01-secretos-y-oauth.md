# Secretos y OAuth

La regla es separar mapping público, configuración de conexión y secretos.

## Qué puede estar en la receta

- IDs y nombres públicos;
- protocolo y estrategia de discovery;
- topics, paths, registros, group addresses y clusters;
- límites, unidades, DPTs y allowlists;
- flags públicos como `commissioning_required`.

## Qué no puede estar en la receta

- passwords, tokens, API keys o client secrets;
- URLs con `user:password@host`;
- códigos de commissioning o claves de fabric;
- tokens OAuth o refresh tokens;
- material sensible de KNX Secure.

Los tests de seguridad y el export de schemas verifican que los campos y
ejemplos no conviertan esos valores en metadata pública.

## Flujo de secretos

```text
operador
   ▼
ConnectionSetup / env reference
   ▼
secret store
   ▼
transport factory
   ▼
conexión
```

El provider recibe referencias o valores sólo en el borde estrecho de setup.
No debe copiarlos a manifest, candidate metadata, diagnostics, logs, recipe o
audit payloads.

## Home Assistant OAuth

OAuth tiene dos partes diferentes:

1. **Preflight local**, implementado por DomoAI.
2. **Registro de aplicación y autorización**, que ocurre en Home Assistant o
   en el proveedor externo.

Para comprobar la configuración pública local:

```bash
uv run domoai-admin oauth preflight \
  --env-file deploy/gateway.env
```

El preflight valida authorization URL, token URL, revoke URL opcional, client
ID, redirect URI y scopes. No hace red, no intercambia tokens y nunca imprime
secretos.

Un resultado `manual_required` con
`oauth_client_registration_required` significa que la configuración local es
coherente, pero aún hay que registrar externamente la aplicación y su redirect
URI.

## OAuth y qualification

La qualification software puede probar que el preflight y el ciclo de token
son seguros. No puede afirmar que la aplicación existe en la cuenta externa.
Ese paso debe aparecer como gate `oauth_registration` y completarse con el
secret store del despliegue.
