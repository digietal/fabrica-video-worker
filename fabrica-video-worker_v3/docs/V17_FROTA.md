# Worker v17 — vários Workers independentes

Mesmo pipeline do v16 (flags iguais, padrões desligados). Sem mudança no banco, no Gateway ou nos contratos.

## Por que já é seguro rodar N Workers
- Claim atômico: `claim_next_job()` usa `FOR UPDATE SKIP LOCKED` + `UPDATE ... WHERE status='QUEUED'` — um job vai para um só Worker.
- Dependências checadas no próprio claim (`job_is_eligible`).
- Lease de 60 min: job de Worker que morreu volta para a fila (até 3 tentativas).
- `/complete` e `/fail` idempotentes; falha de comunicação nunca vira FAILED.
- Cache local por contêiner (não compartilhar volume entre réplicas).

## Novo no v17
- `WORKER_ID` (padrão: `RAILWAY_REPLICA_ID` ou hostname+sufixo) em todo log `📊`.
- `queue_wait_s` no `job_start` (criação do job → início).
- `worker_heartbeat` a cada `HEARTBEAT_S` (60): ativos, capacidade, pesado esperando, consultas vazias, memória, CPU.
- `IDLE_JITTER` (0.3): espera ociosa ±30% para Workers não consultarem a fila em sincronia.
- `tools/fleet_report.py`: junta logs de vários Workers → duração por etapa, espera na fila, retries, falhas, tráfego, ocupação, tempo total.

## Recursos por Worker (recomendação inicial, validar)
| Recurso | Valor |
|---|---|
| Contêiner | 2 vCPU / 2 GB |
| WORKER_CONCURRENCY | 2 |
| HEAVY_CONCURRENCY | 1 (+ HEAVY_WAIT_RELEASE=1) |
| FFMPEG_THREADS | 2 |
| MIN_FREE_MEM_MB / HEAVY_MIN_FREE_MEM_MB | 280 / 450 |

Com 1 GB, usar WORKER_CONCURRENCY=1 (o MIX code -9 da Auto90 veio de concorrência 2 em 1 GB).

## Capacidade (hipóteses, não medido em produção)
- Medido (Railway, jobs reais, logs): CLEAN 14 s, SPLIT 28–38 s, REACT 48 s, MIX 35 s, TRIM 31 s, UGC_MATTE 15–19 s.
- Estimativa v16 (benchmark local): ~110 s de CPU por UGC → 100 UGCs ≈ 11.000 s.
- Tempo ≈ 11.000 / vagas efetivas. 10 vagas (5 Workers × 2) ≈ 18 min **se** a ocupação for ~100%; com 70% ≈ 26 min.
- 5 produções simultâneas no mesmo tempo exigiriam ~5× as vagas (~50).
- Custo: depende do preço real de CPU/RAM do provedor — medir 1 produção com `fleet_report.py` (busy_s) e multiplicar pelo preço por vCPU-hora.

## Plano de validação (uma rodada)
1. Criar 2 serviços Worker de teste com v17 (produção principal intacta) e `WORKER_MODE=loop`.
2. Rodar produção pequena (10 UGCs) com arquivos novos; coletar logs dos dois.
3. `python tools/fleet_report.py w1.txt w2.txt --run <id>`: conferir sem duplicados, ocupação, espera, memória.
4. Escalar para 5 Workers e produção de 100 UGCs; medir `wall_s`.
5. Reversão: desligar Workers extras; o v16 continua funcionando sozinho.
