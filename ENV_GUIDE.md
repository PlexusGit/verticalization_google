# Guia de configuração do `.env`

Este guia explica todas as variáveis do arquivo `.env` do Plexus Vertical Cuts, quais precisam ser preenchidas antes de `setup_gcp.sh`, quais são necessárias somente no deploy e quais podem permanecer vazias.

O arquivo `.env` contém segredos e está ignorado pelo Git. Nunca o publique, envie por e-mail ou copie para imagens Docker.

Alguns scripts carregam o arquivo com `source`. Portanto, coloque entre aspas valores com espaços ou caracteres especiais, por exemplo:

```dotenv
FIRESTORE_DB_NAME="(default)"
DEFAULT_CAPTION_FONT="DejaVu Sans"
```

Não adicione espaços ao redor de `=`.

## 1. Checklist obrigatório antes de `setup_gcp.sh`

O script interrompe imediatamente se qualquer variável desta tabela estiver vazia.

| Variável | Obrigatória | Como preencher |
|---|---:|---|
| `GOOGLE_CLOUD_PROJECT` | Sim | ID de um projeto GCP **já existente**, com billing ativo. Não é o nome de exibição nem o número do projeto. |
| `DEPLOY_REGION` | Sim | Região principal, por exemplo `us-central1`. |
| `BUCKET_NAME` | Sim | Nome de bucket globalmente único, em minúsculas, sem `gs://`, por exemplo `minha-editora-vertical-cuts-prod`. |
| `ARTIFACT_REGISTRY_REPOSITORY` | Sim | Nome do repositório Docker que o setup criará, por exemplo `plexus-vertical-cuts`. |
| `SERVICE_ACCOUNT_EMAIL` | Sim | Deve usar exatamente o ID do projeto: `<SERVICE_ACCOUNT_NAME>@<GOOGLE_CLOUD_PROJECT>.iam.gserviceaccount.com`. |
| `SECURITY_TOKEN` | Sim | Token aleatório compartilhado entre app, workers e importadores. Gere com `openssl rand -hex 32`. Não use o valor de exemplo. |
| `SECRET_KEY` | Sim | Chave aleatória para assinar cookies de sessão. Gere com `openssl rand -hex 48`. **Não é o OAuth Client Secret.** |
| `GEMINI_API_KEY` | Sim | Chave da Gemini API criada para o projeto/conta que será usado. Não pode permanecer vazia. |

Embora o script não as trate como obrigatórias, estas variáveis também devem ser decididas antes da primeira execução:

| Variável | Motivo |
|---|---|
| `FIRESTORE_LOCATION` | A localização do banco Firestore não pode ser alterada depois da criação. `nam5` é multirregional nos EUA; escolha de acordo com residência de dados e latência. |
| `FIRESTORE_DB_NAME` | Mantenha `(default)` salvo se não houver necessidade de banco nomeado. |
| `SERVICE_ACCOUNT_NAME` | Deve ser a parte anterior a `@` em `SERVICE_ACCOUNT_EMAIL`. |
| `INITIAL_ALLOWED_EMAILS` | Sem pelo menos um e-mail válido, ninguém conseguirá entrar na interface após o deploy. Use uma lista separada por vírgulas. |
| `SECRET_KEY_SECRET`, `SECURITY_TOKEN_SECRET`, `GEMINI_API_KEY_SECRET` | Nomes dos segredos que serão criados no Secret Manager e usados pelos scripts de deploy. Os padrões são adequados. |

### Exemplo mínimo antes do setup

Substitua os valores entre `<...>`; não copie os exemplos de segredo literalmente.

