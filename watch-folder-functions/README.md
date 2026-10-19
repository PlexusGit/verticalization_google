# Guia dos importadores por pasta monitorada (watch folders)

Este diretório contém duas Cloud Functions Gen2 que iniciam o processamento de vídeos automaticamente quando um arquivo chega a um bucket monitorado:

| Diretório | Função implantada (padrão) | Entry point | Origem do vídeo | Gatilho |
|---|---|---|---|---|
| `process_gcs_bucket/` | `process-gcs-bucket` | `process_gcs_bucket` | Bucket **GCS** | Notificação `OBJECT_FINALIZE` → Pub/Sub |
| `process_S3_bucket/` | `process-s3-bucket` | `process_s3_bucket` (minúsculo) | Bucket **AWS S3** | Evento S3 → (ponte AWS) → Pub/Sub |

Ambas terminam chamando `POST {MAIN_API_URL}/api/import/gcs` com `X-API-Key`, o que cria o vídeo e dispara o mesmo pipeline do upload manual (transcode → transcrição → capítulos → rascunhos → `PENDING_REVIEW`). A aprovação e os cortes finais continuam pela interface ou pela API (veja [`../API_GUIDE.md`](../API_GUIDE.md)).

## Sumário

1. [Como funciona](#1-como-funciona)
2. [Pré-requisitos e variáveis do `.env`](#2-pré-requisitos-e-variáveis-do-env)
3. [Caminho rápido (scripts do repositório)](#3-caminho-rápido-scripts-do-repositório)
4. [Guia GCS passo a passo](#4-guia-gcs-passo-a-passo)
5. [Guia S3 passo a passo](#5-guia-s3-passo-a-passo)
6. [Testes locais e automatizados](#6-testes-locais-e-automatizados)
7. [Operação e solução de problemas](#7-operação-e-solução-de-problemas)
8. [Limitações conhecidas](#8-limitações-conhecidas)

---

## 1. Como funciona

```text
GCS (bucket monitorado) ── OBJECT_FINALIZE ──► Pub/Sub (GCS_PUBSUB_TOPIC) ──► process-gcs-bucket ──┐
                                                                                                    ├─► POST /api/import/gcs ─► pipeline
AWS S3 (bucket monitorado) ─► Lambda (ponte) ─► Pub/Sub (S3_PUBSUB_TOPIC) ──► process-s3-bucket ───┘
                                                  (copia o arquivo S3 → GCS principal antes de chamar a API)
```

**GCS** – A função não baixa o vídeo. Ela filtra (bucket correto, extensão permitida), monta um payload determinístico e chama a API; a API copia o objeto do bucket monitorado para o bucket principal (`BUCKET_NAME`).

**S3** – A função recebe o evento S3 e faz o *streaming* do objeto (`boto3` → upload resumível do GCS, em blocos de 16 MiB, sem passar por memória ou disco) para o bucket principal em `{S3_GCS_PREFIX}/{bucket}/{hash}/{arquivo}`, usando *generation match 0* (idempotente). Se esse destino já existir (reentrega), a transferência é pulada. Só então a API é chamada e, depois da resposta, a cópia intermediária é apagada (a API já guardou a cópia gerenciada em `{video_id}/...`). Como o upload resumível só cria o objeto ao ser finalizado, uma transferência interrompida não deixa arquivo parcial.

**Idempotência** – A chave é derivada de `bucket + objeto + generation` (GCS) ou `bucket + chave + versionId/etag/sequencer` (S3). Reentregas do Pub/Sub devolvem o mesmo `video_id` (`created: false`), sem reprocessar.

**Mensagens descartadas × reentregues** – Mensagens malformadas, de outro bucket, de evento que não seja *finalize/ObjectCreated* ou com extensão não permitida são confirmadas (ack) e ignoradas. Respostas da API tratadas por tipo: `2xx` e `409` = sucesso; `400/404/413/415/422` = rejeição **permanente** (registrada em log como erro e confirmada, sem reentrega, pois repetir não muda o resultado); qualquer outra (`401/403/408/429/5xx` e falhas de rede) **lança exceção** e o Pub/Sub reentrega (`--retry`), de modo que um token errado ou uma indisponibilidade temporária não perdem o evento.

Extensões aceitas por padrão: `.mp4 .mov .mkv .webm .avi .mxf` (idênticas às da API). Ajuste com `GCS_ALLOWED_EXTENSIONS` / `S3_ALLOWED_EXTENSIONS`, mas **somente** com extensões que a API aceita; qualquer outra volta como `400` e a função a reentrega indefinidamente.

## 2. Pré-requisitos e variáveis do `.env`

Ferramentas: `gcloud` (autenticado: `gcloud auth login` e `gcloud auth application-default login`), Python 3 e, para o S3, acesso à conta AWS (console ou `aws` CLI).

O aplicativo principal já precisa estar implantado (a URL vira `MAIN_API_URL`) e o projeto GCP configurado (`./setup_gcp.sh`). Todas as configurações ficam no `.env` da raiz:

| Variável | Usada por | Descrição |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT`, `DEPLOY_REGION` | ambas | Projeto e região (a região da função pode ser sobrescrita por `GCS_PROCESS_REGION` / `S3_PROCESS_REGION`). |
| `MAIN_API_URL` | ambas | URL pública do app, sem `/` final (ex.: `https://plexus-vertical-cuts-xxxx.run.app`). `deploy_all.sh` preenche. |
| `API_TOKEN_SECRET` | ambas | **Nome** do segredo no Secret Manager com o valor de `SECURITY_TOKEN` (padrão `plexus-vertical-cuts-api-token`). |
| `GCS_WATCH_BUCKET` | GCS | Bucket a monitorar. **Deve ser diferente de `BUCKET_NAME`**, ou os artefatos do próprio sistema seriam reimportados. |
| `GCS_PUBSUB_TOPIC` | GCS | Tópico que recebe as notificações do bucket (padrão `plexus-vertical-cuts-gcs-imports`). |
| `GCS_ALLOWED_EXTENSIONS` | GCS | Lista separada por vírgula (opcional). |
| `S3_WATCH_BUCKET` | S3 | Nome **exato** do bucket AWS. |
| `S3_PUBSUB_TOPIC` | S3 | Tópico que recebe os eventos S3 (padrão `plexus-vertical-cuts-s3-imports`). |
| `GCS_MAIN_BUCKET` | S3 | Bucket principal de destino; **igual a `BUCKET_NAME`**. |
| `S3_REGION` | S3 | Região do bucket AWS (padrão `us-east-1`). |
| `S3_GCS_PREFIX` | S3 | Prefixo no bucket principal (padrão `imports/s3`). |
| `S3_FUNCTION_MEMORY` | S3 | Memória da função (padrão `1Gi`; não precisa crescer com o tamanho do vídeo, pois a cópia é por streaming). |
| `S3_FUNCTION_TIMEOUT` | S3 | Timeout (padrão `540s`, que é o **máximo** para funções Gen2 acionadas por evento). |
| `AWS_ACCESS_KEY_ID_SECRET`, `AWS_SECRET_ACCESS_KEY_SECRET` | S3 | **Nomes** dos segredos com as credenciais AWS de leitura. |
| `AWS_SESSION_TOKEN_SECRET` | S3 | Opcional, para credenciais temporárias (STS). |
| `GCS_PROCESS_SERVICE_ACCOUNT`, `S3_PROCESS_SERVICE_ACCOUNT` | ambas | Conta de serviço de execução (padrão `SERVICE_ACCOUNT_EMAIL`). |
| `GCS_PROCESS_FUNCTION_NAME`, `S3_PROCESS_FUNCTION_NAME` | ambas | Nome das funções. |
| `FUNCTION_RUNTIME` | ambas | Padrão `python312`. |

Veja também [`../ENV_GUIDE.md`](../ENV_GUIDE.md) (seção 7 e 8).

## 3. Caminho rápido (scripts do repositório)

Para a maior parte dos casos, os scripts da raiz fazem tudo do lado Google:

1. Preencha no `.env`: `GCS_WATCH_BUCKET` (e, se usar S3, `S3_WATCH_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).
2. `./setup_gcp.sh` — habilita as APIs, cria a conta de serviço com os papéis necessários, cria o bucket `GCS_WATCH_BUCKET` e os dois tópicos Pub/Sub, concede `roles/pubsub.publisher` ao agente de serviço do GCS, cria a notificação `OBJECT_FINALIZE` e grava os segredos (token da API e credenciais AWS) no Secret Manager.
3. `./deploy_all.sh` — implanta o app e, em seguida, `process-gcs-bucket` (se `GCS_WATCH_BUCKET` estiver preenchido) e `process-s3-bucket` (se `S3_WATCH_BUCKET` e os nomes dos segredos AWS estiverem preenchidos).
4. Para S3, configure o **lado AWS** (seção 5.1–5.3). O setup Google não cria nada na AWS.

Para implantar apenas uma função, depois do `setup_gcp.sh`:

```bash
./watch-folder-functions/process_gcs_bucket/deploy.sh
./watch-folder-functions/process_S3_bucket/deploy.sh
```

As seções 4 e 5 mostram os mesmos passos manualmente (útil para entender, auditar ou usar outro projeto).

## 4. Guia GCS passo a passo

Variáveis usadas nos exemplos:

```bash
export PROJECT=meu-projeto
export REGION=us-central1
export WATCH=meu-projeto-watch-videos          # GCS_WATCH_BUCKET
export TOPIC=plexus-vertical-cuts-gcs-imports  # GCS_PUBSUB_TOPIC
export SA=plexus-vertical-cuts@${PROJECT}.iam.gserviceaccount.com
export MAIN_API_URL=https://seu-app.run.app
gcloud config set project "$PROJECT"
```

### 4.1 Habilitar APIs

```bash
gcloud services enable pubsub.googleapis.com cloudfunctions.googleapis.com eventarc.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com
```

### 4.2 Criar o bucket monitorado

```bash
gcloud storage buckets create "gs://${WATCH}" --location "$REGION" --uniform-bucket-level-access
```

Quem envia vídeos precisa de `roles/storage.objectCreator` neste bucket (por usuário, grupo ou conta de serviço). Não use o mesmo bucket de `BUCKET_NAME`.

### 4.3 Criar o tópico Pub/Sub

```bash
gcloud pubsub topics create "$TOPIC"
```

### 4.4 Permitir que o Cloud Storage publique no tópico

O agente de serviço do GCS precisa de `pubsub.publisher`:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member "serviceAccount:service-${PROJECT_NUMBER}@gs-project-accounts.iam.gserviceaccount.com" \
  --role roles/pubsub.publisher
```

(O `setup_gcp.sh` concede o mesmo papel no nível do projeto.)

### 4.5 Criar a notificação do bucket

```bash
gcloud storage buckets notifications create "gs://${WATCH}" \
  --topic "$TOPIC" --event-types OBJECT_FINALIZE --payload-format json
# opcional: restringir a uma pasta
#   --object-prefix entrada/
gcloud storage buckets notifications list "gs://${WATCH}"
```

`OBJECT_FINALIZE` dispara quando um objeto é criado ou **sobrescrito** (nova *generation*, logo um novo vídeo).

### 4.6 Segredo com o token da API

O token é o valor de `SECURITY_TOKEN` do app (o `setup_gcp.sh` já o grava). Para criar manualmente:

```bash
printf '%s' "$SECURITY_TOKEN" | gcloud secrets create plexus-vertical-cuts-api-token \
  --replication-policy automatic --data-file=-
# ou, se já existe:
printf '%s' "$SECURITY_TOKEN" | gcloud secrets versions add plexus-vertical-cuts-api-token --data-file=-
```

### 4.7 Permissões da conta de serviço da função

A conta de execução (`SA`) precisa de:

| Papel | Para quê |
|---|---|
| `roles/secretmanager.secretAccessor` | Ler `API_TOKEN` |
| `roles/eventarc.eventReceiver` | Receber o evento do Pub/Sub |
| `roles/run.invoker` | O Eventarc invoca a função (Gen2 roda no Cloud Run) |

```bash
for role in roles/secretmanager.secretAccessor roles/eventarc.eventReceiver roles/run.invoker; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${SA}" --role "$role" --quiet
done
```

A **API principal** (conta de serviço do Cloud Run do app) precisa ler o bucket monitorado para copiar o arquivo: `roles/storage.objectViewer` (ou `objectAdmin`, que a conta padrão já tem) em `gs://${WATCH}`.

### 4.8 Implantar a função

```bash
./watch-folder-functions/process_gcs_bucket/deploy.sh
```

Equivalente manual:

```bash
gcloud functions deploy process-gcs-bucket \
  --gen2 --runtime python312 --region "$REGION" \
  --source watch-folder-functions/process_gcs_bucket \
  --entry-point process_gcs_bucket \
  --trigger-topic "$TOPIC" --retry \
  --set-env-vars "^@^MAIN_API_URL=${MAIN_API_URL}@GCS_WATCH_BUCKET=${WATCH}@GCS_ALLOWED_EXTENSIONS=.mp4,.mov,.mkv,.webm,.avi,.mxf" \
  --set-secrets "API_TOKEN=plexus-vertical-cuts-api-token:latest" \
  --service-account "$SA"
```

### 4.9 Testar

```bash
gcloud storage cp entrevista.mp4 "gs://${WATCH}/entrada/entrevista.mp4"
gcloud functions logs read process-gcs-bucket --gen2 --region "$REGION" --limit 30
curl -sS -H "X-API-Key: $API_KEY" "$MAIN_API_URL/api/videos" | head
```

Nos logs deve aparecer `Importing finalized GCS object ...` e, na API, um vídeo `v_import_...` com `source_type: gcs` e status `TRANSCODING`. Reenviar o mesmo objeto sem alterá-lo não cria outro vídeo; sobrescrever o arquivo (nova *generation*) cria.

## 5. Guia S3 passo a passo

A função S3 consome mensagens do **Pub/Sub do Google**, mas o S3 não publica no Pub/Sub. É necessária uma **ponte do lado AWS** (seção 5.3). A função aceita dois formatos de mensagem:

- o evento nativo do S3 (`{"Records":[{"eventName":"ObjectCreated:Put","s3":{"bucket":{"name":"..."},"object":{"key":"...","eTag":"...","versionId":"..."}}}]}`); ou
- o formato canônico simples: `{"bucket": "...", "key": "...", "versionId": "...", "eTag": "..."}`.

As chaves do S3 chegam com *URL encoding* (`+` por espaço, `%xx`); a função decodifica.

### 5.1 Bucket S3 e permissões de leitura

Crie (ou use) o bucket na região de `S3_REGION`. Para a função baixar os vídeos, crie um usuário IAM dedicado (somente leitura) com a política:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::NOME-DO-BUCKET/*"
    }
  ]
}
```

Gere uma access key para esse usuário. Prefira credenciais de menor privilégio e rotacione-as periodicamente. **Nunca** coloque as chaves no Git.

### 5.2 Criar tópico, conta de serviço e segredos no Google

```bash
export S3_TOPIC=plexus-vertical-cuts-s3-imports   # S3_PUBSUB_TOPIC
gcloud pubsub topics create "$S3_TOPIC"

# segredos AWS (o setup_gcp.sh faz isso a partir de AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY do .env)
printf '%s' "$AWS_ACCESS_KEY_ID"     | gcloud secrets create plexus-vertical-cuts-aws-access-key --replication-policy automatic --data-file=-
printf '%s' "$AWS_SECRET_ACCESS_KEY" | gcloud secrets create plexus-vertical-cuts-aws-secret-key --replication-policy automatic --data-file=-
```

A conta de execução da função S3 precisa dos mesmos três papéis da seção 4.7 e, além disso, de **escrita no bucket principal** (`roles/storage.objectAdmin` ou `objectCreator`), pois ela copia o vídeo para `gs://${BUCKET_NAME}/${S3_GCS_PREFIX}/...`.

### 5.3 Ponte AWS → Pub/Sub

Esta parte **não é criada por nenhum script do repositório** e precisa ser montada na AWS. A opção mais simples é uma Lambda disparada diretamente pelo evento do S3 que republica o evento no Pub/Sub.

1. **Publisher no Google**: crie uma conta de serviço (ex.: `s3-bridge@${PROJECT}.iam.gserviceaccount.com`) com `roles/pubsub.publisher` **somente neste tópico**:

   ```bash
   gcloud iam service-accounts create s3-bridge
   gcloud pubsub topics add-iam-policy-binding "$S3_TOPIC" \
     --member "serviceAccount:s3-bridge@${PROJECT}.iam.gserviceaccount.com" \
     --role roles/pubsub.publisher
   ```

2. **Credencial da Lambda**: use *Workload Identity Federation* (recomendado, sem chave de longa duração) ou, como alternativa mais simples, uma chave JSON da conta `s3-bridge` guardada no AWS Secrets Manager. Com federação:

   ```bash
   gcloud iam workload-identity-pools create aws-pool --location=global
   gcloud iam workload-identity-pools providers create-aws aws-provider \
     --location=global --workload-identity-pool=aws-pool --account-id=ID_DA_CONTA_AWS
   gcloud iam service-accounts add-iam-policy-binding s3-bridge@${PROJECT}.iam.gserviceaccount.com \
     --role roles/iam.workloadIdentityUser \
     --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/aws-pool/attribute.aws_role/ARN_DA_ROLE_DA_LAMBDA"
   gcloud iam workload-identity-pools create-cred-config \
     projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/aws-pool/providers/aws-provider \
     --service-account s3-bridge@${PROJECT}.iam.gserviceaccount.com --aws --output-file cred-config.json
   ```

   Empacote `cred-config.json` com a Lambda e defina `GOOGLE_APPLICATION_CREDENTIALS=cred-config.json` nas variáveis da função.

3. **Lambda (Python 3.12)** — dependência `google-cloud-pubsub` no pacote/layer; variáveis `GCP_PROJECT` e `PUBSUB_TOPIC`:

   ```python
   import json, os
   from google.cloud import pubsub_v1

   publisher = pubsub_v1.PublisherClient()
   TOPIC = publisher.topic_path(os.environ["GCP_PROJECT"], os.environ["PUBSUB_TOPIC"])

   def handler(event, context):
       # 'event' já é o JSON nativo do S3 ({"Records": [...]}); a função do Google o aceita como está.
       publisher.publish(TOPIC, json.dumps(event).encode("utf-8")).result(timeout=30)
       return {"ok": True}
   ```

   A role da Lambda precisa apenas das permissões básicas de execução (logs).

4. **Evento no S3**: em *Properties → Event notifications*, crie um evento **All object create events** (ou `s3:ObjectCreated:*`) com destino **Lambda function**, opcionalmente com *prefix*/*suffix* (ex.: `.mp4`). Se o bucket receber uploads *multipart*, o evento `CompleteMultipartUpload` já é coberto por `ObjectCreated:*`.

> A Lambda e o Workload Identity são pontos de integração externos a este repositório; os comandos acima seguem a documentação das duas nuvens, mas valide-os no seu ambiente.

### 5.4 Implantar a função S3

Preencha no `.env`: `S3_WATCH_BUCKET`, `S3_PUBSUB_TOPIC`, `GCS_MAIN_BUCKET` (= `BUCKET_NAME`), `S3_REGION`, `MAIN_API_URL`, `API_TOKEN_SECRET`, `AWS_ACCESS_KEY_ID_SECRET`, `AWS_SECRET_ACCESS_KEY_SECRET`. Depois:

```bash
./watch-folder-functions/process_S3_bucket/deploy.sh
```

Equivalente manual:

```bash
gcloud functions deploy process-s3-bucket \
  --gen2 --runtime python312 --region "$REGION" \
  --source watch-folder-functions/process_S3_bucket \
  --entry-point process_s3_bucket \
  --trigger-topic "$S3_TOPIC" --retry --memory 1Gi --timeout 540s \
  --set-env-vars "^@^MAIN_API_URL=${MAIN_API_URL}@S3_WATCH_BUCKET=NOME-DO-BUCKET@GCS_MAIN_BUCKET=${BUCKET_NAME}@S3_ALLOWED_EXTENSIONS=.mp4,.mov,.mkv,.webm,.avi,.mxf@AWS_REGION=us-east-1@S3_GCS_PREFIX=imports/s3" \
  --set-secrets "API_TOKEN=plexus-vertical-cuts-api-token:latest,AWS_ACCESS_KEY_ID=plexus-vertical-cuts-aws-access-key:latest,AWS_SECRET_ACCESS_KEY=plexus-vertical-cuts-aws-secret-key:latest" \
  --service-account "$SA"
```

### 5.5 Testar

Sem depender da Lambda, publique diretamente uma mensagem no tópico (o arquivo precisa existir no S3):

```bash
gcloud pubsub topics publish "$S3_TOPIC" --message '{"bucket":"NOME-DO-BUCKET","key":"entrada/entrevista.mp4"}'
gcloud functions logs read process-s3-bucket --gen2 --region "$REGION" --limit 30
```

Em seguida, teste o caminho completo enviando um arquivo ao bucket S3. Resultado esperado: o arquivo aparece em `gs://${BUCKET_NAME}/imports/s3/<bucket>/<hash>/<arquivo>` e um vídeo `source_type: s3` surge na API.

## 6. Testes locais e automatizados

### Testes unitários (sem nuvem)

```bash
./watch-folder-functions/process_gcs_bucket/test.sh
./watch-folder-functions/process_S3_bucket/test.sh
# ou todos: ./test_all.sh
```

Também é possível rodar as duas de uma vez: `python -m pytest watch-folder-functions`. Cada suíte carrega o seu `main.py` com um nome de módulo próprio (`process_gcs_bucket_main` / `process_s3_bucket_main`), e os arquivos de teste têm nomes distintos (`test_process_gcs_bucket.py`, `test_process_s3_bucket.py`), evitando conflito de coleta.

### Execução local

`./run_all_local.sh` sobe o app e as duas funções (portas 8083 e 8084), com `MAIN_API_URL` apontando para o app local e `API_TOKEN` igual a `SECURITY_TOKEN`. Para uma função isolada: `./watch-folder-functions/process_gcs_bucket/run_local.sh`.

Simular uma entrega do Pub/Sub (formato CloudEvent):

```bash
DATA=$(printf '%s' '{"bucket":"MEU_WATCH_BUCKET","name":"entrada/clip.mp4","generation":"1"}' | base64 | tr -d '\n')
curl -sS -X POST http://127.0.0.1:8083 \
  -H "Content-Type: application/json" \
  -H "ce-id: teste-1" -H "ce-specversion: 1.0" \
  -H "ce-type: google.cloud.pubsub.topic.v1.messagePublished" \
  -H "ce-source: //pubsub.googleapis.com/projects/p/topics/t" \
  -d "{\"message\":{\"data\":\"$DATA\",\"attributes\":{\"eventType\":\"OBJECT_FINALIZE\"}}}"
```

Para a S3 use a porta 8084 e um corpo como `{"bucket":"...","key":"..."}`. Localmente, o objeto referenciado precisa existir de verdade (GCS ou S3), pois o app/função o acessa com as credenciais locais.

## 7. Operação e solução de problemas

| Sintoma | Causa provável e ação |
|---|---|
| Nada acontece ao enviar um arquivo ao GCS | Verifique `gcloud storage buckets notifications list gs://WATCH`; o agente `service-<nº>@gs-project-accounts...` precisa de `pubsub.publisher`; confirme `gcloud pubsub subscriptions list --filter="topic:TOPIC"` (o deploy cria a assinatura do gatilho). |
| Log: `Ignoring object from an unwatched bucket` | `GCS_WATCH_BUCKET`/`S3_WATCH_BUCKET` da função difere do bucket do evento. Redeploy com o valor correto. |
| Log: `Ignoring object with an unsupported extension` | Extensão fora de `*_ALLOWED_EXTENSIONS`. |
| `RuntimeError: MAIN_API_URL and API_TOKEN must be configured` | Faltam variável/segredo; confira `--set-env-vars` e `--set-secrets` e o acesso da conta ao segredo. |
| `Main API returned HTTP 401` | O segredo `API_TOKEN` não bate com `SECURITY_TOKEN` do app (recrie a versão do segredo e reimplante/reinicie). |
| Log: `Main API permanently rejected the import (HTTP 400/404/413/415/422)` | A API recusou o arquivo (extensão/objeto inválido). A mensagem é descartada de propósito; corrija o arquivo/configuração e reenvie o vídeo. |
| S3: `botocore ... AccessDenied/NoSuchKey` | Credencial sem `s3:GetObject`, região errada (`S3_REGION`) ou chave decodificada incorretamente. |
| S3: nenhuma mensagem chega ao tópico | O problema está na ponte AWS: confira os logs da Lambda, o evento do S3 e o papel `pubsub.publisher` da conta `s3-bridge`. |
| Vídeo duplicado ou ausente após reenvio | A chave de idempotência inclui a *generation*/versão; reenviar o mesmo objeto sem mudar não cria novo vídeo, sobrescrever cria. |
| `ERROR` no vídeo importado | Mesmo tratamento do upload manual: `POST /api/video/{id}/retry` (veja `../API_GUIDE.md`). |

Logs: `gcloud functions logs read NOME --gen2 --region REGION --limit 50`. Nome do objeto e da chave S3 são registrados apenas como *hash* parcial (SHA-256), e nenhum cabeçalho (`X-API-Key`) é logado.

## 8. Limitações conhecidas

O que **já foi tratado** no código: reentrega infinita de erros permanentes (seção 1), limite de memória em vídeos grandes (cópia por streaming), arquivos parciais no GCS e cópia intermediária esquecida em `imports/s3/` (apagada após a resposta da API).

O que **permanece** e por quê:

- **Limite de 540 s por entrega (S3).** Funções Gen2 acionadas por evento não podem passar de 9 minutos (limite do produto, não ajustável). A cópia por streaming precisa terminar nesse tempo: com ~50 MB/s sustentados cabem vídeos de dezenas de GB, mas o throughput real entre AWS e GCP varia. Se o tempo estourar, a entrega é reiniciada do zero (o upload anterior nunca foi finalizado). Para arquivos muito grandes ou redes lentas, use o Storage Transfer Service para copiar S3 → GCS e chame `/api/import/gcs` (ou deixe a função GCS processar o bucket de destino).
- **Reentregas repetem a cópia.** Depois de apagar a cópia intermediária, uma segunda entrega da mesma mensagem baixa o arquivo de novo antes de a API responder `created: false`. É raro (o Pub/Sub entrega "pelo menos uma vez") e inofensivo, mas gasta transferência.
- **Falhas 5xx/401/403/429 reentregam até o Pub/Sub desistir.** É intencional (para não perder eventos). Para limitar as tentativas, configure uma *dead-letter topic* na assinatura criada pelo gatilho (não é automatizado, porque o Eventarc cria e gerencia essa assinatura):

  ```bash
  SUB=$(gcloud eventarc triggers describe NOME-DO-GATILHO --location "$REGION" \
          --format='value(transport.pubsub.subscription)')
  gcloud pubsub topics create plexus-importers-dlq
  gcloud pubsub subscriptions update "$SUB" \
    --dead-letter-topic plexus-importers-dlq --max-delivery-attempts 10
  # o agente do Pub/Sub precisa publicar na DLQ e assinar a assinatura de origem:
  PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
  gcloud pubsub topics add-iam-policy-binding plexus-importers-dlq --member "serviceAccount:${PUBSUB_SA}" --role roles/pubsub.publisher
  gcloud pubsub subscriptions add-iam-policy-binding "$SUB" --member "serviceAccount:${PUBSUB_SA}" --role roles/pubsub.subscriber
  ```

  (Liste os gatilhos com `gcloud eventarc triggers list --location "$REGION"`.)
- **Dono e título fixos.** Vídeos importados têm `owner_email = "bucket-import"` e título igual ao nome do arquivo; a API não usa metadados do objeto para isso. Renomeie pela interface ou recrie o registro se precisar de outro título.
- **Sem limite diário na importação.** `/api/import/gcs` não aplica `DAILY_VIDEO_LIMIT`; quem controla o volume é quem tem acesso de escrita ao bucket monitorado.
- **Lado AWS não automatizado.** A ponte S3 → Pub/Sub (Lambda ou equivalente) e o IAM da AWS não são criados pelos scripts deste repositório e dependem da sua conta AWS.
- **Credenciais AWS de longa duração** ficam no Secret Manager; use um usuário de leitura mínima e rotacione.
- **Streaming S3 → GCS validado só com testes unitários.** O comportamento contra buckets reais não foi exercitado nesta alteração; faça um teste ponta a ponta (seção 5.5) com um arquivo grande antes de depender dele.
