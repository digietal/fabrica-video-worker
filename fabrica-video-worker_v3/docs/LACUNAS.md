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

1. **BACKGROUND_REMOVE**: não existe provider definido no produto. `BackgroundRemovalProvider` é o ponto isolado; hoje falha sem processar. Consequência: RENDER_REACT também fica bloqueado.
2. **THUMBNAIL**: `/complete` só cria artifacts; não atualiza `thumbnail_path` do artifact de render existente. O Worker registra um artifact `THUMBNAIL` próprio (com `thumbnail_path` = jpg). A galeria do app só verá a miniatura se passar a ler esse artifact ou se o `/complete` ganhar essa ligação.
3. **Cache**: não há rota para consultar artifacts por `cache_key`; o Worker não consegue pular trabalho já feito. O `/complete` já evita artifact duplicado (mesmo dono + cache_key + storage_path).
4. **TRIM**: usa silence_db=-35 e min_pause_ms=250, os únicos parâmetros existentes (definidos no cache_key em produce.ts).
5. **REACT_DEFAULT**: o produto não define tamanho do creator; usado 60% da largura, encostado no rodapé, centralizado. Confirmar.
6. Caminhos de saída escolhidos pelo Worker: `<owner>/<project>/<run>/{normalized/<asset_id>.mp4, mix|trim/<ugc_hash>.mp4, matte/<ugc_hash>.mov, renders/<creative_code>.mp4|.jpg, zips/<zip_name>}`.

## Memória (Railway)

`FFMPEG_THREADS` (variável de ambiente, padrão `2`) limita as threads do FFmpeg/x264 (`-threads N`, `-x264-params threads=N:lookahead-threads=1:rc-lookahead=10`) e os filtros usam `-filter_threads 1`. Objetivo: evitar que o x264 detecte todos os núcleos do host (ex.: 60 threads) e estoure a RAM do container (SIGKILL, code -9).
