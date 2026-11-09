# Plexus Vertical Cuts

Plexus Vertical Cuts é um projeto open source para transformar reportagens e programas jornalísticos horizontais (16:9) em vídeos verticais (9:16), com sugestões de capítulos por IA e revisão humana antes da exportação.

A interface está em português brasileiro, é responsiva e usa somente a marca Plexus no chrome da aplicação. O código é licenciado sob Apache License 2.0.

## Escopo da primeira versão

- Upload direto pelo navegador ou API, inclusive arquivos grandes via upload resumível para GCS.
- Normalização H.264/AAC em Cloud Run, com NVIDIA L4/NVENC opcional e fallback para CPU.
- Transcrição `pt-BR` com Gemini Transcribe (Chirp 3 como fallback/alternativa) ou SRT fornecido pelo usuário.
- Sugestão de capítulos e ganchos com saída estruturada do Gemini.
- Rascunho vertical automático com pipeline autoframe v10: detecção facial YOLO local, locutor ativo por áudio-vídeo (LR-ASD) e câmera virtual planejada em três passes.
- Editor de capítulos, limites, legendas estáticas, logo e texto de marca.
- Templates reutilizáveis de logo e título, selecionáveis por capítulo antes da aprovação.
- Reenquadramento manual no modelo RPP: duas áreas independentes, empilhadas, com quadros-chave.
- Exportação final somente após aprovação humana.
- Login Google com allowlist de e-mails no Firestore e API key para automação.
- Funções Pub/Sub opcionais para importar objetos de GCS e S3.

> **Sobre active speaker tracking:** o enquadramento automático (estratégia v10, `autodetect.md`) identifica o locutor ativo com o modelo áudio-visual LR-ASD — MFCC + crops faciais — com histerese de 0,4 s e plano mínimo de 1,5 s antes de trocar. O plano de câmera do rascunho é persistido no GCS e reutilizado exato no render final.

## Arquitetura

```text
 Navegador / API
       │
       ├── upload resumível ───────────────────────────────┐
       │                                                    ▼
       ▼                                           Cloud Storage
 FastAPI (Cloud Run) ─── Firestore                         │
       │                                                    │
       ├── Cloud Run Job (pipeline: process / finais) ──────┤
       │       └── executa o mesmo código do app            │
       ├── Transcode worker (Cloud Run CPU/GPU) ◄───────────┤
       │       └── MP4 H.264 + MP3 16 kHz                   │
       ├── Gemini Transcribe pt-BR                          │
       ├── Gemini: capítulos                                │
       └── Vertical render worker (Cloud Run CPU) ◄─────────┘
               └── rascunhos e finais 1080 × 1920

 Pub/Sub ── process_gcs_bucket ──┐
 Pub/Sub ── process_S3_bucket  ──┴── POST /api/import/gcs
```

### Componentes implantáveis

| Componente | Diretório | Função |
|---|---|---|
| Aplicação | raiz + `app/` | UI, API, autenticação e orquestração |
| Job de pipeline | `app/jobs/pipeline.py`, `app/services/dispatch.py` | Cloud Run Job que executa `process_video` e o lote de finais fora da instância web (criado por `./deploy.sh`) |
| Transcodificador | `transcode_function/` | MP4 normalizado (ou remux sem re-encode quando a fonte já é compatível) e áudio MP3 |
| Renderizador | `vertical_render_function/` | recorte, legendas e sobreposições |
| Importador GCS | `watch-folder-functions/process_gcs_bucket/` (guia: [watch-folder-functions/README.md](watch-folder-functions/README.md)) | evento Pub/Sub de `OBJECT_FINALIZE` |
| Importador S3 | `watch-folder-functions/process_S3_bucket/` | evento S3 encaminhado ao Pub/Sub |

Não existe servidor de livestream neste projeto.

## Documentação técnica interativa

[`docs/index.html`](docs/index.html) explica passo a passo como o vídeo é processado (entrada, transcode, transcrição com Gemini, sugestão de capítulos, rascunhos, autoframe v10, aprovação, corte horizontal) com links para as linhas exatas no GitHub. Abra o arquivo no navegador ou, para ler os trechos de código dentro da própria página, rode `python3 -m http.server` na raiz do repositório e acesse `http://localhost:8000/docs/`. O teste `tests/test_docs_links.py` valida cada link contra o commit fixado na página.

