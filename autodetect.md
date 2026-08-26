# Enquadramento automático

Este documento descreve o comportamento do algoritmo de enquadramento automático do Plexus Vertical Cuts, desde a análise facial até a geração do recorte vertical pelo FFmpeg. Também registra as limitações atuais e o comportamento planejado para rastreamento corporal e composição automática com duas pessoas.

## Objetivo

O enquadramento automático transforma um vídeo de origem em uma composição vertical 9:16, mantendo a pessoa principal dentro da área visível e evitando movimentos bruscos de câmera.

O sistema foi projetado para ser econômico:

- executa no próprio worker de renderização;
- não chama APIs externas de visão computacional;
- não cobra por frame analisado;
- usa detecção facial local com YuNet;
- limita a quantidade de frames analisados;
- reutiliza resultados entre o rascunho e o render final;
- mantém o recorte central como fallback seguro.

O algoritmo detecta e acompanha rostos, mas não afirma identificar o locutor ativo. Identificar quem está falando exigiria sinais adicionais, como atividade labial, diarização ou associação audiovisual.

## Modos de enquadramento

### `center_crop`

É o modo automático padrão.

O worker analisa o capítulo, encontra rostos, escolhe o foco principal e cria uma trajetória temporal. O renderer converte essa trajetória em um recorte vertical dinâmico de 1080 × 1920.

Quando nenhum foco confiável está disponível, o centro do vídeo é usado.

Na interface, este modo aparece como **Foco automático em rostos**.

### `split_stack`

Cria duas janelas independentes, uma na metade superior e outra na metade inferior do vídeo vertical.

Atualmente, as posições são definidas por âncoras manuais e persistidas em `keyframes_top` e `keyframes_bottom`. O sistema ainda não seleciona duas pessoas automaticamente neste modo.

## Fluxo implementado

```text
Vídeo do capítulo
       │
       ▼
Amostragem temporal limitada
       │
       ▼
Redução dos frames para análise
       │
       ▼
Detecção facial com YuNet
       │
       ▼
Seleção do foco principal
       │
       ▼
Suavização temporal
       │
       ▼
Geração de pontos focais absolutos
       │
       ▼
Compactação para até 24 keyframes
       │
       ▼
Expressões dinâmicas de crop no FFmpeg
       │
       ▼
Vídeo vertical 1080 × 1920
```

## 1. Amostragem do vídeo

O worker baixa o vídeo para um diretório temporário e analisa apenas o intervalo do capítulo, entre `trim_start` e `trim_end`.

A análise observa no máximo 240 frames por capítulo. O intervalo é calculado por:

```text
máximo de 0,5 segundo ou duração do capítulo ÷ 240
```

Consequências:

- capítulos curtos são observados aproximadamente duas vezes por segundo;
- capítulos longos continuam limitados a 240 análises;
- o custo de CPU não cresce proporcionalmente ao número total de frames do vídeo;
- o FFmpeg continua responsável pela renderização de todos os frames.

## 2. Resolução de análise

Frames com largura superior a 640 pixels são reduzidos proporcionalmente antes da detecção.

As coordenadas detectadas são convertidas novamente para a resolução original informada em `video_resolution`. Assim, a análise pode ser executada em baixa resolução sem alterar a precisão geométrica do crop final.

## 3. Detecção facial

O sistema usa o modelo YuNet do OpenCV Zoo:

```text
vertical_render_function/models/face_detection_yunet_2023mar.onnx
```

Configuração atual:

- confiança mínima: `0,65`;
- limiar de NMS: `0,3`;
- máximo de 20 detecções por frame;
- rostos menores que 18 × 18 pixels no frame analisado são ignorados.

O modelo é executado localmente em CPU. Sua licença MIT está preservada em:

```text
vertical_render_function/models/LICENSE
```

## 4. Seleção do foco principal

Cada detecção contém:

```text
x, y, largura, altura e confiança
```

Quando vários rostos estão presentes, o sistema considera:

- área do rosto: 48% do score;
- continuidade em relação ao foco anterior: 25%;
- confiança da detecção: 17%;
- proximidade do centro da imagem: 10%.

A continuidade reduz trocas ocasionais, mas o sistema atual ainda não mantém IDs persistentes de pessoas.

### Rostos próximos

Quando dois ou mais rostos cabem juntos dentro de aproximadamente 82% da largura e 72% da altura do crop, o foco é colocado no centro do grupo. Isso evita cortar uma conversa quando as pessoas estão suficientemente próximas.

### Composição vertical

O foco vertical recebe um deslocamento de 12% da altura do crop. Na prática, isso posiciona o rosto acima do centro e preserva mais espaço para ombros e corpo abaixo dele.