```dotenv
GOOGLE_CLOUD_PROJECT=<id-do-projeto-existente>
DEPLOY_REGION=us-central1
FIRESTORE_LOCATION=nam5
FIRESTORE_DB_NAME="(default)"

ARTIFACT_REGISTRY_REPOSITORY=plexus-vertical-cuts
SERVICE_ACCOUNT_NAME=plexus-vertical-cuts
SERVICE_ACCOUNT_EMAIL=plexus-vertical-cuts@<id-do-projeto-existente>.iam.gserviceaccount.com

BUCKET_NAME=<nome-globalmente-unico-do-bucket>
INITIAL_ALLOWED_EMAILS=<seu-email>@example.com

SECRET_KEY=<saida-de-openssl-rand-hex-48>
SECURITY_TOKEN=<saida-de-openssl-rand-hex-32>
GEMINI_API_KEY=<chave-real-da-gemini-api>

SECRET_KEY_SECRET=plexus-vertical-cuts-session-key
SECURITY_TOKEN_SECRET=plexus-vertical-cuts-api-token
API_TOKEN_SECRET=plexus-vertical-cuts-api-token
GEMINI_API_KEY_SECRET=plexus-vertical-cuts-gemini-key
```

Gere os dois valores aleatórios:

```bash
openssl rand -hex 48   # copiar para SECRET_KEY
openssl rand -hex 32   # copiar para SECURITY_TOKEN
```

Valide o projeto e as variáveis sem criar recursos:

```bash
./setup_gcp.sh --check
```

Depois execute o provisionamento:

```bash
./setup_gcp.sh
```

## 2. Projeto e localizações

### `GOOGLE_CLOUD_PROJECT`

- **Fase:** obrigatória antes do setup.
- ID imutável do projeto GCP existente.
- O script não cria projeto e não vincula billing.
- Confirme com:

```bash
gcloud projects describe "$GOOGLE_CLOUD_PROJECT"
gcloud billing projects describe "$GOOGLE_CLOUD_PROJECT"
```

### `LOCATION`

- **Fase:** opcional/avançada.
- Localização lógica padrão da aplicação; atualmente o fluxo de transcrição usa `CHIRP_LOCATION` de forma explícita.
- O padrão `us` pode ser mantido.

### `DEPLOY_REGION`

- **Fase:** obrigatória antes do setup.
- Região da aplicação principal, Artifact Registry, bucket padrão e funções quando não houver override.
- Exemplo: `us-central1`.

### `TRANSCODE_REGION`

- **Fase:** necessária antes do deploy do transcodificador.
- Região do worker CPU/GPU.
- Mantenha igual a `DEPLOY_REGION` para CPU. Para GPU, use uma região com NVIDIA L4 disponível e quota aprovada.

### `FIRESTORE_LOCATION`

- **Fase:** decidir antes do primeiro setup.
- Localização usada somente ao criar o banco.
- Não pode ser migrada por este script depois da criação.

### `ARTIFACT_REGISTRY_REPOSITORY`

- **Fase:** obrigatória antes do setup e deploy.
- Nome do repositório Docker regional.
- O setup cria o repositório quando ele ainda não existe.

### `SERVICE_ACCOUNT_NAME`

- **Fase:** recomendada antes do setup.
- Nome curto da conta de serviço, sem domínio.
- Exemplo: `plexus-vertical-cuts`.

### `SERVICE_ACCOUNT_EMAIL`

- **Fase:** obrigatória antes do setup.
- Formato:

```text
<SERVICE_ACCOUNT_NAME>@<GOOGLE_CLOUD_PROJECT>.iam.gserviceaccount.com
```

- O setup cria essa conta e concede as permissões usadas por Storage, Firestore, Speech, Secret Manager, Cloud Run, Pub/Sub e Eventarc.

## 3. Aplicação e workers

### `SERVICE_NAME`

- **Fase:** opcional antes do deploy.
- Nome do serviço Cloud Run principal.
- Padrão: `plexus-vertical-cuts`.

### `TRANSCODE_SERVICE_NAME`

- **Fase:** opcional antes do deploy.
- Nome do serviço Cloud Run de transcode.
- Padrão: `plexus-vertical-cuts-transcode`.

### `VERTICAL_RENDER_SERVICE_NAME`

