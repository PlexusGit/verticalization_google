> **Nota de produção:** este diretório preserva o protótipo original. A implementação integrada ao worker vive em `vertical_render_function/autoframe_v10/` (mesma pipeline, empacotada e testável); `face_detection.py` aqui virou um wrapper fino sobre o pacote. Documentação operacional em `autodetect.md` na raiz do repositório.

## **Arquitetura da v10**

A v10 não processa o vídeo frame a frame com heurísticas online. Ela é dividida em **três passes offline**, cada um salvando um artefato JSON intermediário no `workdir`:

1. `analyze` → `*_analysis.json`  
2. `plan` → `*_plan.json`  
3. `render` → MP4 vertical 9:16

Essa separação permite fazer look-ahead sobre o vídeo inteiro antes de decidir para onde a câmera “olha”, eliminando o efeito de “respiração” do zoom e a troca errada de falante por um frame isolado.

### **1\. Passe de análise (analyze.py)**

* **Vídeo:** aberto com `cv2.VideoCapture`; extraídos `fps`, `width`, `height`, e os frames do trecho definido por `start` e `duration`.  
* **Cenas:** `scenedetect` (ContentDetector) segmenta o vídeo em cortes visuais. Todos os passos seguintes são feitos dentro de cada cena, evitando que tracks se misturem em mudanças de plano.  
* **Áudio e VAD:** áudio resampleado para 16 kHz; energia RMS calculada em janelas de `vad_window=0.15 s`; threshold relativo a 15% do percentil 95 (`vad_rel_thr=0.15`).  
* **Rostos:** `ultralytics` YOLOv11n-face (`yolov11n-face.pt`), entrada `face_imgsz=640`, confiança mínima `face_conf=0.5`, executando em `device=mps` (ou `cuda`/`cpu`).  
* **Tracking:** dentro de cada cena, associação por IoU com `track_iou=0.4`, permitindo gaps de até `track_max_gap=10` frames e descartando tracks menores que `track_min_len=10` frames.  
* **Active Speaker Detection (LR-ASD):** para cada track, recorta a região do rosto (com margem `asd_crop_scale=0.40`) e roda o modelo LR-ASD (`finetuning_TalkSet.model`, 0,84M params) produzindo um `asd_logit`. Se `|logit| < asd_low_conf=1.0`, o score final é mesclado com o fallback visual (FaceMesh).  
* **Fallback visual:** FaceMesh calcula a abertura dos lábios pelos landmarks 13, 14, 61 e 291; histórico de `visual_hist=10` frames; thresholds `visual_var_thr=0.008` e `visual_min_open=0.10`.

O JSON final de análise contém: `fps`, `width`, `height`, `scenes`, `tracks` (com `boxes`, `speak`, `asd_logit`, `visual`), `vad` e `frames`.

### **2\. Passe de planejamento de câmera (plan.py)**

Esse é o diferencial da v10: em vez de seguir o rosto a cada frame, ele decide *planos* com duração mínima e transições bem definidas.

* **Seleção do falante ativo:** a cada frame, olha para o score `speak`; um novo falante só vira alvo se estiver acima de `speak_thr=0.5` por pelo menos `confirm_sec=0.4 s` e o plano atual já tiver durado `min_shot_sec=1.5 s`.  
* **Histerese:** pequenas variações dentro de uma zona morta (`dead_zone_ratio=0.02`) são ignoradas, mantendo o crop fixo.  
* **Zoom:** altura do crop é `face_h * zoom_factor=3.2`, mas nunca menor que `min_crop_h_ratio=0.55` da altura do vídeo (evita pixelação).  
* **Posicionamento:** o centro do rosto fica na altura `headroom=0.42` do crop (regra dos terços adaptativa).  
* **Modos de câmera:**  
  * `static`: centro estável (std \< `static_std_ratio=0.012`); crop congelado.  
  * `pan`: movimento suave de um alvo para outro próximo ao longo de `pan_sec=0.45 s`.  
  * `track`: o falante se desloca dentro do frame e o crop acompanha com suavização gaussiana de `smooth_sigma_sec=0.35`.  
  * `nobody`: sem falante detectado; usa crop central (`no_face_mode=center`) ou fundo borrado.  
* **Corte vs. pan:** se o novo falante estiver a mais de `cut_dist_ratio=0.20` da largura do frame, a troca é um corte seco; caso contrário, é um pan.  
* **Split (diálogo):** quando `multi_mode=split` e dois falantes estiverem ativos por mais de `split_window_sec=6.0`, o output 9:16 é dividido em dois crops empilhados; a ordem segue a posição vertical original, sem trocar quando o falante muda.

O plano final é um array `frames[]` com `crop`, `mode`, `layout` (`cut`/`split`), `track` e `trans`.

### **3\. Passe de renderização (render.py)**

* **Composição:** cada frame original é recortado segundo o plano, redimensionado para 1080×1920 com `cv2.INTER_LANCZOS4`. Modos suportados: crop simples, `split` (top+bottom) e `blur_fit` (fonte 16:9 embutida no 9:16 com blur de fundo).  
* **FFmpeg:** pipeline rawvideo `bgr24` → `h264_videotoolbox` no macOS ou `libx264` na nuvem, `pix_fmt=yuv420p`, bitrate `8M`, `movflags=+faststart`.  
* **Áudio:** muxado do vídeo original em AAC 160 kbps estéreo, com `-shortest` para manter sincronia exata.  
* **Debug (opcional):** gera um vídeo lado a lado mostrando os tracks, scores de fala e o crop. Desabilitado para fontes 1080p no macOS porque o canvas passaria do limite do VideoToolbox.