## Fluxo e estados

```text
UPLOADING
  → TRANSCODING
  → TRANSCRIBING
  → SUGGESTING
  → RENDERING_DRAFTS
  → PENDING_REVIEW
  → RENDERING_FINALS
  → COMPLETED
```

Qualquer etapa pode terminar em `ERROR`. O botão/API de retry reutiliza artefatos já persistidos sempre que possível e limpa o erro anterior; a UI só exibe o painel de erro quando o status é `ERROR`.

Um vídeo cujo pipeline foi interrompido (sem `heartbeat_at` há mais de 150 s) é retomado automaticamente quando alguém o abre ou lista, sem repetir transcode, transcrição, sugestão de capítulos nem renders já concluídos.

Cada capítulo tem rascunho e render final separados. Alterar legendas queimadas, marca ou enquadramento exige novo render.

## Estrutura de dados

### Firestore

```text
vertical_cuts_videos/{video_id}
  id, title, description, status, owner_email
  source_type, source_id, source_filename
  gcs_source_path, gcs_high_path, gcs_audio_chunks
  subtitle_text, subtitle_source, gcs_srt_path
  duration_seconds, video_resolution, error, error_type
  heartbeat_at, lease_owner, resume_count
  created_at, updated_at, completed_at

vertical_cuts_videos/{video_id}/events/{event_id}
vertical_cuts_videos/{video_id}/renders/{render_id}
  status, result (gravado pelo worker; usado para adotar renders após uma interrupção)

vertical_cuts_videos/{video_id}/chapters/{chapter_id}
  title, reason, start_seconds, end_seconds
  subtitle_text, selected, render_options
  draft_status, draft_gcs_path, draft_render_id
  final_status, final_gcs_path, final_render_id

vertical_cuts_videos/{video_id}/chapters/{chapter_id}/renders/{render_id}
  kind, status, target_gcs_path, gcs_path, options, error

vertical_cuts_users/{google_sub}
vertical_cuts_allowed_emails/{email_normalizado}
```

### Cloud Storage

```text
{video_id}/{video_id}_source.ext
{video_id}/{video_id}_high.mp4
{video_id}/{video_id}.srt
{video_id}/audio/chunk_000.mp3
{video_id}/branding/{chapter_id}.png
{video_id}/chapters/{chapter_id}/{render_id}.mp4
imports/s3/{bucket}/{hash}/{filename}
```

## Pré-requisitos

Para desenvolvimento local:

- Python 3.12
- FFmpeg/ffprobe
- Google Cloud CLI autenticado
- Docker para testar imagens
- um projeto GCP **já criado e com faturamento ativo**

Serviços usados no GCP:

- Cloud Run e Cloud Functions Gen2
- Artifact Registry e Cloud Build
- Cloud Storage
- Firestore Native
- Gemini Transcribe e Speech-to-Text v2 (fallback Chirp 3)
- Pub/Sub e Eventarc
- Secret Manager

O script não cria projeto, não vincula billing, não solicita quota de GPU, não cria OAuth Client e não cria chave Gemini.

## Configuração única

Consulte o [guia completo de parâmetros do `.env`](ENV_GUIDE.md), incluindo a lista obrigatória antes de `setup_gcp.sh` e os valores gerados automaticamente no deploy.

Todos os componentes leem somente `.env` na raiz.

```bash
cp .env.example .env
```

Preencha pelo menos:

| Variável | Descrição |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | ID do projeto existente |
| `DEPLOY_REGION` | região principal de Cloud Run/Artifact Registry |
| `BUCKET_NAME` | bucket gerenciado de vídeos, globalmente único |
| `SERVICE_ACCOUNT_EMAIL` | conta de execução criada pelo setup |
| `GOOGLE_CLIENT_ID` | OAuth Web Client para Google Identity Services |
| `INITIAL_ALLOWED_EMAILS` | administradores iniciais, separados por vírgula |
| `SECRET_KEY` | segredo longo para assinar sessões |
| `SECURITY_TOKEN` | token longo para app, workers e importadores |
| `GEMINI_API_KEY` | chave da Gemini API |
| `TRANSCODE_USE_GPU` | `1` para L4 ou `0` para CPU (transcode **e** render vertical) |
| `AUTOFRAME_ENGINE` | `legacy` (padrão de rollout) ou `v10` para o enquadramento automático |

