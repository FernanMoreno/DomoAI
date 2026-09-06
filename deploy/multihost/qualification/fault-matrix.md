# Multi-host lab v2 fault matrix

Este documento describe únicamente evidencia reproducible en Docker. No es un
runbook de producción ni acredita un gateway físico.

| Scenario ID | Fault injected | Safe result | Evidence produced | Not covered |
| --- | --- | --- | --- | --- |
| `ownership-race` | `host-a` y `host-b` compiten 20 veces por el mismo hogar | Un solo owner por carrera; `unauthorized_writes=0` | epoch/owner allowlisted, JSONL `lab` | dos dominios físicos reales |
| `partition-takeover` | Se desconecta la red del owner con el socket abierto | Tras TTL, takeover activo-pasivo; epoch stale rechazado | `stale_epoch_rejected=true` | último salto KNX/Matter/HA físico |
| `crash-replay` | Crash después del claim durable | Intent pasa a `unknown`; outbox reintenta y entrega una vez | `outbox_delivery_count=1` | crash de gateway físico real |
| `control-plane-loss` | Se detiene un miembro etcd | El quorum conserva coordinación | `etcd_member_loss_recovered=true` | partición que elimine quorum |
| `database-primary-failover` | Se detiene el primary Patroni identificado | HAProxy observa otro writer | primary inicial/sucesor y writer writable | RPO/RTO de producción |
| `secure-rotation` | CA efímera, cliente válido, rotado, no confiable y expirado | mTLS local acepta/rechaza según certificado | flags booleanos sin PEM | PKI, mTLS y rotación productivos |
| `backup-restore` | `pg_dump` y restore a PostgreSQL desechable | Sentinels de intents, outbox y métricas preservados | tiempos y conteos | política cifrada, RPO/RTO y restore operativo |
| `bounded-load` | 24 admisiones sobre límites 8/8 | Backpressure por hogar y total | profundidad máxima/rechazos | carga representativa de hogares reales |

## Ejecución

```bash
./scripts/run_multihost_lab_v2.sh
```

El script crea un proyecto Compose único, no publica puertos, limita los
fallos a recursos con `com.domoai.lab=true` y elimina contenedores, red y
volúmenes al salir. `DOMOAI_LAB_RACE_COUNT` y
`DOMOAI_LAB_SKIP_SCENARIOS` existen solo para depuración local; el gate normal
usa 20 carreras y los ocho escenarios.

## Interpretación

Un resultado `passed` solo demuestra la composición software/laboratorio. La
activación productiva sigue requiriendo PKI/mTLS real, service discovery,
dominios de fallo independientes, backups cifrados con restore probado,
commissioning HIL y evidencia firmada del gateway. `active-active` permanece
bloqueado.