- **Fase:** opcional antes do deploy.
- Nome do serviço Cloud Run de renderização 9:16.
- Padrão: `plexus-vertical-cuts-render`.

### `TRANSCODE_URL`

- **Fase:** deixar vazia antes de `deploy_all.sh`.
- É preenchida automaticamente com a URL do worker implantado.
- Preencha manualmente apenas se o worker já estiver implantado separadamente.

### `VERTICAL_RENDER_URL`

- **Fase:** deixar vazia antes de `deploy_all.sh`.
- É preenchida automaticamente com a URL do renderizador.

### `TRANSCODE_USE_GPU`

- **Fase:** definir antes do deploy.
- `0`: CPU, recomendado para o primeiro deploy e sem necessidade de quota GPU.
- `1`: NVIDIA L4/NVENC. Requer região compatível e quota; o container ainda possui fallback para CPU se o NVENC falhar em runtime.
- Aplica-se **também ao worker de renderização vertical** (`vertical_render_function/deploy.sh`): a análise da v10 roda YOLO-face em todos os frames do capítulo e o encoder usa NVENC; com `1` a imagem é construída com o índice PyTorch CUDA e o serviço sobe com L4 em `--execution-environment=gen2`.

### `AUTOFRAME_ENGINE`

- **Fase:** opcional; definir no `.env` antes do deploy ou do `run_all_local.sh`.
- `legacy` (padrão de rollout): mantém o detector v3 (YuNet + keyframes FFmpeg); o código da v10 já vai na imagem, mas fica inerte até o flip.
- `v10`: pipeline de três passes (YOLO-face + LR-ASD + câmera planejada) para `center_crop`. Ver `autodetect.md`.
- `AUTOFRAME_DEVICE` e `AUTOFRAME_ENCODER` são overrides opcionais de dispositivo/encoder quando `TRANSCODE_USE_GPU` não basta.

### `CLOUD_RUN_DOMAIN`

- **Fase:** deixar vazia no primeiro deploy.
- `deploy_all.sh` grava a URL final da aplicação.

### `DOMAIN`

- **Fase:** `http://localhost:8080` no desenvolvimento local.
- `deploy_all.sh` substitui pelo domínio Cloud Run final.
- Controla URLs/origem e o atributo `Secure` do cookie de sessão.

## 4. Autenticação Google e desenvolvimento local

### `GOOGLE_APPLICATION_CREDENTIALS`

- **Fase:** opcional e somente local.
- Caminho para um JSON de service account ignorado pelo Git.
- Em Cloud Run/Functions e ao usar ADC local, mantenha a linha comentada; não exporte a variável com valor vazio.
- O setup também remove defensivamente um valor vazio para permitir o fallback de ADC.
- ADC é preferível localmente:

```bash
gcloud auth application-default login
```

### `GOOGLE_CLIENT_ID`

- **Fase:** obrigatório antes do deploy da interface, mas não é validado pelo setup.
- Use o OAuth 2.0 Client ID de uma aplicação Web.
- Formato usual: `<número>-<identificador>.apps.googleusercontent.com`.
- Não coloque o OAuth Client Secret em nenhuma variável deste projeto.
- Antes de usar a interface, configure como origens JavaScript autorizadas:
  - `http://localhost:8080`
  - URL final mostrada por `deploy_all.sh`

### `SECRET_KEY`

- **Fase:** obrigatória antes do setup.
- Assina cookies de sessão.
- Deve ser aleatória e diferente de `SECURITY_TOKEN`, `GEMINI_API_KEY` e credenciais OAuth.
- Gere com `openssl rand -hex 48`.

### `INITIAL_ALLOWED_EMAILS`

- **Fase:** recomendada antes do setup.
- E-mails autorizados a entrar, separados por vírgula e sem aspas:

```dotenv
INITIAL_ALLOWED_EMAILS=editor1@example.com,editor2@example.com
```

