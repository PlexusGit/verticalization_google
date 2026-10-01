# Enquadramento automático (estratégia v10)

Este documento descreve o comportamento atual do algoritmo de enquadramento automático do Plexus Vertical Cuts. Esta é a **versão 10 da estratégia** (`AUTOFRAME_STRATEGY_VERSION = 10`, `focal_strategy_version = 10` nos `render_options` do capítulo): em vez de rastrear o rosto frame a frame com heurísticas reativas, o vídeo passa por **três passes offline** — análise, planejamento de câmera e composição — com look-ahead sobre o capítulo inteiro antes de decidir para onde a câmera virtual olha.

> A estratégia anterior (v3, YuNet + fluxo óptico + keyframes no FFmpeg) continua disponível como caminho de rollback: basta definir `AUTOFRAME_ENGINE=legacy` no `.env`/deploy do worker. O modo manual `split_stack` não é afetado pela v10.

## Objetivo

Transformar um vídeo de origem em uma composição vertical 9:16 mantendo o falante ativo dentro da área visível, com planos de duração mínima, transições controladas (corte seco ou pan) e zoom variável — eliminando a "respiração" do crop, a perseguição atrasada e a troca errada de falante por um frame isolado.

## Arquitetura em três passes

```text
Vídeo do capítulo
       │
       ▼
PASSE 1 — analyze (autoframe_v10/analyze.py)
  cenas (PySceneDetect) · áudio+VAD · rostos (YOLO-face)
  tracks por IoU · locutor ativo (LR-ASD) · fallback visual (FaceMesh)
       │
       ▼  analysis.json (workdir temporário)
PASSE 2 — plan (autoframe_v10/plan.py)
  falante por frame com histerese · segmentos de plano
  static / pan / track / nobody · zoom e headroom por segmento
       │
       ▼  plan.json → artefato versionado no GCS
PASSE 3 — render (autoframe_v10/render.py)
  composição por frame em OpenCV → rawvideo → ffmpeg
  legendas ASS + logo + títulos na mesma codificação · áudio AAC da origem
       │
       ▼
Vídeo vertical 1080 × 1920
```

A separação em passes é o diferencial: o planejador enxerga o vídeo inteiro (quem fala quando, onde cada rosto estará) antes de emitir qualquer movimento de câmera.

## 1. Passe de análise

- **Vídeo**: `cv2.VideoCapture`; `fps`, dimensões e o intervalo `trim_start`–`trim_end` são lidos da fonte.
- **Cenas**: `scenedetect` `ContentDetector` (threshold 27, cena mínima de 0,5 s). Todos os passos seguintes acontecem **dentro de cada cena**, impedindo que tracks vazem através de cortes.
- **Áudio e VAD**: áudio mono 16 kHz extraído via ffmpeg; RMS por frame em janela de `0,15 s`; limiar de fala = máximo entre `0,005` e `0,15 × percentil 95` do RMS. Vídeos sem áudio degradam para silêncio (o restante da análise continua).
- **Rostos**: `yolov11n-face.pt` (Ultralytics YOLO11n-face), entrada 640 px, confiança mínima `0,5`, inferência em lotes de 16 frames. Dispositivo por `resolve_device()`: `TRANSCODE_USE_GPU=1` → CUDA → MPS → CPU; `0` → CPU. `AUTOFRAME_DEVICE` força um dispositivo.
- **Tracking**: associação gulosa por IoU (`track_iou=0,4`), tolera até `track_max_gap=10` frames sem detecção com interpolação linear das caixas e descarta tracks com menos de `track_min_len=10` frames.
- **Locutor ativo (LR-ASD)**: modelo leve áudio-visual (0,84M parâmetros, MIT) que combina MFCC 100 fps com crops faciais 112×112 a 25 fps e produz um logit de fala por frame. Roda em CPU mesmo quando o detector usa GPU/MPS (o `max_pool3d` do PyTorch não existe em MPS).
- **Fallback visual**: quando `|logit| < asd_low_conf=1,0`, o score é mesclado com a abertura labial medida por FaceMesh (landmarks 13/14/61/291, janela de 10 frames). MediaPipe é opcional — em Python 3.13 não há wheel e o fallback simplesmente pontua zero.
- **Score de fala**: `speak = w·sigmoid(logit) + (1−w)·(0,5·sigmoid(logit) + 0,5·visual)`, com `w = |logit|/asd_low_conf` truncado em [0,1]; frames fora de fala (VAD) recebem portão ×0,3.

