# Enquadramento automático (estratégia v2)

Este documento descreve o comportamento atual do algoritmo de enquadramento automático do Plexus Vertical Cuts, desde a análise do vídeo até a geração do recorte vertical pelo FFmpeg. Esta é a **versão 2 da estratégia** (`FOCAL_STRATEGY_VERSION = 2`): além do rastreamento facial, ela adiciona detecção de locutor ativo, previsão de movimento, suavização adaptativa, composição com respiro de cabeça, fallback por saliência para trechos sem rostos e amostragem em duas passadas.

## Objetivo

O enquadramento automático transforma um vídeo de origem em uma composição vertical 9:16, mantendo a pessoa principal dentro da área visível, antecipando movimentos rápidos e evitando oscilações de câmera.

O sistema continua econômico:

- executa no próprio worker de renderização;
- não chama APIs externas de visão computacional;
- usa detecção facial local com YuNet e fluxo óptico do OpenCV;
- concentra a detecção facial (cara) apenas nos trechos com movimento;
- reutiliza resultados entre o rascunho e o render final, com controle de versão;
- mantém o recorte central como fallback seguro.

A versão 2 também cobre dois casos que antes ficavam sem solução:

1. **Quem está falando**: entre vários rostos, a atividade dos lábios escolhe o locutor, com histerese contra trocas impulsivas.
2. **Trechos sem rostos**: um mapa de saliência (movimento + gradientes) mantém o corte no conteúdo relevante em vez de travar no centro.

## Modos de enquadramento

### `center_crop`

É o modo automático padrão (na interface, **Foco automático em rostos**).

O worker analisa o capítulo, escolhe o foco principal e cria uma trajetória temporal. O renderer converte essa trajetória em um recorte vertical dinâmico de 1080 × 1920.

### `split_stack`

Cria duas janelas independentes, uma na metade superior e outra na metade inferior do vídeo vertical. As posições continuam definidas por âncoras manuais (`keyframes_top` e `keyframes_bottom`); a seleção automática de duas pessoas segue como evolução planejada (ver o fim deste documento).

## Fluxo implementado

```text
Vídeo do capítulo
       │
       ▼
Passada 1 — varredura barata (movimento, sem detecção facial)
       │
       ▼
Segmentos ativos → plano de amostragem denso/coeso
       │
       ▼
Passada 2 — detecção facial com YuNet (com landmarks)
       │
       ▼
Seleção do foco (locutor ativo + grupo + composição)
       │
       ▼
Fluxo óptico / tracker corporal durante ausência facial
       │
       ▼
Saliência (movimento + gradientes) quando não há rostos
       │
       ▼
Previsão de velocidade + suavização adaptativa
       │
       ▼
Pontos focais absolutos (keyframes)
       │
       ▼
Compactação para até 48 keyframes
       │
       ▼
Expressões dinâmicas de crop no FFmpeg
       │
       ▼
Vídeo vertical 1080 × 1920
```

## 1. Amostragem em duas passadas

A análise acontece em duas passadas sobre o intervalo do capítulo (`trim_start` a `trim_end`):

**Passada 1 — varredura barata.** A cada 0,75 segundo (máximo de 300 amostras), o worker reduz o frame, calcula a diferença média em escala de cinza em relação à amostra anterior e mede a "energia de movimento". Não há detecção facial aqui — o custo por amostra é mínimo.

**Segmentos ativos.** Amostras com movimento acima de `0,015` (diferença média normalizada 0–1) formam segmentos ativos, expandidos em 1 segundo para cada lado e unidos quando a lacuna entre eles é menor que 1 segundo. Cortes de cena também geram movimento, portanto ficam cobertos.

**Passada 2 — detecção.** O orçamento de 240 frames de detecção é distribuído assim:

- trechos ativos: amostras a cada **0,18 segundo**;
- trechos estáticos: amostras a cada **1,1 segundo**;
- se o total ultrapassa 240, as amostras são afinadas uniformemente.

Na prática, capítulos de notícia passam a maior parte do tempo em plano fixo: a detecção densa fica concentrada onde algo acontece, sem aumentar o custo total.

## 2. Resolução de análise

Frames com largura superior a 640 pixels são reduzidos proporcionalmente antes da detecção. As coordenadas detectadas (rostos e landmarks) são convertidas de volta para a resolução original informada em `video_resolution`, preservando a precisão geométrica do crop.

## 3. Detecção facial e landmarks

O modelo continua sendo o YuNet do OpenCV Zoo:

```text
vertical_render_function/models/face_detection_yunet_2023mar.onnx
```