- O setup cria documentos na coleção `ALLOWED_EMAILS_COLLECTION`.
- Executar o setup novamente adiciona/atualiza esses e-mails; não remove registros antigos.

### `OAUTHLIB_INSECURE_TRANSPORT`

- **Fase:** somente desenvolvimento local.
- `1` permite OAuth em `http://localhost`.
- Não é enviado ao Cloud Run pelo deploy atual.

## 5. Storage e Firestore

### `BUCKET_NAME`

- **Fase:** obrigatória antes do setup.
- Bucket principal de fontes, áudio, legendas, branding e renders.
- Deve ser globalmente único e informado sem `gs://`.
- Regras recomendadas: minúsculas, números e hífens.

### `GCS_WATCH_BUCKET`

- **Fase:** opcional.
- Deixe vazio quando o primeiro deploy aceitar somente upload pela UI/API.
- Quando preenchido, o setup cria/configura o bucket e uma notificação `OBJECT_FINALIZE`; `deploy_all.sh` também implanta `process_gcs_bucket`.
- Use nome diferente de `BUCKET_NAME` para evitar que os próprios artefatos processados sejam reimportados.

### `FIRESTORE_DB_NAME`

- **Fase:** decidir antes do setup.
- Mantenha `(default)` para a instalação normal.

### `COLLECTION_VIDEOS`

- **Fase:** opcional; não altere após começar a usar o sistema sem migrar dados.
- Padrão: `vertical_cuts_videos`.

### `COLLECTION_USERS`

- **Fase:** opcional.
- Padrão: `vertical_cuts_users`.

### `ALLOWED_EMAILS_COLLECTION`

- **Fase:** opcional, mas deve permanecer consistente entre setup e app.
- Padrão: `vertical_cuts_allowed_emails`.

### `COLLECTION_PROMPTS`

- **Fase:** opcional/reservada para prompts configuráveis.
- Padrão: `vertical_cuts_prompts`.

### `COLLECTION_TEMPLATES`

- **Fase:** opcional.
- Padrão: `vertical_cuts_templates`.
- Armazena templates reutilizáveis de logo e título para os renders verticais.

## 6. Segredos internos e IA

### `SECURITY_TOKEN`

- **Fase:** obrigatória antes do setup.
- Autentica chamadas headless, app → workers e importadores → API.
- Gere com `openssl rand -hex 32`.
- O setup envia o valor ao segredo nomeado por `SECURITY_TOKEN_SECRET`.

### `SECRET_KEY_SECRET`

- **Fase:** necessária antes do deploy; o padrão pode ser mantido.
- Nome do Secret Manager que recebe `SECRET_KEY`.

### `SECURITY_TOKEN_SECRET`

- **Fase:** necessária antes do deploy; o padrão pode ser mantido.
- Nome do Secret Manager que recebe `SECURITY_TOKEN` para app e workers.

### `API_TOKEN_SECRET`

- **Fase:** necessária somente ao implantar importadores GCS/S3.
- Deve apontar para o **mesmo segredo** que `SECURITY_TOKEN_SECRET`, porque os importadores chamam a API com o mesmo token.

### `GEMINI_API_KEY`

- **Fase:** obrigatória antes do setup.
- Chave real usada para sugerir capítulos.
- O setup a copia para Secret Manager; o valor não é enviado como variável de ambiente simples no deploy.

### `GEMINI_API_KEY_SECRET`

- **Fase:** necessária antes do deploy; o padrão pode ser mantido.
- Nome do segredo que recebe `GEMINI_API_KEY`.

### `GEMINI_MODEL`

- **Fase:** opcional.
- Modelo usado para saída estruturada de capítulos.
- Padrão recomendado pelo projeto: `gemini-2.5-flash`.
- Se usar um alias como `gemini-flash-latest`, valide disponibilidade e comportamento antes de produção.

### `CHIRP_LOCATION`

- **Fase:** opcional.
- Localização primária do Speech-to-Text v2 Chirp 3.
- Padrão: `us`.