Gere segredos locais, por exemplo:

```bash
openssl rand -hex 32
openssl rand -hex 48
```

Não versionar `.env`, JSON de service account, chaves AWS ou tokens. Em Cloud Run/Functions, os deploys usam Secret Manager e identidade de workload; JSON de credencial serve somente como fallback local.

## Preparar um projeto GCP

Depois de revisar `.env`, valide sem criar recursos e então execute:

```bash
./setup_gcp.sh --check
./setup_gcp.sh
```

O script é idempotente e:

1. valida autenticação, acesso ao projeto e billing;
2. habilita APIs;
3. cria conta de serviço e IAM (inclui `roles/run.developer` e `roles/iam.serviceAccountUser` sobre ela mesma, necessários para o app iniciar o job de pipeline);
4. cria Firestore Native, Artifact Registry e buckets;
5. cria tópicos Pub/Sub e notificação GCS opcional;
6. envia segredos para Secret Manager;
7. configura CORS do bucket;
8. adiciona `INITIAL_ALLOWED_EMAILS` à allowlist.

### Etapas manuais

1. Configure a tela de consentimento OAuth.
2. Crie um OAuth Client do tipo **Web application**.
3. Adicione `http://localhost:8080` e a URL final do Cloud Run às origens JavaScript autorizadas.
4. Crie a chave Gemini e coloque-a em `.env` antes de executar novamente o setup.
5. Se usar GPU, confirme que a quota `run.googleapis.com/nvidia_l4_gpu_allocation_no_zonal_redundancy` (console: "Total Nvidia L4 GPU allocation without zonal redundancy") foi **concedida** na região. Um pedido negado mantém a quota efetiva em 0 e o deploy falha com `Quota exceeded for total allowable count of GPUs per project per region`; confira o estado com `gcloud beta quotas preferences list --project=<projeto>` e use `TRANSCODE_USE_GPU=0` enquanto isso.
6. Se usar S3, configure um publisher/bridge AWS que publique o evento no tópico Google Pub/Sub. S3 não publica diretamente em Google Pub/Sub.

## Implantação

### Tudo de uma vez

```bash
./deploy_all.sh
```

A ordem é transcode → render → app → importadores. O script captura URLs, atualiza apenas as chaves geradas em `.env`, redeploya o app com o domínio final e atualiza CORS.

O importador GCS só é implantado quando `GCS_WATCH_BUCKET` está definido. O S3 só é implantado quando a configuração S3 está completa.

### Componentes individuais

```bash
./transcode_function/deploy.sh
./vertical_render_function/deploy.sh
./deploy.sh
./watch-folder-functions/process_gcs_bucket/deploy.sh
./watch-folder-functions/process_S3_bucket/deploy.sh
```

`./deploy.sh` também cria/atualiza o job `plexus-vertical-cuts-pipeline` (mesma imagem do app, comando `python -m app.jobs.pipeline`) antes de implantar o serviço, que referencia o job por `PIPELINE_JOB_NAME`.

Os workers permitem invocação pública no nível Cloud Run porque usam `X-API-Key` secreto e falham fechados quando o token não está configurado. O app também é público para permitir Google Identity Services, mas suas rotas de dados exigem sessão ou API key.

## Desenvolvimento local

Prepare uma única virtualenv na raiz; ela contém as dependências do app, workers e funções:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt

gcloud auth application-default login
./test.sh
```

### Executar tudo

```bash
./run_all_local.sh
```

O orquestrador verifica conflitos de porta, injeta as URLs locais dos workers no app e encerra somente os processos que iniciou quando você pressiona `Ctrl+C`. Os handlers Functions Framework usam um servidor Werkzeug local, single-process e sem reloader, evitando o crash de `fork()` das bibliotecas Objective-C no macOS; os containers de produção continuam usando seus servidores normais.

| Componente | URL local |
|---|---|
| App/UI | `http://127.0.0.1:8080` |
| OpenAPI | `http://127.0.0.1:8080/docs` |
| Transcode | `http://127.0.0.1:8081` |
| Vertical render | `http://127.0.0.1:8082/health` |
| Importador GCS | `http://127.0.0.1:8083` |
| Importador S3 | `http://127.0.0.1:8084` |

O transcode local usa CPU por padrão. Use `LOCAL_USE_NVENC=1` somente em um host NVIDIA com suporte `h264_nvenc` no FFmpeg.