Configuração: confiança mínima `0,65`, limiar de NMS `0,3`, rostos menores que 18 × 18 pixels ignorados. Licença MIT preservada em `vertical_render_function/models/LICENSE`.

Além da caixa e da confiança, a versão 2 consome os **5 landmarks** do YuNet (olho direito, olho esquerdo, nariz e cantos da boca). Eles alimentam duas funções novas:

- **direção do olhar**: a posição do nariz em relação ao ponto médio dos olhos indica para onde a pessoa vira o rosto;
- **região da boca**: base para medir a atividade labial (detecção do locutor).

## 4. Seleção do foco principal

### Pesos clássicos (sem sinal de fala)

Quando não há medição de atividade labial, a seleção usa os pesos originais:

- área do rosto: 48% do score;
- continuidade em relação ao foco anterior: 25%;
- confiança da detecção: 17%;
- proximidade do centro da imagem: 10%.

### Pesos de locutor (com sinal de fala)

Quando há medição labial, o score passa a privilegiar quem fala:

- atividade labial normalizada: 30%;
- área do rosto: 28%;
- continuidade: 22%;
- confiança: 12%;
- proximidade do centro: 8%.

### Histerese do locutor

O locutor atual só perde o foco quando um desafiante sustenta um score **1,5× maior** por pelo menos **1,5 segundo**. Detecções isoladas ou picos curtos de fala não trocam o enquadramento. Quando o locutor atual desaparece do frame, o melhor candidato assume imediatamente.

### Rostos próximos

Quando dois ou mais rostos cabem juntos dentro de aproximadamente 82% da largura e 72% da altura do crop, o foco é colocado no centro do grupo — uma conversa próxima não é cortada.

## 5. Composição vertical

Três regras substituem o antigo deslocamento fixo de 12%:

1. **Respiro de cabeça**: o centro do rosto é posicionado a **40% da altura do crop** (acima do centro), preservando espaço para ombros e corpo.
2. **Margem de segurança**: o foco é ajustado para que a caixa do rosto mais 15% de margem fique sempre dentro da janela do crop, quando a fonte permite. Isso evita cortar testa ou queixo em posições extremas.
3. **Viés de olhar**: o foco horizontal se inclina até **8% da largura do crop** na direção para onde a pessoa olha (nariz deslocado em relação ao ponto médio dos olhos), antecipando a direção da atenção.

Para fontes 16:9 o crop ocupa toda a altura da fonte; nesse caso o eixo Y fica fixo no centro e apenas o eixo X se move.

## 6. Estabilidade e suavização adaptativa

A versão 2 substitui a suavização fixa por um perfil que reage à velocidade e à confiança:

**Velocidade observada.** A velocidade do alvo é estimada a partir dos **alvos observados** (janela de 1,2 segundo), não do crop estabilizado. Medir o crop já atrasado subestimava a velocidade e mantinha o sistema permanentemente lento.

**Previsão (lead).** O alvo recebe um adiantamento de `velocidade × 0,3 s` (limitado a 25% da largura do crop). A câmera passa a *antecipar* o movimento em vez de persegui-lo.

**Janela de média adaptativa.** Os alvos observados são acumulados em média móvel antes da suavização:

- movimento lento (abaixo de 25% da largura do crop por segundo): janela de **5 segundos** — estabilidade máxima;
- movimento rápido: janela de **1 segundo** — a média longa faria o crop ficar para trás.

**Zona morta adaptativa.** Deslocamentos menores que **12%** da largura do crop (mínimo de 8 pixels) não geram movimento; com o sujeito em movimento rápido, a zona cai para **6%**, permitindo que o crop acompanhe.

**Ganho adaptativo.** Ao superar a zona morta, o algoritmo aplica:

- 34% da diferença em regime normal;
- 55% com o sujeito em movimento rápido;
- 62% em saltos grandes (acima de 90% da largura do crop) ou mudanças de cena;
- com confiança de detecção abaixo de `0,75` (tracker, fluxo ou saliência), o ganho é multiplicado por 0,6 — o crop fica mais pesado quando a referência é menos confiável.

**Emissão de keyframes.** Um novo keyframe é criado quando: é o primeiro do capítulo, ocorre mudança de cena, ou o foco estabilizado se afasta da zona morta correspondente do último keyframe emitido.

## 7. Ausência temporária de rosto

Quando o rosto desaparece, duas camadas mantêm o enquadramento:

