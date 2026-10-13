# Lacunas de contrato (Gateway x Worker)

O Worker não acessa o banco. Hoje o `/claim` só enriquece INGEST e NORMALIZE (storage_path/file_name/mime_type).
Para as etapas abaixo, o job salvo não diz ONDE estão os arquivos das etapas anteriores, e nenhuma rota do Gateway
permite consultar artifacts. Por isso esses handlers estão implementados, mas falham com erro controlado
(`GatewayContractGap` -> `/fail`) até o Gateway entregar `payload.inputs` na resposta do `/claim`.

Enriquecimento proposto (somente na resposta; IMPLEMENTADO no Gateway — cada input é um objeto {storage_path, file_name, mime_type, artifact_id, cache_key, kind}; ZIP.files traz também position e final_file_name; dependências ausentes vêm em payload.inputs_missing):

| Job | payload.inputs esperado | Origem no banco |
|---|---|---|
| MIX | hook, body1, body2 (se houver), cta | artifact NORMALIZE de cada *_asset_id |
| TRIM | mix | artifact MIX do mesmo ugc_hash |
| BACKGROUND_REMOVE | trim | artifact TRIM do ugc_hash |
| RENDER_CLEAN | trim | artifact TRIM do ugc_hash |
| RENDER_SPLIT | trim, base | + artifact NORMALIZE de production_items.base_asset_id |
| RENDER_REACT | trim, matte, base | + artifact BACKGROUND_REMOVE do ugc_hash |
| THUMBNAIL | render | artifact de production_items.artifact_id |
| ZIP | files [{storage_path, file_name}], folder, zip_name | block_items (ordem position) + artifacts dos itens; BLOCO_NN_PRODUTO_DD-MMM |

Outras lacunas:
1. **BACKGROUND_REMOVE**: RESOLVIDO — provider local Robust Video Matting (ONNX/CPU) em `worker/matting.py`. Gera `matte/<ugc_hash>.mp4` (máscara alfa em tons de cinza, 540x960, 30 fps, mesma duração do TRIM). Falha real -> /fail (modelo ausente, FFmpeg, nenhum quadro, nenhuma pessoa detectada). Variáveis: BG_PROVIDER=rvm, RVM_MODEL_PATH, BG_WIDTH=540, BG_DOWNSAMPLE=0.4, BG_MIN_COVERAGE=0.01.
2. **THUMBNAIL**: RESOLVIDO no Gateway — `/complete` copia o JPG para `thumbnail_path` do artifact de render do item (vale para CLEAN, SPLIT e REACT). Histórico: O Worker registra um artifact `THUMBNAIL` próprio (com `thumbnail_path` = jpg). A galeria do app só verá a miniatura se passar a ler esse artifact ou se o `/complete` ganhar essa ligação.
3. **Cache**: não há rota para consultar artifacts por `cache_key`; o Worker não consegue pular trabalho já feito. O `/complete` já evita artifact duplicado (mesmo dono + cache_key + storage_path).
4. **TRIM**: usa silence_db=-35 e min_pause_ms=250, os únicos parâmetros existentes (definidos no cache_key em produce.ts).
5. **REACT_DEFAULT**: o produto não define tamanho do creator; usado 60% da largura, encostado no rodapé, centralizado. Confirmar.
6. Caminhos de saída escolhidos pelo Worker: `<owner>/<project>/<run>/{normalized/<asset_id>.mp4, mix|trim/<ugc_hash>.mp4, matte/<ugc_hash>.mp4, renders/<creative_code>.mp4|.jpg, zips/<zip_name>}`.

## Memória (Railway)
FFMPEG_THREADS (padrão 2) limita FFmpeg, x264 (`threads=N:lookahead-threads=1:rc-lookahead=10`), `-filter_threads 1` e o ONNX Runtime do recorte. Medido localmente: pico ~750 MB no BACKGROUND_REMOVE+REACT; demais etapas 300–600 MB. Recomendado: 1 GB+ de RAM no serviço.

