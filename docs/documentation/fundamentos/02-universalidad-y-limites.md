# Universalidad y límites

DomoAI puede integrar muchos productos sin crear un adapter desde cero para
cada marca, pero no puede detectar de forma segura “cualquier cosa” sólo a
partir de una URL, una IP o un nombre de producto.

## Lo que sí es genérico

Un engine es reutilizable cuando define una forma segura de:

- encontrar candidatos;
- validar el endpoint y la configuración;
- enumerar dispositivos o fuentes;
- leer estados;
- ejecutar sólo comandos permitidos;
- confirmar readback cuando una operación modifica estado;
- traducir el resultado a capacidades normalizadas.

Por eso una receta Modbus sirve para muchos fabricantes si el mapa de
registros es conocido. Una receta MQTT sirve para productos con topics y
payloads compatibles. El nombre comercial no tiene que estar codificado en
DomoAI.

## Lo que no se puede inferir de forma segura

No se debe asumir automáticamente:

- que un puerto abierto significa que el producto es compatible;
- que un registro Modbus representa temperatura o potencia;
- que una dirección KNX usa un DPT concreto;
- que un cluster Matter propietario puede ejecutarse sin allowlist;
- que cualquier endpoint HTTP acepta los mismos métodos y payloads;
- que una capacidad con nombre `power` tiene la misma semántica en todos los productos.

Estas decisiones necesitan un mapping explícito, un perfil validado o código de
proveedor.

## Protocolos incluidos en el flujo de recetas

- MQTT.
- REST/OpenAPI sobre HTTP o HTTPS.
- Modbus, actualmente con el flujo declarativo orientado a Modbus TCP.
- KNX/IP, con DPT y group addresses explícitos.
- Matter a través de un Matter Server y nodos ya comisionados.

BLE, Thread directo, serial propietario, protocolos binarios y flujos de
pairing no se convierten automáticamente en recetas sólo por existir una
conexión de red. Para ellos se usa un provider personalizado o se añade un
engine específico con sus límites y tests.

## Regla práctica

Pregunta en este orden:

1. ¿El producto usa uno de los cinco transportes soportados?
2. ¿Conocemos sus topics, operaciones, registros, direcciones o interacciones?
3. ¿Podemos declarar lecturas, escrituras y readback sin usar comodines
   peligrosos?
4. ¿El producto necesita pairing o semántica que no está en el perfil?

Si las respuestas a 1–3 son sí y a 4 es no, usa una receta. Si no, sube al
nivel de perfil o provider personalizado.