**Fluxo óptico (Lucas-Kanade).** Entre amostras consecutivas com até 0,6 segundo de intervalo, os cantos da caixa facial e os landmarks são rastreados com `calcOpticalFlowPyrLK`, com verificação direta e reversa (erro retroativo < 2,5 px) e deslocamento mediano. É rápido, não deriva e cobre piscadas, viradas curtas e borrões.

**Tracker corporal (CSRT, com reserva MIL).** Para lacunas maiores, o último frame facial semeia um tracker sobre a região expandida de cabeça, ombros e torso (limitada a 72% da largura e 78% da altura do frame analisado). O worker usa `TrackerCSRT` quando a build do OpenCV oferece (a imagem de produção usa `opencv-contrib-python-headless`) e recua para `TrackerMIL` caso contrário. Cada amostra rastreada com sucesso atualiza a semente, evitando que um tracker antigo seja reaproveitado.

**Retenção e fallback.** A última posição confirmada é mantida por até **10 segundos**; após isso, o sistema tenta a saliência (próxima seção) e, em último caso, retorna suavemente ao centro.

## 8. Saliência para trechos sem rostos

Quando não há rostos nem rastreamento válido, um mapa de saliência decide onde o corte deve ficar:

- **energia de movimento**: diferença absoluta entre o frame atual e o anterior (peso 0,6);
- **densidade de gradientes**: magnitude Sobel do frame atual (peso 0,4) — captura produtos, telas, textos e cenário relevante mesmo parados.

O foco é o centróide do quartil superior do mapa combinado. Um contraste (pico ÷ média) abaixo de `2,0` rejeita frames sem conteúdo destacável — frames chapados ficam próximos de 1,0 — e o sistema mantém a última posição ou volta ao centro. Isso substitui o antigo comportamento de "sem rosto = crop central fixo".

## 9. Mudanças de cena

Cada amostra gera um histograma de 32 níveis de cinza e uma medida de pixels alterados (diferença em resolução reduzida):

- **corte seco**: correlação de histograma abaixo de `0,25` **ou** mais de 38% dos pixels alterados;
- **fade**: correlação sustentada entre `0,25` e `0,60` por 3 amostras consecutivas.

Em qualquer um dos casos, o estado de rastreamento (locutor, candidato, semente, tracker, fluxo) é descartado para impedir que uma identidade da cena anterior contamine a seguinte. Diferente das versões anteriores, **o foco não salta para o centro**: um keyframe é emitido no limite da cena com o foco anterior, e o novo foco entra suavemente — o easing de 2 segundos do renderer costura a transição entre as cenas.

## 10. Compactação dos keyframes

A análise pode produzir dezenas de posições. Inserir todas no filtro `crop` cria expressões profundamente aninhadas. Antes da renderização, a trajetória é compactada para **no máximo 48 keyframes** (era 24 na versão 1).

O algoritmo remove iterativamente o ponto cuja ausência produz o menor desvio em relação à interpolação entre seus vizinhos, preservando primeiro e último pontos, mudanças grandes, cortes de cena e curvas relevantes. A compactação também é aplicada a keyframes antigos recuperados do Firestore.

## 11. Geração do crop no FFmpeg

Para uma fonte horizontal, o crop usa toda a altura disponível e calcula a largura correspondente à proporção 9:16. Exemplo para uma fonte 640 × 360:

```text
crop: 202 × 360
saída: 1080 × 1920
```

As posições focais viram expressões temporais para X e Y. Cada keyframe contribui com **um único condicional**: o valor permanece no patamar anterior até a janela de transição abrir e então faz o easing cossenoidal até o alvo — um `max(0, min(...))` embute a fase de espera na própria curva. Essa forma compacta permite dobrar o orçamento de keyframes sem estourar o parser do FFmpeg. Os valores são limitados para que o crop nunca saia das dimensões da fonte.

Depois do crop e do redimensionamento, o pipeline aplica legendas ASS, logo, textos de marca, codificação H.264/AAC e `faststart`.

## 12. Persistência e controle de versão

Os pontos focais calculados durante o rascunho são devolvidos pelo worker e armazenados em:

```text
render_options.automatic_focal_points
render_options.focal_strategy_version
```

O render final reutiliza esses pontos **apenas quando a versão da estratégia coincide** (`FOCAL_STRATEGY_VERSION` no worker e em `app/config.py`). Ao evoluir o algoritmo, aumente a versão nos dois lugares: capítulos com pontos antigos são reanalisados no próximo render em vez de reaproveitar trajetórias obsoletas.

## 13. Fallbacks

O recorte central é usado quando:

