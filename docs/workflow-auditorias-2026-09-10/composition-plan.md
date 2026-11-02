# Revisión de composición del diseño — pendiente de ejecución

No es el gate postimplementación. Delimita qué habrá que demostrar al desarrollar; no declara PASS.

| Superficie | Invariante | Prueba necesaria |
|---|---|---|
| Evento → regla → executor | Destino temporal único, replay sin duplicado | Dos horas, misma hora, DST, replay/reinicio |
| Cola → adaptador → ledger | Cancelación antes de envío no actúa; después conserva incertidumbre real | Barreras deterministas antes/después del envío y cambio de autoridad mientras espera |
| ACK → readback → recovery | Terminalidad con owner/attempt/epoch y CAS | SQLite y PostgreSQL aislado; dueño obsoleto no modifica; reinicio no degrada confirmado |
| Lifecycle → health → admisión | Scheduler obligatorio vivo; tasks opcionales no compensan su caída | Cancelar cada tarea obligatoria con otras activas; verificar 503 y recuperación definida |
| Identidad → Management → conexión | Administración propia y target doméstico autorizado | Matriz read/mutate/manage, dos hogares, token estático y externo; sin ampliación implícita |
| HA → snapshot → registry | Un atributo inválido no elimina dispositivos sanos | Snapshot mixto null/unknown; fallo global de auth sigue visible |
| Registry/rutas → recursos MCP | Misma proyección autorizada y disponibilidad efectiva | Cero dispositivos visibles, jerarquía privada, proveedor vivo/ruta caída |
| Proveedores → solver → comparación | Demanda completa/ausencia explícita; no_action válido; moneda y origen conservados | Replays energéticos conocidos; coste/pérdidas/SOC; modo legado explícito |
| Perfil/tarea → plan → consentimiento | Versiones, autoridad y estados con un propietario | Otro agente continúa, conflicto concurrente, revocación y plan parcial |
| Manual/external → replan → scheduler | Prioridad observable e histéresis, sin lucha de consignas | Intervención conocida/incierta, automatización externa, sin Internet |
| Historial → ahorro → vista/aviso | Medida/estimación diferenciadas y aviso deduplicado | Huecos, tarifa nueva, replay/reinicio, hogares separados |
| Backup → restore → runtime | Recuperar datos sin reejecutar efectos ni restaurar acceso revocado | Dependencias reales desechables, claves/retención y traspaso |

Arquitectura: Import Linter del proyecto; no inventar fronteras nuevas. Contratos: pytest contract/composition local; Pact o equivalente adicional si consumidor/proveedor se despliega/evoluciona independientemente. PostgreSQL y transportes externos cuando el límite lo requiera; HIL separado con perfil, firmware y readback. Cualquier fallo se depura hasta la primera frontera incorrecta antes de editar.
