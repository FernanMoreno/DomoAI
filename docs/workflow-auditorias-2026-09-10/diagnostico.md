# Fuente, reproducciones y causa raíz

Ejecución nueva posterior a Spec Kit, Graphify e inspección. Los scripts asertan comportamiento defectuoso: exit 0 significa **fallo reproducido**, nunca corregido. Fixtures locales, sin hardware.

| ID | Primera frontera incorrecta | Evidencia observada |
|---|---|---|
| A-01 | `local_automation.py:181,327`. evaluate_time emite evento sin destino estructurado; handle_event lo compara contra todas las reglas y matcher solo comprueba tipo time. | [Regla de 18:00 se ejecuta al evaluar 12:00.](repro-ejecucion.txt) |
| A-02 | `household_queue.py:99; executor.py:560`. Cancelar future no retira el trabajo: worker invoca item.work antes de comprobar future.done; executor ya marcó intento de despacho al entrar en cola. | [Plan unknown con cero llamadas; drenar cola produce una llamada física simulada.](repro-cancelacion.txt) |
| A-03 | `persistence/coordination.py:131,142; application/executor.py:568,781`. settle solo acepta origen dispatching; ACK→confirmed se descarta y recuperación reinterpreta ACK como incierto. | [Resultado confirmado conserva ledger acknowledged; recovery lo vuelve unknown.](repro-ejecucion.txt) |
| A-04 | `mcp/health.py:113; application/runtime_lifecycle.py:61`. Readiness cuenta tareas sin verificar identidad y vida del scheduler obligatorio. | [Scheduler cancelado, tres tareas vivas: HTTP 200 ready.](repro-readiness.txt) |
| A-05 | `management/api.py:124,308; management/auth.py:121`. Autorización genérica execute sin target de conexión/hogar; alias mutate permite operación administrativa global. | [Token restringido obtiene 200 y llama disable en doble ASGI; no prueba bypass del executor físico.](repro-superficie.txt) |
| A-06 | `adapters/home_assistant/provider.py:1283,326`. float(None) no normalizado en brillo; comprensión global aborta snapshot entero por una entidad. | [TypeError ante brightness null.](repro-superficie.txt) |
| A-07 | `mcp/resources.py:29; mcp/domotics_server.py:1723`. Filtro de dispositivos no filtra áreas y sus referencias al serializar inventario. | [devices=[] aún revela área privada.](repro-superficie.txt) |
| A-08 | `mcp/resources.py:145`. Estado active se deriva de proveedor presente, sin disponibilidad efectiva de ruta. | [Ruta unavailable publicada active.](repro-superficie.txt) |
| A-09 | `optimizer/providers.py:476,569; optimizer/cp_sat.py:158`. Composer carece de proveedor base y no transmite forecast; solver aplica cero por default legado. | [Coste compuesto 0 frente a 0.4 con 1kW base durante 2h a 0.2.](repro-energia.txt) |
| A-10 | `optimizer/counterfactual.py:17,68,89`. Conjunto de éxito excluye no_action_required, descartando baseline pasivo y sus alternativas. | [Baseline pasivo válido con coste 0.4 genera variations={}.](repro-energia.txt) |
| A-11 | `optimizer/cp_sat.py:565`. Resta toda exportación de toda solar sin conservar origen de energía exportada. | [1kWh solar consumido + exportación posterior de batería inicial informa autoconsumo 0.](repro-energia.txt) |
| A-12 | `optimizer/energy.py:130; optimizer/cp_sat.py:513`. Valida forma de series pero no moneda homogénea; acumulador suma números sin conversión. | [0.2 EUR + 0.2 USD aceptados como coste 0.4.](repro-energia.txt) |
