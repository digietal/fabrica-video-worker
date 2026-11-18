# Worker v18 — personalização visual por produção

Sem flags novas: tudo chega no **payload do job** (valores definidos pelo app por produção).
Produções antigas, sem os campos novos, renderizam exatamente como antes.

## TELA DIVIDIDA — `payload.split_style`
- `EXPAND` (padrão; visual v14): avatar ocupa 100% da largura da metade inferior (cover),
  corte vertical ancorado perto do topo (15%) para preservar cabeça/olhos.
- `BLUR` (visual v12): avatar inteiro (contain, sem corte) centralizado na metade inferior,
  laterais preenchidas com o próprio UGC desfocado (`gblur`).
- Base sempre na metade superior 1080×960 (cover). Áudio só do UGC. Saída 1080×1920/30fps.

## REACT — `payload.react_bottom_margin`
- Margem inferior do Creator em px (padrão 24 = comportamento antigo; 0 = encostado na borda).
- Recorte do Creator ancorado no TOPO (`crop y=0`): cabeça/olhos nunca são cortados quando a
  proporção difere de 9:16.
- Tamanho inalterado: 60% da largura, canto inferior direito.

## Máscara — `payload.matte_expand` (ASSET_MATTE)
- Dilatação morfológica da máscara alfa em px (0–8, padrão 0), NumPy puro, vizinhança 8.
- Preserva dedos, mãos, embalagens e produtos que o RVM corta nas bordas.
- Custo: desprezível por frame (540×960); sem impacto relevante no tempo do ASSET_MATTE.
- Faz parte da cache_key do ASSET_MATTE/UGC_MATTE (app): mudar o valor gera máscaras novas.

## Novo job `PREVIEW`
- Trecho de ~4s (`PREVIEW_SECONDS`) de UM UGC em SPLIT ou REACT, com o MESMO pipeline do render
  final (`max_dur` no render_split/render_react) — a prévia representa o resultado real.
- Sem `production_item_id`; não toca em `production_items`. Artifact `kind='PREVIEW'` em
  `<owner>/<project>/<run>/previews/`.
- Payload: `ugc_hash`, `format`, `base_asset_id`, `split_style`, `react_bottom_margin`,
  `creative_code`. Elegibilidade (banco): TRIM do UGC + NORMALIZE da Base (+ UGC_MATTE se REACT).
- Fluxo no app: produção criada com status `PREVIEW` enfileira só a preparação + 1 UGC + prévias;
  `confirmProduction` enfileira o restante com as configurações aprovadas.