Saída (`*_analysis.json`): `fps`, `width`, `height`, `scenes`, `tracks` (frames, boxes, `asd_logit`, `visual`, `speak`), `audio_rms`, `vad`.

## 2. Passe de planejamento

Doutrina: **não mova a menos que precise**.

- **Seleção do alvo**: por frame, o track com maior `speak`; um novo falante só assume se ficar acima de `speak_thr=0,5` por `confirm_sec=0,4 s` e o plano atual já durar `min_shot_sec=1,5 s`. Trechos de alvo com menos de 0,3 s são absorvidos pelo segmento vizinho (sem flicker).
- **Zoom**: `crop_h = face_h × 3,2`, limitado entre `0,55 × altura da fonte` (evita pixelar) e a altura total; a largura segue a proporção 9:16 de saída. O zoom é **fixo por segmento** — nada de respiração.
- **Modos por segmento**:
  - `static`: centro estável (desvio < `1,2%` da largura) → crop congelado;
  - `pan`: deriva suave entre dois patamares próximos;
  - `track`: o rosto se desloca e o crop acompanha com zona morta (`2%`) + suavização gaussiana bidirecional (`σ=0,35 s`);
  - `nobody`: sem falante → crop central (`no_face_mode=center`).
- **Corte vs. pan entre falantes**: distância acima de `20%` da largura → corte seco; próximos e coexistentes → pan de `0,45 s` com easing cossenoidal.
- **Composição**: centro do rosto a `42%` da altura do crop (`headroom`) — regra dos terços adaptativa.
- **Split**: o planejador sabe produzir `layout=split` para diálogos (`multi_mode=split`), mas o worker fixa `multi_mode=cut` nesta fase — o modo manual `split_stack` continua intacto para dois falantes.

Saída (`*_plan.json`): por frame — `layout`, `crop=[x,y,w,h]` em coordenadas da fonte, `track`, `mode`, `scene`, `trans` — mais a lista de segmentos com suas transições.

## 3. Passe de composição

Cada frame da fonte é recortado conforme o plano (`LANCZOS4` para 1080×1920) e enviado como `rawvideo bgr24` a um único processo ffmpeg, que aplica na mesma codificação:

- legendas ASS (`write_ass_captions`, tempos relativos ao recorte);
- logo (frame único, `overlay` com `eof_action=repeat` — sem `-loop 1`);
- textos de título (`drawtext`, com centralização na caixa quando `max_width` definido);
- áudio da fonte em AAC 160 kbps com `-shortest`;
- encoder por `resolve_encoder()`: `h264_nvenc` quando CUDA disponível, `h264_videotoolbox` em macOS local, `libx264` como padrão — com **uma retomada automática para libx264** se o encoder acelerado falhar;
- saída `yuv420p`, `+faststart`.

## 4. Reuso exato do plano entre rascunho e final

O plano é grande demais para o Firestore (dezenas de milhares de frames). Ele é serializado como JSON gzipado e publicado no bucket de saída em:

```text
{video_id}/chapters/{chapter_id}/autoframe/v10/{fingerprint}.plan.json.gz
```

O **fingerprint** (SHA-256) cobre tudo que determina o plano: caminho e **generation** do objeto de origem, `trim_start`/`trim_end`, dimensões, todos os parâmetros de configuração que afetam a trajetória e os checksums dos modelos YOLO/LR-ASD. O worker responde `camera_plan_gcs_path` + `camera_plan_fingerprint`; o app os grava em `chapter.render_options` e os devolve no render final.