Os importadores iniciam mesmo com buckets opcionais vazios, mas ignoram/rejeitam eventos até `GCS_WATCH_BUCKET` ou `S3_WATCH_BUCKET` serem configurados.

### Executar componentes separadamente

Em terminais diferentes:

```bash
./transcode_function/run_local.sh
./vertical_render_function/run_local.sh
./run_local.sh
./watch-folder-functions/process_gcs_bucket/run_local.sh
./watch-folder-functions/process_S3_bucket/run_local.sh
```

As portas podem ser alteradas com `APP_LOCAL_PORT`, `TRANSCODE_LOCAL_PORT`, `VERTICAL_RENDER_LOCAL_PORT`, `GCS_IMPORT_LOCAL_PORT` e `S3_IMPORT_LOCAL_PORT`. Ao executar o app separadamente, ele aponta automaticamente para as portas locais configuradas.

## Testes

```bash
./test.sh
./transcode_function/test.sh
./vertical_render_function/test.sh
./watch-folder-functions/process_gcs_bucket/test.sh
./watch-folder-functions/process_S3_bucket/test.sh
./test_all.sh
```

Os testes cobrem modelos, autenticação, API pública, SRT, payloads Pub/Sub, idempotência, validação dos workers, filtros FFmpeg, interpolação do editor e tratamento de falhas. O renderizador inclui testes de construção segura de argumentos sem shell.

## API

O navegador usa cookie de sessão. Clientes headless enviam:

```http
X-API-Key: valor-de-SECURITY_TOKEN
```

Principais endpoints:

| Método | Endpoint | Uso |
|---|---|---|
| `POST` | `/api/upload/init` | criar upload resumível |
| `POST` | `/api/upload/finalize` | verificar objeto e processar |
| `POST` | `/api/videos` | upload multipart alternativo |
| `POST` | `/api/import/gcs` | importação idempotente pelas funções |
| `GET` | `/api/videos` | listar vídeos |
| `GET` | `/api/video/{id}` | vídeo, capítulos e previews |
| `GET` | `/api/video/{id}/source-url` | preview assinado da fonte |
| `GET` | `/api/video/{id}/subtitles.srt` | baixar transcrição |
| `POST` | `/api/video/{id}/retry` | retomar pipeline |
| `POST` | `/api/video/{id}/regenerate-chapters` | pedir novos capítulos ao Gemini |
| `POST` | `/api/video/{id}/chapters` | adicionar capítulo |
| `PUT` | `/api/video/{id}/chapters/{chapter}` | salvar revisão |
| `DELETE` | `/api/video/{id}/chapters/{chapter}` | excluir capítulo |
| `POST` | `/api/video/{id}/chapters/{chapter}/logo` | upload de marca |
| `POST` | `/api/video/{id}/chapters/{chapter}/render` | enfileirar novo render |
| `POST` | `/api/video/{id}/approve` | gerar finais selecionados |
| `DELETE` | `/api/video/{id}` | excluir vídeo e artefatos |
| `GET` | `/health` | health check público |

A documentação OpenAPI fica em `/docs`. Para o passo a passo de uso apenas pela API (envio, revisão, aprovação e download), veja [API_GUIDE.md](API_GUIDE.md).

## Contratos Pub/Sub

### GCS

Use uma notificação `OBJECT_FINALIZE` com payload JSON padrão do Cloud Storage. A função ignora outro bucket, outro evento e extensões não suportadas. `bucket + name + generation` forma a identidade imutável.

### S3

A mensagem Pub/Sub decodificada pode ser o evento AWS padrão:

```json
{"Records":[{"eventName":"ObjectCreated:Put","s3":{"bucket":{"name":"news"},"object":{"key":"folder%2Fvideo.mp4","eTag":"abc"}}}]}
```

ou o formato canônico:

```json
{"bucket":"news","key":"folder/video.mp4","etag":"abc","event_name":"ObjectCreated"}
```

A função baixa para arquivo temporário, envia ao GCS usando precondição de geração zero e chama a API com uma chave idempotente. Falhas transitórias são relançadas para retry do Pub/Sub.

## Renderização

### `center_crop`

Detecta rostos localmente com YuNet, seleciona o foco principal com continuidade temporal e gera um recorte 1080 × 1920 animado por keyframes. Se nenhum rosto for encontrado ou a análise falhar, usa o centro da imagem.