## REACT
RENDER_REACT = Base normalizada no canvas 1080x1920 + Creator (UGC TRIM) com alfa do matte (`alphamerge`), 60% da largura, inferior central; áudio só do UGC; H.264/AAC 48 kHz/30 fps.

## RENDER_REACT — memória (v5)
- Composição trocada de RGBA+alphamerge+overlay para maskedmerge em yuv420p (Creator e máscara com o mesmo scale/crop/pad 1080x1920; máscara preta fora do Creator).
- Decoders das 3 entradas limitados a FFMPEG_THREADS; -max_muxing_queue_size 1024.
- matting.py libera sessão ONNX e buffers NumPy (gc.collect) ao fim do BACKGROUND_REMOVE.
- Medido localmente (25 s): pico 877 MB -> 612 MB.

## MATTE reutilizável por asset (Bloco 1)
- ASSET_MATTE (1 por Gancho/Corpo/CTA, nunca Base): RVM no asset normalizado -> `<owner>/<project>/<run>/matte/assets/<asset_id>.mp4` (cinza, 540x960, 30 fps).
- UGC_MATTE (1 por UGC, sem IA): concat dos ASSET_MATTE na ordem do MIX + mesmos cortes do TRIM (recalculados do MIX com os mesmos parâmetros) -> `matte/ugc/<ugc_hash>.mp4`.
- RENDER_REACT consome UGC_MATTE (fallback: BACKGROUND_REMOVE de produções antigas). Composição maskedmerge inalterada.
- CLEAN/SPLIT nunca dependem de MATTE.
- Reuso entre produções diferentes ainda não ativo: o Gateway só libera arquivos da própria produção.

## v8 — VOICE_CONVERT (ElevenLabs Speech-to-Speech)
- Novo handler `worker/voice.py`: NORMALIZE → VOICE_CONVERT (paralelo ao ASSET_MATTE). Extrai WAV, chama
  POST /v1/speech-to-speech/{voice_id} (model eleven_multilingual_sts_v2, mp3_44100_192), remuxa com o vídeo
  normalizado (vídeo copiado → mesmos frames; matte continua alinhado). Sem time-stretch: diferença ≤ max(0,6s; 5%)
  é corrigida com silêncio/corte suave; acima disso o job FALHA.
- Retentativa: até 3 tentativas para 429/5xx/rede (respeita Retry-After); 401/402/404/créditos falham na hora.
- Variável obrigatória no Railway: ELEVENLABS_API_KEY (opcional: ELEVENLABS_TIMEOUT, VOICE_MAX_DRIFT_S).
- MIX: o Gateway entrega o artifact VOICE_CONVERT quando o asset tem voz; sem fallback para o original.

## v10 — Concorrência controlada + cache local + métricas
Variáveis (todas opcionais; padrão conservador):
| Variável | Padrão | Significado |
|---|---|---|
| WORKER_CONCURRENCY | 2 | jobs simultâneos neste processo (teto WORKER_CONCURRENCY_MAX=4) |
| HEAVY_CONCURRENCY | 1 | jobs pesados simultâneos (ASSET_MATTE, BACKGROUND_REMOVE, RENDER_REACT) |
| MIN_FREE_MEM_MB | 450 | não reivindica novo job se a memória livre do contêiner for menor |
| FFMPEG_THREADS | 2 | threads por processo FFmpeg (inalterado) |
| MAX_JOBS | 1 | total de jobs em WORKER_MODE=once — NÃO é concorrência |
| CACHE_ENABLED / CACHE_DIR / CACHE_MAX_MB / CACHE_TTL_HOURS | 1 / /tmp/fabrica-cache / 3000 / 12 | cache local |
Rollback sem reimplantar: WORKER_CONCURRENCY=1 e CACHE_ENABLED=0 reproduzem o comportamento do v9.
Logs: linhas `📊 {"event":"job_start"|"job_end"|...}` em JSON (run_id, job_id, type, duração, active_jobs,
download/upload s+bytes, cache_hit/miss, memória, load, erro). Testes: `python -m unittest discover -s tests -t .`