### `CHIRP_FALLBACK_LOCATION`

- **Fase:** opcional.
- Localização tentada quando a primária retorna exaustão de recursos.
- Padrão: `eu`.
- Confirme requisitos de residência de dados antes de permitir fallback entre regiões.

### `MAX_VIDEO_DURATION_SECONDS`

- **Fase:** opcional.
- Duração máxima aceita depois do probe/transcode.
- `3600` equivale a uma hora.

### `DAILY_VIDEO_LIMIT`

- **Fase:** opcional.
- Limite diário por usuário/e-mail.
- Padrão: `20`.

### `DEFAULT_CHAPTER_MIN_SECONDS`

- **Fase:** opcional.
- Duração mínima solicitada ao Gemini para cada capítulo.
- Padrão: `15`.

### `DEFAULT_CHAPTER_MAX_SECONDS`

- **Fase:** opcional.
- Duração máxima solicitada ao Gemini.
- Padrão: `180`.
- Deve ser maior que `DEFAULT_CHAPTER_MIN_SECONDS`.

## 7. Importadores Pub/Sub

### `GCS_PUBSUB_TOPIC`

- **Fase:** padrão pode ser mantido antes do setup.
- Tópico criado para eventos do bucket GCS observado.
- Também é exigido pelo deploy de `process_gcs_bucket`.

### `S3_PUBSUB_TOPIC`

- **Fase:** padrão pode ser mantido antes do setup.
- Tópico criado para mensagens S3 encaminhadas ao Google Pub/Sub.
- O setup Google não configura o lado AWS.

### `GCS_PROCESS_FUNCTION_NAME`

- **Fase:** opcional.
- Nome da função Gen2 GCS.
- Padrão: `process-gcs-bucket`.

### `S3_PROCESS_FUNCTION_NAME`

- **Fase:** opcional.
- Nome da função Gen2 S3.
- Padrão: `process-s3-bucket`.

### `MAIN_API_URL`

- **Fase:** deixar vazia no primeiro deploy.
- `deploy_all.sh` grava a URL Cloud Run principal.
- Obrigatória somente se implantar os importadores de forma individual.

### `GCS_MAIN_BUCKET`

- **Fase:** deve ser igual a `BUCKET_NAME`.
- `deploy_all.sh` corrige automaticamente antes de implantar importadores.
- Se executar a função S3 separadamente, preencha manualmente.

## 8. Importador S3 opcional

Deixe todas estas variáveis vazias quando o primeiro deploy usar somente upload pela interface. O setup continuará criando o tópico S3, mas não criará segredos AWS nem implantará a função.

### `S3_WATCH_BUCKET`

- Nome exato do bucket AWS aceito pela função.
- Quando vazio, `deploy_all.sh` ignora o importador S3.

### `S3_REGION`

- Região AWS do bucket.
- Padrão: `us-east-1`.

### `S3_FUNCTION_MEMORY` e `S3_FUNCTION_TIMEOUT`

- **Fase:** opcionais.
- Memória (padrão `1Gi`) e timeout (padrão `540s`) da função S3. A cópia S3 → GCS é feita por streaming, então a memória não cresce com o tamanho do vídeo.
- `540s` é o limite máximo de funções Gen2 acionadas por evento.

### `AWS_ACCESS_KEY_ID`

- Access key usada somente pelo importador S3.
- Se preenchida junto com `AWS_SECRET_ACCESS_KEY`, o setup envia ambas ao Secret Manager.
- Use um usuário/role AWS com acesso de leitura mínimo ao bucket observado.

### `AWS_SECRET_ACCESS_KEY`

- Secret access key correspondente.
- Nunca versionar ou registrar em logs.

### `AWS_SESSION_TOKEN`

- Opcional para credenciais temporárias STS.
- Quando usado, configure também `AWS_SESSION_TOKEN_SECRET`.

### `AWS_ACCESS_KEY_ID_SECRET`