- o modelo YuNet não está disponível;
- o vídeo não pode ser aberto para análise;
- nenhuma face é encontrada e a saliência não atinge o contraste mínimo;
- a análise lança uma exceção;
- não existem keyframes automáticos válidos.

Uma falha na detecção nunca impede a renderização do capítulo.

## Avaliação

`vertical_render_function/tests/test_focal_quality.py` contém o harness de qualidade: testes unitários das primitivas (fluxo óptico, saliência, composição, locutor, plano de amostragem) e um clip sintético com um sujeito em movimento conhecido, medindo:

- **% do tempo com o sujeito dentro do crop** (atual: 100% no clip de referência);
- **jitter** — maior passo entre keyframes consecutivos;
- **latência de troca** após cortes.

Rode com `PYTHONPATH=. ../.venv/bin/python -m pytest tests/test_focal_quality.py` a partir de `vertical_render_function/`.

## Modo automático com duas pessoas planejado

A evolução recomendada continua sendo um terceiro modo:

```text
auto_split
```

Na interface: **Foco automático em uma pessoa**, **Duas pessoas automáticas** e **Duas âncoras manuais**.

### Comportamento proposto de `auto_split`

1. detectar e manter IDs persistentes para as pessoas visíveis;
2. selecionar as duas tracks mais estáveis e relevantes;
3. atribuir uma identidade ao quadro superior e outra ao inferior, sem inverter quando cruzarem posições;
4. gerar trajetórias independentes para `keyframes_top` e `keyframes_bottom`;
5. rastrear o corpo quando um dos rostos desaparecer;
6. compactar cada trajetória separadamente;
7. usar o renderer `split_stack` existente para compor o resultado.

A prioridade de seleção considera duração da track, área média, confiança acumulada, estabilidade e presença ao longo do capítulo. Com apenas uma pessoa disponível por longos períodos, a política (manter composição dupla com plano geral ou solicitar revisão humana) deve ser explícita.

## Limitações atuais

- o bloqueio de identidade usa proximidade e continuidade, não reconhecimento biométrico;
- a detecção de locutor é feita por movimento labial aparente; cortes rápidos entre falantes ou bocas pouco visíveis podem confundir;
- o tracker corporal (CSRT/MIL) pode derivar em oclusões longas ou movimentos muito rápidos;
- `split_stack` ainda depende de âncoras manuais;
- reflexos, telas e fotografias podem ser reconhecidos como rostos;
- rostos muito pequenos, ocultos ou de perfil podem não ser detectados;
- a saliência não distingue conteúdo relevante de movimento irrelevante (ex.: chuva, folhagem);
- uma falha simultânea do detector, do fluxo e do tracker mantém a última posição por no máximo dez segundos.

## Custos operacionais

A detecção não usa APIs pagas; o custo adicional é o tempo de CPU do worker. Controles:

- varredura da passada 1 sem detecção facial (redimensionamento + diferença de cinza);
- detecção densa apenas em segmentos ativos, com orçamento global de 240 frames;
- análise em largura máxima de 640 pixels;
- fluxo óptico esparso (barato) como primeira camada de rastreamento;
- reutilização dos keyframes no render final, com controle de versão;
- compactação para no máximo 48 keyframes.

## Arquivos relacionados

```text
vertical_render_function/face_detection.py
vertical_render_function/renderer.py
vertical_render_function/main.py
vertical_render_function/models/face_detection_yunet_2023mar.onnx
vertical_render_function/tests/test_face_detection.py
vertical_render_function/tests/test_focal_quality.py
vertical_render_function/tests/test_renderer.py
vertical_render_function/tests/test_ffmpeg_integration.py
app/config.py
app/models.py
app/services/processing.py
app/templates/video_detail.html
```

## Resumo do comportamento atual

```text
Varredura barata localiza segmentos ativos
        ↓
Detecção densa com YuNet apenas onde há movimento
        ↓
Atividade labial escolhe o locutor (histerese de 1,5 s a 1,5×)
        ↓
Grupos próximos viram um único foco
        ↓
Composição: respiro de cabeça a 40%, margem de 15%, viés de olhar
        ↓
Fluxo óptico cobre ausências curtas; CSRT/MIL cobre as longas
        ↓
Saliência (movimento + gradientes) quando não há rostos
        ↓
Velocidade prevê o alvo (+0,3 s) e adapta média, zona morta e ganho
        ↓
Cortes e fades emitem keyframe e costuram a transição
        ↓
Compactar trajetória para até 48 keyframes
        ↓
Interpolar crop no FFmpeg com easing cossenoidal
        ↓
Persistir com versão da estratégia e reutilizar no render final
```