### `split_stack`

Duas janelas de recorte são animadas de forma independente e empilhadas em 9:16. Quadros-chave `x/y/time` usam interpolação cossenoidal e são limitados às dimensões da fonte.

### Legendas e marca

- SRT editável por capítulo.
- Queima estática via ASS/libass.
- Fonte, tamanho, cor, contorno e posição básica.
- Logo PNG/JPG/WebP com tamanho, opacidade e posição.
- Texto com tamanho, cor e posição; os templates podem ativar uma caixa de leitura atrás do título (cor e opacidade; padrão sem caixa).

Entradas são validadas, nomes são saneados e FFmpeg é executado com array de argumentos, nunca por shell.

## Segurança e operação

- Allowlist e tokens são obrigatórios; workers falham fechados.
- Não use wildcard CORS em produção; os scripts configuram localhost e o domínio implantado.
- Rotacione tokens executando novamente `setup_gcp.sh` e redeploy.
- Configure orçamento, alertas e limites de instâncias.
- Cloud Run GPU depende de disponibilidade/quota regional; CPU é funcional, porém mais lenta.
- Os pipelines longos (processamento e lote de finais) rodam em um **Cloud Run Job** (`PIPELINE_JOB_NAME`, criado por `./deploy.sh`), que não sofre o desligamento por ociosidade nem os deploys do serviço web. Se o job não estiver configurado (execução local) ou não puder ser iniciado, o mesmo código roda dentro do serviço. Previews de um único capítulo continuam em segundo plano no serviço.
- Um heartbeat (`heartbeat_at`, a cada 30 s) permite detectar execuções interrompidas: ao abrir ou listar um vídeo sem heartbeat há mais de 150 s, o app o retoma de forma atômica (no máximo 3 vezes seguidas; depois `ERROR`), reaproveitando capítulos, transcrição e renders que os workers já concluíram.
- Os capítulos renderizam em paralelo (`RENDER_PARALLELISM`, padrão 5 — uma instância por pedido no serviço de render). Se a conexão com o worker cai, o app adota o resultado que ele registra no Firestore em vez de reenviar cegamente; o worker marca `heartbeat_at` no render e um heartbeat parado há mais de 300 s indica render perdido (reenviado automaticamente).
- O transcode escala até `TRANSCODE_MAX_INSTANCES` (padrão 5 em CPU, 1 com GPU) e o app repete o pedido por até `TRANSCODE_QUEUE_WAIT_SECONDS` quando o Cloud Run informa que não há instância livre.
- Revise permissões IAM para políticas organizacionais específicas antes de produção.

## Foco automático

> O motor padrão é o **v10** (YOLO-face, LR-ASD e plano de câmera persistido), descrito em [`autodetect.md`](autodetect.md). O texto abaixo documenta o motor legado (`AUTOFRAME_ENGINE=legacy`).

No motor legado o render final reutiliza os focos (`automatic_focal_points`) do rascunho em vez de repetir a detecção, mas só enquanto os limites do capítulo continuam os mesmos (`focal_points_bounds`); ao editar o início ou o fim, a detecção é refeita.

O motor legado amostra no máximo 240 frames de cada capítulo e executa o YuNet localmente, sem API de visão ou cobrança por imagem. Uma média móvel de três segundos estabiliza a posição da pessoa rastreada, uma zona morta de 20% ignora movimentos pequenos da cabeça e a histerese de 1,5 segundo evita trocas ocasionais de pessoa. Quando o rosto desaparece, o `TrackerMIL` acompanha uma região expandida de cabeça, ombros e torso, com retenção de até dez segundos se o tracker falhar e reinicialização em mudanças de cena. Os focos `(x, y)` são compactados para até 24 keyframes, persistidos no rascunho e reutilizados no render final.

A implementação fica isolada em `vertical_render_function/face_detection.py`. O modelo YuNet incluído em `vertical_render_function/models/` vem do OpenCV Zoo e mantém sua licença MIT no mesmo diretório. O renderer continua responsável apenas pela interpolação e composição. Uma evolução futura pode combinar atividade labial ou diarização sem trocar o contrato de keyframes.

## Licença

Copyright 2026 Plexus.

Licenciado sob a [Apache License 2.0](LICENSE).