- Nome do segredo Google que recebe `AWS_ACCESS_KEY_ID`.
- Padrão pode ser mantido.

### `AWS_SECRET_ACCESS_KEY_SECRET`

- Nome do segredo Google que recebe `AWS_SECRET_ACCESS_KEY`.
- Padrão pode ser mantido.

### `AWS_SESSION_TOKEN_SECRET`

- Nome do segredo Google para token STS.
- Deixe vazio com credenciais AWS permanentes.

## 9. Renderização

### `OUTPUT_WIDTH`

- Largura final em pixels.
- Padrão: `1080`.

### `OUTPUT_HEIGHT`

- Altura final em pixels.
- Padrão: `1920`.

Os workers atuais renderizam 1080 × 1920; alterar apenas essas variáveis não muda todas as constantes internas do worker. Mantenha os padrões nesta versão.

### `DEFAULT_CAPTION_FONT`

- Fonte padrão da legenda.
- Padrão: `DejaVu Sans`, instalada nas imagens Docker.
- O deploy principal atual não envia esta variável ao Cloud Run; o padrão do código é usado.

### `DEFAULT_CAPTION_FONT_SIZE`

- Tamanho padrão da legenda.
- Padrão: `54`.
- Pode ser alterado pelo editor por capítulo.

## 10. O que `deploy_all.sh` preenche automaticamente

Não preencha estas variáveis antes do primeiro deploy, salvo se estiver reutilizando serviços existentes:

```dotenv
TRANSCODE_URL=
VERTICAL_RENDER_URL=
CLOUD_RUN_DOMAIN=
MAIN_API_URL=
```

O script também atualiza:

```dotenv
DOMAIN=<URL final do Cloud Run>
GCS_MAIN_BUCKET=<BUCKET_NAME>
```

Depois do deploy, adicione `CLOUD_RUN_DOMAIN` às origens JavaScript autorizadas do OAuth Client e execute um novo login.

## 11. Problemas a corrigir no `.env` atual antes do setup

Revise o arquivo local sem publicar seus valores:

1. Confirme que `GOOGLE_CLOUD_PROJECT` corresponde a um projeto que já existe e tem billing ativo.
2. Confirme que `SERVICE_ACCOUNT_EMAIL` termina com `@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com`.
3. Substitua `SECURITY_TOKEN` se ainda contiver o texto de exemplo.
4. Preencha `GEMINI_API_KEY`; o setup não aceita valor vazio.
5. `SECRET_KEY` deve ser uma chave aleatória gerada para sessões, não um OAuth Client Secret. Se um segredo OAuth real foi usado, rotacione-o no Google Cloud e gere uma nova chave com `openssl rand -hex 48`.
6. Confirme que `BUCKET_NAME` está disponível globalmente.
7. Defina `GCS_MAIN_BUCKET` igual a `BUCKET_NAME` ou deixe `deploy_all.sh` corrigi-lo antes dos importadores.
8. Manter `GCS_WATCH_BUCKET`, `S3_WATCH_BUCKET` e credenciais AWS vazios é correto para o primeiro deploy com upload somente pela UI.
9. Confirme que `INITIAL_ALLOWED_EMAILS` contém o e-mail que fará o primeiro login.
10. No OAuth Client, use somente o Client ID em `GOOGLE_CLIENT_ID`; este projeto não pede OAuth Client Secret.

## 12. Ordem recomendada

```bash
# 1. Autenticar o gcloud e ADC local
gcloud auth login
gcloud auth application-default login

# 2. Preencher e revisar .env
openssl rand -hex 48
openssl rand -hex 32

# 3. Validar sem alterações
./setup_gcp.sh --check

# 4. Provisionar APIs, IAM, Firestore, buckets, tópicos e segredos
./setup_gcp.sh

# 5. Rodar testes locais
source .venv/bin/activate
./test_all.sh

# 6. Implantar todos os componentes
./deploy_all.sh
```