## 5. Estabilidade e suavização

O foco não salta diretamente para cada nova detecção. Uma zona morta ignora deslocamentos inferiores a 10% da largura do crop, com mínimo de 8 pixels. Isso evita keyframes provocados por pequenos movimentos da cabeça, oscilações do detector ou mudanças de expressão.

Quando o alvo supera a zona morta, o algoritmo aplica 34% da diferença entre a posição atual e o novo alvo. Para mudanças maiores que 90% da largura do crop ou mudanças de cena, aplica 62%, permitindo uma resposta mais rápida.

Um novo keyframe é criado somente quando:

- é o primeiro foco do capítulo;
- ocorre uma mudança de cena; ou
- o foco estabilizado se afasta pelo menos 10% da largura do crop do último keyframe emitido.

### Bloqueio e histerese

Enquanto a pessoa atual continuar próxima da posição rastreada, ela permanece selecionada mesmo que outro rosto seja maior. Um candidato distante precisa permanecer consistente durante pelo menos 1,5 segundo antes de substituir a pessoa atual. Uma detecção isolada não troca o enquadramento.

## 6. Ausência temporária de rosto

Quando o rosto desaparece, o sistema usa o último frame com detecção para inicializar um `TrackerMIL` sobre uma região expandida de cabeça, ombros e torso. O tracker é criado sob demanda, evitando custo adicional enquanto o rosto permanece visível.

O comportamento é:

1. acompanhar o corpo com `TrackerMIL` quando o rosto desaparece;
2. corrigir a referência quando o rosto volta a ser detectado;
3. manter a última posição por até dez segundos se o tracker falhar;
4. retornar suavemente ao centro após o timeout;
5. reiniciar imediatamente o estado quando uma mudança de cena é detectada.

Assim, um apresentador que vira de costas para mostrar um quadro pode continuar enquadrado por roupa, contorno corporal e textura visual. A inicialização do tracker é limitada a uma região segura do frame; qualquer falha do OpenCV é tratada como fallback e não interrompe o render.

## 7. Compactação dos keyframes

A análise pode produzir mais de 100 posições em um capítulo. Inserir todas elas no filtro `crop` cria expressões `if(...)` profundamente aninhadas e pode ultrapassar o limite prático do parser do FFmpeg.

Antes da renderização, a trajetória é compactada para no máximo 24 keyframes.

O algoritmo remove primeiro o ponto cuja ausência produz o menor erro em relação à interpolação entre seus vizinhos. Dessa forma, são preservados:

- o primeiro e o último ponto;
- mudanças grandes de posição;
- mudanças de cena;
- curvas relevantes da trajetória;
- transições entre pessoas ou lados do quadro.

A compactação também é aplicada a keyframes antigos recuperados do Firestore.

## 8. Geração do crop no FFmpeg

Para uma fonte horizontal, o crop usa toda a altura disponível e calcula a largura correspondente à proporção 9:16.

Exemplo para uma fonte 640 × 360:

```text
crop: 202 × 360
saída: 1080 × 1920
```

As posições focais são convertidas em expressões temporais para X e Y. A interpolação usa easing cossenoidal, e os valores são limitados para que o crop nunca saia das dimensões da fonte.

Depois do crop e do redimensionamento, o pipeline aplica:

1. legendas ASS;
2. logo;
3. textos de marca;
4. codificação H.264/AAC;
5. otimização `faststart` para reprodução web.

## 9. Persistência e reutilização

Os pontos focais calculados durante o rascunho são devolvidos pelo worker e armazenados em:

```text
render_options.automatic_focal_points
```

O render final reutiliza esses pontos. Isso evita executar novamente a análise facial do mesmo capítulo e mantém o enquadramento consistente entre rascunho e versão final.

Se pontos antigos contiverem mais de 24 entradas, eles são compactados antes do uso.

## 10. Fallbacks

O recorte central é usado quando:

- o modelo YuNet não está disponível;
- o vídeo não pode ser aberto para análise;
- nenhuma face é encontrada;
- a análise facial lança uma exceção;
- não existem keyframes automáticos válidos.

Uma falha na detecção não deve impedir a renderização do capítulo.

## Rastreamento corporal implementado

O rastreamento combina YuNet e `TrackerMIL` sem dependência ou modelo adicional. YuNet continua sendo a fonte principal; o MIL é usado apenas durante ausência facial.

A caixa corporal deriva da face, é expandida para cabeça, ombros e torso e depois limitada para ocupar no máximo 72% da largura e 78% da altura do frame analisado. Essa margem é necessária para que o MIL encontre amostras positivas e negativas ao redor da pessoa.