- fingerprint igual → o final **reutiliza o plano do rascunho sem re-analisar** (o vídeo final é pixel-identico em enquadramento ao que foi aprovado);
- limites do capítulo alterados, fonte substituída, configuração ou modelo novo → fingerprint diverge → análise regenerada automaticamente;
- artefato ausente/corrompido → regeneração com log de aviso;
- renders antigos da estratégia v3 (`focal_strategy_version=3`) não alimentam a v10 — e vice-versa: o app só envia `automatic_focal_points` quando a versão gravada é 3.

`AUTOFRAME_ENGINE=legacy` devolve o comportamento v3 completo (inclusive reuso de `automatic_focal_points`) como caminho de rollback.

## 5. Fallbacks

- falha em qualquer estágio v10 (modelo ausente, vídeo ilegível, análise/planejamento quebrado) → **crop central determinístico** pelo renderer existente — nunca o detector antigo em silêncio;
- fonte sem áudio → ASD/VAD operam sobre silêncio e a seleção recai para o maior rosto;
- MediaPipe ausente → fallback visual zerado, LR-ASD decide sozinho;
- encoder acelerado falha → repetição única com `libx264`;
- `multi_mode` sempre `cut` e `no_face_mode` sempre `center` nesta fase.

## 6. Requisitos de runtime

- pacote `vertical_render_function/autoframe_v10/` + `requirements-v10.txt`;
- modelos via **Git LFS**: `models/yolov11n-face.pt` (GPL-3.0, Ultralytics AGPL-3.0) e `third_party/LR-ASD/weight/finetuning_TalkSet.model` (MIT, commit `1b6dcd2`); checksums são validados no build da imagem e em `THIRD_PARTY_NOTICES.md`;
- CPU pura funciona, mas a análise roda YOLO em **todos os frames** — para capítulos longos, `TRANSCODE_USE_GPU=1` (L4 no Cloud Run) é o caminho dimensionado;
- `AUTOFRAME_ENGINE=v10|legacy` escolhe o engine; `AUTOFRAME_DEVICE`/`AUTOFRAME_ENCODER` permitem overrides manuais.

## Limitações atuais

- a associação por IoU não é reconhecimento biométrico — rostos parecidos podem trocar de identidade em reentradas;
- LR-ASD depende de sincronia áudio-vídeo razoável; sem áudio a seleção recai para tamanho do rosto;
- o split automático de diálogo existe no planejador, mas ainda não é exposto (`multi_mode=cut` fixo);
- rostos muito pequenos, de perfil ou ocluídos podem escapar do detector;
- a análise processa cada frame do capítulo — capítulos muito longos em CPU podem aproximar-se do timeout do worker (3600 s).

## Avaliação

`vertical_render_function/tests/test_autoframe_v10.py` cobre fingerprint/artefatos, histerese do falante, modos de câmera, fallback central, composição, fallback de encoder, fonte sem áudio e o roteamento do worker — sem exigir os modelos ML para a maioria dos testes.

## Arquivos relacionados

```text
vertical_render_function/autoframe_v10/{config,models,analyze,plan,render,artifacts}.py
vertical_render_function/third_party/LR-ASD/         (MIT, commit 1b6dcd2)
vertical_render_function/models/yolov11n-face.pt     (Git LFS)
vertical_render_function/models/face_detection_yunet_2023mar.onnx  (legado v3)
vertical_render_function/{main,renderer}.py
vertical_render_function/face_detection.py           (engine legado / rollback)
vertical_render_function/face_detection_v_10/        (protótipo original + README)
vertical_render_function/requirements-v10.txt
vertical_render_function/tests/test_autoframe_v10.py
app/{config,models}.py · app/services/processing.py
```
