# Worker de Renderização Vertical GNI

Um worker Functions Framework/Cloud Run que baixa mídia de origem e assets
opcionais de logo do GCS, renderiza um MP4 fixo de 1080x1920, envia para o GCS
e atualiza o estado de render/capítulo no Firestore.

## API

`GET /health` é sem autenticação. `OPTIONS` oferece suporte a CORS. `POST /`
exige que o cabeçalho `X-API-Key` corresponda exatamente a `API_KEY` (o worker
falha fechado quando não definido). Um corpo representativo é:

```json
{
  "video_id": "video-1", "chapter_id": "chapter-1", "render_id": "render-1",
  "source_gcs_path": "gs://bucket/source.mp4",
  "output_gcs_path": "gs://bucket/renders/render-1.mp4",
  "trim_start": 10, "trim_end": 30,
  "video_resolution": {"w": 1920, "h": 1080},
  "mode": "split_stack",
  "keyframes_top": [{"time": 10, "x": 0, "y": 0}],
  "keyframes_bottom": [{"time": 10, "x": 900, "y": 540}],
  "captions": [{"start": 11, "end": 14, "text": "Legenda editável"}],
  "caption_style": {"font": "DejaVu Sans", "size": 64, "color": "#FFFFFF", "margin_bottom": 170},
  "logo": {"gcs_path": "gs://bucket/assets/logo.png", "position": "top_right", "size": 180, "opacity": 0.8},
  "text_overlays": [{"text": "AO VIVO", "position": "top_left", "size": 48, "opacity": 1, "color": "#FF0000"}]
}
```

`center_crop` usa a pipeline **autoframe v10** (`autoframe_v10/`): três passes
offline — análise (PySceneDetect + YOLO-face + tracking por IoU + LR-ASD para
locutor ativo), planejamento de câmera (histerese, planos `static`/`pan`/`track`,
zoom fixo por segmento, headroom 42%) e composição frame a frame com
legendas/logo/títulos na mesma codificação ffmpeg. O plano de câmera é
fingerprintado e publicado no GCS como `*.plan.json.gz`; o render final reutiliza
o plano exato do rascunho. Falhas v10 caem para um crop central determinístico;
`AUTOFRAME_ENGINE=legacy` devolve o detector v3 (YuNet + keyframes) como
rollback. Detalhes completos em `../autodetect.md`.
`split_stack` porta o modelo de keyframes top/bottom x/y independente do RPP,
incluindo easing de cosseno e limitação de crop. Tempos de legenda e tempos de
keyframe são tempos absolutos da mídia de origem. Licenças e proveniência dos
modelos ML estão em `THIRD_PARTY_NOTICES.md`.

Os caminhos no Firestore são `COLLECTION_VIDEOS/{video_id}/renders/{render_id}`
e, quando fornecido, `COLLECTION_VIDEOS/{video_id}/chapters/{chapter_id}`. Os
status são `PROCESSING`, `COMPLETED` e `ERROR`.

## Teste local e implantação

```bash
python3 -m pip install -r requirements-test.txt
./test.sh
./deploy.sh
```

`deploy.sh` lê `../.env`, envia para o Artifact Registry e implanta com limites
de Cloud Run apropriados para renderização. `TRANSCODE_USE_GPU=1` alterna para
uma imagem CUDA (`--build-arg TORCH_INDEX=...cu128`) com L4/NVENC e
`--execution-environment=gen2`; `0` mantém CPU/libx264. `AUTOFRAME_ENGINE`
(v10|legacy) é repassado ao serviço. Os pesos ML (`models/`,
`third_party/LR-ASD/weight/`) são rastreados por Git LFS — rode `git lfs pull`
antes de buildar a imagem localmente. A conta de serviço do runtime
precisa de acesso de leitura/escrita de objetos do Storage e escrita no
Firestore. Para um tratamento de secrets mais robusto, configure o
Cloud Run/Secret Manager após a primeira implantação em vez de manter `API_KEY`
como uma variável de ambiente em texto plano.