O último frame facial é mantido como semente. No primeiro frame sem rosto, o worker inicializa o tracker nessa semente e atualiza a caixa no frame atual. Quando o rosto reaparece, o tracker é descartado e uma nova semente corrigida é armazenada.

### Mudança de cena

Cada frame analisado gera um histograma de 32 níveis de cinza. Uma correlação inferior a `0,25` é tratada como mudança de cena. Nesse caso, foco anterior, candidato pendente, semente corporal e tracker são descartados para impedir que uma identidade da cena anterior seja carregada para a seguinte.

## Modo automático com duas pessoas planejado

A evolução recomendada é adicionar um terceiro modo:

```text
auto_split
```

Na interface, os modos seriam:

1. **Foco automático em uma pessoa**;
2. **Duas pessoas automáticas**;
3. **Duas âncoras manuais**.

### Comportamento proposto de `auto_split`

1. detectar e manter IDs persistentes para as pessoas visíveis;
2. selecionar as duas tracks mais estáveis e relevantes;
3. atribuir uma identidade ao quadro superior;
4. atribuir a outra identidade ao quadro inferior;
5. nunca inverter os quadros apenas porque as pessoas cruzaram suas posições;
6. gerar trajetórias independentes para `keyframes_top` e `keyframes_bottom`;
7. rastrear o corpo quando um dos rostos desaparecer;
8. manter a última posição durante ausências temporárias;
9. compactar cada trajetória separadamente;
10. usar o renderer `split_stack` existente para compor o resultado.

### Seleção das duas pessoas

A prioridade deve considerar:

- duração da track;
- área média da pessoa;
- confiança acumulada;
- estabilidade da posição;
- presença durante a maior parte do capítulo.

Não é necessário identificar qual pessoa está falando, pois ambas permanecem visíveis. Se houver mais de duas pessoas, o sistema deve manter as duas tracks mais persistentes e evitar alternâncias rápidas.

### Apenas uma pessoa disponível

Quando uma das pessoas desaparece temporariamente:

- manter seu último enquadramento por alguns segundos;
- continuar o rastreamento corporal;
- não trocar imediatamente por alguém ao fundo.

Se somente uma pessoa estiver disponível durante grande parte do capítulo, o sistema pode:

- continuar com a composição dupla usando um plano geral no segundo quadro; ou
- solicitar revisão humana antes do render final.

A política deve ser explícita para evitar mudanças automáticas inesperadas de layout.

## Limitações atuais

- o bloqueio de identidade usa proximidade e continuidade, não reconhecimento biométrico;
- o `TrackerMIL` pode derivar para o fundo em oclusões longas ou movimentos muito rápidos;
- não há identificação de locutor ativo;
- `split_stack` ainda depende de âncoras manuais;
- reflexos, telas e fotografias podem ser reconhecidos como rostos;
- rostos muito pequenos, ocultos ou de perfil podem não ser detectados;
- mudanças graduais de cena podem não ultrapassar o limiar do histograma;
- uma falha simultânea do detector e do tracker mantém a última posição por no máximo dez segundos.

## Custos operacionais

A detecção não usa APIs pagas. O custo adicional é apenas o tempo de CPU do worker Cloud Run.

Os principais controles de custo são:

- análise em largura máxima de 640 pixels;
- máximo de 240 frames analisados por capítulo;
- modelo YuNet pequeno;
- concorrência do worker limitada;
- reutilização dos keyframes no render final;
- compactação para no máximo 24 keyframes;
- fallback sem nova tentativa externa.

## Arquivos relacionados

```text
vertical_render_function/face_detection.py
vertical_render_function/renderer.py
vertical_render_function/main.py
vertical_render_function/models/face_detection_yunet_2023mar.onnx
vertical_render_function/tests/test_face_detection.py
vertical_render_function/tests/test_renderer.py
vertical_render_function/tests/test_ffmpeg_integration.py
app/models.py
app/services/processing.py
app/templates/video_detail.html
```

## Resumo do comportamento atual

```text
Detectar rostos localmente com YuNet
        ↓
Bloquear a pessoa por proximidade e continuidade
        ↓
Ignorar movimentos menores que 10% do crop
        ↓
Exigir 1,5 segundo para trocar de candidato
        ↓
Rastrear cabeça, ombros e torso quando o rosto desaparece
        ↓
Manter o último foco por até dez segundos se o MIL falhar
        ↓
Reiniciar o estado em mudanças de cena
        ↓
Compactar trajetória para até 24 keyframes
        ↓
Interpolar crop no FFmpeg
        ↓
Persistir e reutilizar no render final
```

## Resumo da evolução recomendada

```text
Rastreamento atual
  + reidentificação visual mais robusta
  + opção auto_split
  + duas tracks independentes
        ↓
Composição automática superior/inferior
```
