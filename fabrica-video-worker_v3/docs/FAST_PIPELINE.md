# FAST_PIPELINE — Fase 1 (Worker v13)

Desligado por padrão. `FAST_PIPELINE=0` (ou ausente) = comportamento idêntico ao v12.
Nenhuma mudança em banco, jobs, contratos, cache_keys, planejamento, Bases, blocos, formatos ou downloads.
O método usado em cada job fica em `artifacts.metadata.pipeline` e no log `📊 {"event":"fast_pipeline",...}`.

## O que muda com FAST_PIPELINE=1
| Job | Caminho rápido | Condição de segurança (senão: legado) |
|---|---|---|
| NORMALIZE (Gancho/Corpo/CTA) | Remove só silêncio das BORDAS na mesma codificação (custo zero). Margem 100 ms antes / 120 ms depois (`FAST_EDGE_LEAD_MS`, `FAST_EDGE_TAIL_MS`); soma sempre < pausa mínima do TRIM | Asset com áudio; sobra ≥ 0,3 s; análise ok. Bases nunca. |
| MIX | Vídeo por concat demuxer `-c copy` (0 reencodificação de vídeo); áudio de cada parte ajustado à duração do seu vídeo e concatenado em AAC (barato) | Mesmo codec/perfil/resolução/pix_fmt/30fps/timebase/cor/SAR; áudio igual; vídeo e áudio de cada parte com mesma duração (≤ 1 frame + 1 frame AAC); timestamps em grade exata de 1/30 s; saída validada (frames = soma, início 0, 30 fps, A/V juntos) |
| TRIM | Cópia do MIX quando o TRIM legado não cortaria nada | `silence_keep` = 1 intervalo cobrindo o arquivo; saída validada |
| UGC_MATTE | Concat `-c copy` das máscaras dos assets | TRIM sem cortes **e** frames da máscara = frames do vídeo usado no REACT |
| RENDER_CLEAN | já era cópia desde v11 | — |
| SPLIT / REACT | sem mudança (composição idêntica) | — |

Qualquer falha/incompatibilidade no caminho rápido apaga a saída parcial e executa o método legado no mesmo job.

## Testar em ambiente controlado
1. Local, sem banco: `WORKER_SECRET_TOKEN=x python tools/benchmark_fast_pipeline.py` (ou `--assets DIR` com vídeos reais).
2. Railway: criar um **segundo serviço** (staging) com a mesma imagem v13 e `FAST_PIPELINE=1`, e parar o Worker principal só durante o teste
   (ou usar uma conta de teste). Fazer 1 produção nova pequena (10 UGCs, 3 formatos). Conferir nos logs `fast_pipeline` e
   `job_end`; assistir LIMPO/TELA DIVIDIDA/REACT; comparar com uma produção igual em `FAST_PIPELINE=0`.

## Voltar ao legado
`FAST_PIPELINE=0` (ou remover a variável) e reiniciar. Ou reimplantar v12. Artefatos já gerados continuam válidos.

## Observação de cache
NORMALIZE com bordas decupadas usa o mesmo cache_key do legado. Uma produção posterior com o mesmo arquivo pode
reaproveitar o asset normalizado já decupado (só bordas silenciosas, com margem). Para isolar 100%, testar com arquivos novos.

## v15 — rodada ampla de otimização

- **H (cache):** NORMALIZE com decupagem de borda reporta cache_key com sufixo `|fast1` — um asset
  decupado nunca é reutilizado como se fosse legado (mesmo storage_path, cache_key diferente).
- **D (LIMPO):** com FAST_PIPELINE=1, se o artifact do TRIM já está no formato final, o job
  RENDER_CLEAN conclui referenciando o PRÓPRIO artifact do TRIM (result.artifact_id) — sem download,
  sem remux, sem upload, sem arquivo duplicado no Storage. Fallback ao render legado em qualquer dúvida.
- **I/J (THUMBNAIL/ZIP):** RENDER_CLEAN/RENDER_SPLIT/RENDER_REACT entraram no cache local
  (CACHE_KINDS). THUMBNAIL e ZIP consomem o render já em disco (hardlink) em vez de baixar de novo.
- **G (BASE_PREP, flag própria, padrão 0):** prepara cada Base uma vez por formato (SPLIT 1080x960,
  REACT 1080x1920, CRF 18) com escrita atômica + lock. Benchmark local (10 UGCs, 2 Bases): NÃO compensou
  (57,9s vs 52,2s; +4 recodificações) — o preparo custa mais que o scale/crop economizado por render.
  Manter desligado; reavaliar apenas se uma produção tiver muitos UGCs por Base E medição no Railway
  mostrar ganho. Trade-off: Base passa por dupla compressão (perda leve de qualidade).
- Renders aceitam `prepared=` (Base pré-preparada); sem ela, o grafo é idêntico ao v14.

Flags: FAST_PIPELINE=0 (padrão), BASE_PREP=0 (padrão). Reversão: ambas em 0 = comportamento v12/v14.
