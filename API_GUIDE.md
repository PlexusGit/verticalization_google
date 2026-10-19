# Guia da API — Plexus Vertical Cuts

Este guia mostra como usar o Plexus Vertical Cuts **apenas pela API**: enviar um vídeo, acompanhar o processamento, revisar os capítulos sugeridos, aprovar e baixar os cortes verticais (9:16) e horizontais (16:9).

A referência interativa (OpenAPI) fica em `{BASE_URL}/docs`.

## Sumário

- [Guia da API — Plexus Vertical Cuts](#guia-da-api--plexus-vertical-cuts)
  - [Sumário](#sumário)
  - [1. Visão geral](#1-visão-geral)
  - [2. Autenticação e convenções](#2-autenticação-e-convenções)
  - [3. Limites](#3-limites)
  - [4. Passo 1 — Enviar o vídeo](#4-passo-1--enviar-o-vídeo)
    - [Opção A — Upload multipart (simples)](#opção-a--upload-multipart-simples)
    - [Opção B — Upload resumível direto ao GCS (arquivos grandes)](#opção-b--upload-resumível-direto-ao-gcs-arquivos-grandes)
    - [Opção C — Importar objeto de um bucket (idempotente)](#opção-c--importar-objeto-de-um-bucket-idempotente)
    - [Legendas próprias (opcional)](#legendas-próprias-opcional)
  - [5. Passo 2 — Acompanhar o processamento](#5-passo-2--acompanhar-o-processamento)
    - [Retomar após erro](#retomar-após-erro)
  - [6. Passo 3 — Ler os capítulos](#6-passo-3--ler-os-capítulos)
  - [7. Passo 4 — Revisar e editar (opcional)](#7-passo-4--revisar-e-editar-opcional)
    - [Editar limites, título e legendas](#editar-limites-título-e-legendas)
    - [Adicionar e excluir capítulos](#adicionar-e-excluir-capítulos)
    - [Pedir novas sugestões](#pedir-novas-sugestões)
    - [Renderizar um novo preview (enquadramento e legendas)](#renderizar-um-novo-preview-enquadramento-e-legendas)
  - [8. Templates (logo e título)](#8-templates-logo-e-título)
  - [9. Passo 5 — Aprovar](#9-passo-5--aprovar)
  - [10. Passo 6 — Baixar os cortes](#10-passo-6--baixar-os-cortes)
  - [11. Exemplo completo](#11-exemplo-completo)
    - [Bash](#bash)
    - [Python (`requests`)](#python-requests)
  - [12. Limpeza, referência e solução de problemas](#12-limpeza-referência-e-solução-de-problemas)
    - [Resumo dos endpoints](#resumo-dos-endpoints)
    - [Solução de problemas](#solução-de-problemas)

---

## 1. Visão geral

Fluxo de estados do vídeo (`status`):

```text
UPLOADING → TRANSCODING → TRANSCRIBING → SUGGESTING → RENDERING_DRAFTS
          → PENDING_REVIEW → RENDERING_FINALS → COMPLETED
```

Qualquer etapa pode terminar em `ERROR`.

| Etapa | O que acontece |
|---|---|
| `TRANSCODING` | Transcodifica o vídeo e extrai o áudio. |
| `TRANSCRIBING` | Transcrição PT-BR (Chirp 3). Pulada se você enviou um SRT. |
| `SUGGESTING` | O Gemini sugere capítulos a partir das legendas. |
| `RENDERING_DRAFTS` | Um **rascunho** vertical (preview) é gerado para cada capítulo. |
| `PENDING_REVIEW` | Aguardando você revisar/editar e **aprovar**. |
| `RENDERING_FINALS` | Para cada capítulo aprovado: render final vertical 1080×1920 (com legendas queimadas e template) e corte horizontal 1920×1080 (mesmos instantes de início/fim). |
| `COMPLETED` | Tudo pronto para download. |

Pontos importantes:

- Os rascunhos **não** têm legendas nem template aplicados. Ambos são aplicados apenas no render **final**.
- O corte horizontal 16:9 é gerado automaticamente após cada final vertical bem-sucedido. Uma falha no horizontal **não** invalida o vertical (veja `horizontal_status`).
- O pipeline roda em segundo plano; as chamadas retornam rápido (`202`) e você acompanha por polling.

## 2. Autenticação e convenções

Todas as rotas `/api/*` aceitam o cabeçalho:

```http
X-API-Key: <valor de SECURITY_TOKEN do servidor>
```

Sem o cabeçalho (ou com valor errado) a resposta é `401 {"error": "Não autorizado"}`.

> Rotas administrativas (`GET/POST /api/users`, `/api/prompts*`) exigem sessão de navegador com perfil admin e **não** funcionam com API key. Elas não fazem parte deste guia.

Convenções:

- Corpo e respostas em JSON (`Content-Type: application/json`), exceto uploads multipart e o SRT.
- Erros: `{"error": ...}` (mensagens em português; em `422` o valor é a lista de erros de validação do Pydantic).
- Códigos comuns: `400` entrada inválida, `401` sem autorização, `404` não encontrado, `409` conflito de estado, `422` validação, `429` limite diário.
- Tempos (`start_seconds`, `end_seconds`, `time`) são sempre **segundos** no vídeo de origem.
- `GET /health` não exige autenticação.

Nos exemplos:

```bash
export BASE_URL="https://seu-servico.run.app"   # ou http://127.0.0.1:8080 localmente
export API_KEY="valor-de-SECURITY_TOKEN"
```

## 3. Limites

Valores padrão; podem variar por implantação.

| Limite | Valor |
|---|---|
| Extensões aceitas | `.mp4 .mov .mkv .webm .avi .mxf` |
| Duração máxima | 3600 s (`MAX_VIDEO_DURATION_SECONDS`) — excedê-la leva o vídeo a `ERROR` |
| Vídeos por dia (por dono) | 20 (`DAILY_VIDEO_LIMIT`); uploads via API key contam para o dono `"api"` → `429` |
| Duração sugerida dos capítulos | 15–180 s |
| Logos | PNG/JPG/WebP, até 10 MB |

## 4. Passo 1 — Enviar o vídeo

Todas as opções devolvem um `video_id`, usado nas demais chamadas.

### Opção A — Upload multipart (simples)

`POST /api/videos` — o arquivo passa pelo servidor; ideal para arquivos pequenos/médios.

Campos do formulário: `file` (obrigatório), `title` (obrigatório), `description`, `subtitle_file` (SRT opcional).

```bash
curl -sS -X POST "$BASE_URL/api/videos" \
  -H "X-API-Key: $API_KEY" \
  -F "file=@entrevista.mp4" \
  -F "title=Entrevista com a ministra" \
  -F "description=Gravação de 12/10"
```

Resposta `202`:

```json
{"video_id": "v_4a1b4ec1328e4189", "status": "TRANSCODING"}
```

### Opção B — Upload resumível direto ao GCS (arquivos grandes)

1. Criar a sessão:

```bash
curl -sS -X POST "$BASE_URL/api/upload/init" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
        "title": "Entrevista com a ministra",
        "description": "",
        "filename": "entrevista.mp4",
        "content_type": "video/mp4",
        "size_bytes": 734003200
      }'
```

```json
{"video_id": "v_...", "session_url": "https://storage.googleapis.com/...", "content_type": "video/mp4"}
```

2. Enviar os bytes para a `session_url` (sem `X-API-Key`):

```bash
curl -sS -X PUT "$SESSION_URL" -H "Content-Type: video/mp4" --data-binary @entrevista.mp4
```

3. Finalizar e iniciar o processamento (opcionalmente com legendas próprias em `subtitle_text`):

```bash
curl -sS -X POST "$BASE_URL/api/upload/finalize" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"video_id": "v_..."}'
```

`202 {"video_id": ..., "status": "TRANSCODING"}`. Erros: `404` upload desconhecido, `400` arquivo ausente no GCS ou SRT inválido, `409` processamento já iniciado.

### Opção C — Importar objeto de um bucket (idempotente)

`POST /api/import/gcs` copia um objeto de um bucket GCS para o armazenamento gerenciado.

```bash
curl -sS -X POST "$BASE_URL/api/import/gcs" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
        "bucket": "meu-bucket",
        "object_name": "entrada/entrevista.mp4",
        "source_type": "gcs",
        "source_id": "meu-bucket/entrada/entrevista.mp4#12345"
      }'
```

`source_id` (ou `idempotency_key`) é obrigatório e torna a chamada idempotente: repetir a mesma chave devolve o mesmo `video_id` com `"created": false`, sem reprocessar. O `video_id` é determinístico (`v_import_<hash>`) e o título é o nome do arquivo.

### Legendas próprias (opcional)

Se você enviar um SRT (`subtitle_file` na opção A, `subtitle_text` na opção B), a transcrição automática é pulada e as legendas são usadas para sugerir capítulos e para queimar nos finais. SRT sem nenhuma legenda → `400`.

## 5. Passo 2 — Acompanhar o processamento

```bash
curl -sS -H "X-API-Key: $API_KEY" "$BASE_URL/api/video/$VIDEO_ID"
```

Campos principais da resposta:

| Campo | Descrição |
|---|---|
| `id`, `title`, `source_filename` | Identificação. |
| `status` | Um dos estados da seção 1. |
| `error`, `error_type` | Presentes quando `status = ERROR`. |
| `duration_seconds` | Duração do vídeo. |
| `chapters[]` | Capítulos ordenados por `start_seconds` (seção 6). |
| `approved_chapter_ids`, `approved_at` | Após a aprovação. |

Faça polling a cada ~30 s até `PENDING_REVIEW` (depois do passo 1) e até `COMPLETED` (depois da aprovação). Pare também em `ERROR`.

Listagem resumida (até 100 vídeos, mais recentes primeiro; `status` opcional):

```bash
curl -sS -H "X-API-Key: $API_KEY" "$BASE_URL/api/videos?status=PENDING_REVIEW"
```

### Retomar após erro

- `POST /api/video/{id}/retry` — retoma o pipeline reaproveitando transcode/transcrição já salvos. Só aceito em `ERROR` ou `UPLOADING` (senão `409`). Retorna `202 {"status": "RETRYING"}`.
- `POST /api/video/{id}/retry-finals` — após a aprovação, re-renderiza **apenas** os finais que falharam. Só em `ERROR` (`409` caso contrário). Retorna `202 {"status": "RENDERING_FINALS", "retrying": [...]}`.

## 6. Passo 3 — Ler os capítulos

`GET /api/video/{id}` inclui `chapters`. Campos relevantes de cada capítulo:

| Campo | Descrição |
|---|---|
| `id` | Ex.: `chapter_001`. |
| `title`, `reason` | Título e justificativa sugeridos. |
| `start_seconds`, `end_seconds` | Limites no vídeo de origem. |
| `subtitle_text` | SRT do capítulo, com tempos relativos ao início do capítulo. |
| `selected` | Marcação de exportação (informativa). |
| `template_id`, `template_title` | Escolha de template (`null` = usar o padrão). |
| `render_options` | Opções de render persistidas (seção 7). |
| `draft_status`, `draft_gcs_path`, `draft_preview_url`, `draft_error` | Rascunho vertical. |
| `final_status`, `final_gcs_path`, `final_preview_url`, `final_error` | Final vertical. |
| `horizontal_status`, `horizontal_gcs_path`, `horizontal_preview_url`, `horizontal_error` | Corte 16:9. |

`*_status` ∈ `QUEUED`, `PROCESSING`, `COMPLETED`, `ERROR`. As `*_preview_url` são URLs assinadas que **expiram em 60 minutos**; consulte o vídeo novamente para obter novas.

Outros recursos de leitura:

- `GET /api/video/{id}/subtitles.srt` — transcrição completa (SRT).
- `GET /api/video/{id}/source-url` — `{"url": ...}` assinada (4 h) para o vídeo de origem.

## 7. Passo 4 — Revisar e editar (opcional)

Se os capítulos sugeridos servem, pule direto para a [aprovação](#9-passo-5--aprovar). Só edite o que precisar.

### Editar limites, título e legendas

`PUT /api/video/{id}/chapters/{chapter_id}` — corpo completo (`title`, `start_seconds` e `end_seconds` são obrigatórios):

```bash
curl -sS -X PUT "$BASE_URL/api/video/$VIDEO_ID/chapters/chapter_001" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
        "title": "A decisão do ministério",
        "reason": "Trecho com a declaração principal",
        "start_seconds": 27.1,
        "end_seconds": 118.3,
        "subtitle_text": "",
        "selected": true
      }'
```

Regras:

- `end_seconds > start_seconds` e não pode ultrapassar `duration_seconds` (`400`).
- Se os limites mudarem, um novo **rascunho** é re-renderizado automaticamente.
- `subtitle_text` vazio é recalculado a partir do SRT do vídeo para o novo intervalo; texto preenchido é respeitado (SRT com tempos relativos ao capítulo).
- `render_options` pode ser enviado aqui para **persistir** enquadramento e estilo de legenda sem disparar render (o servidor mantém branding e artefatos do worker já salvos e normaliza `final=false`/`template_id`).

### Adicionar e excluir capítulos

```bash
# adicionar (dispara rascunho automático)
curl -sS -X POST "$BASE_URL/api/video/$VIDEO_ID/chapters" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"title": "Bastidores", "start_seconds": 300, "end_seconds": 360}'
# → 201 {"chapter_id": "chapter_ab12cd34ef56"}

# excluir
curl -sS -X DELETE -H "X-API-Key: $API_KEY" "$BASE_URL/api/video/$VIDEO_ID/chapters/chapter_001"
```

### Pedir novas sugestões

`POST /api/video/{id}/regenerate-chapters` — **apaga todos os capítulos** e cria novos com o Gemini a partir das legendas atuais. Retorna `{"count": N}`. Ele não renderiza rascunhos; se precisar de previews, use `/render` em cada capítulo.

### Renderizar um novo preview (enquadramento e legendas)

`POST /api/video/{id}/chapters/{chapter_id}/render` → `202 {"status": "QUEUED"}`.

```json
{
  "layout_mode": "center_crop",
  "caption_style": {
    "font": "DejaVu Sans",
    "font_size": 54,
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 3,
    "position": "bottom"
  },
  "template_id": null,
  "final": false
}
```

| Campo | Valores |
|---|---|
| `layout_mode` | `center_crop` (padrão): enquadramento **automático** (detecção de rostos, falante ativo e plano de câmera). `split_stack`: enquadramento **manual** em dois quadros empilhados. |
| `keyframes_top`, `keyframes_bottom` | Âncoras do modo `split_stack`: `[{"time": 31.5, "x": 480, "y": 270}, ...]` (até 200). `time` em segundos **absolutos** do vídeo de origem, dentro dos limites do capítulo; `x`/`y` = canto superior esquerdo da janela de corte, em pixels do vídeo de origem (o servidor limita ao tamanho do quadro; entre âncoras a posição é interpolada). Sem âncoras, o quadro é centralizado. |
| `caption_style.font_size` | 18–120 |
| `caption_style.color`, `outline_color` | `#RRGGBB` |
| `caption_style.outline_width` | 0–12 |
| `caption_style.position` | `top`, `middle`, `bottom` |
| `template_id` | id de template, `"none"` (sem template) ou `null` (usa o padrão). |
| `final` | `false` = rascunho (sem legendas e sem template). `true` = final (aplica template e queima legendas). |

Exemplo manual (`split_stack`):

```bash
curl -sS -X POST "$BASE_URL/api/video/$VIDEO_ID/chapters/chapter_001/render" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
        "layout_mode": "split_stack",
        "keyframes_top":    [{"time": 27.1, "x": 200,  "y": 0}],
        "keyframes_bottom": [{"time": 27.1, "x": 1100, "y": 540}]
      }'
```

Observações:

- Para saber quando o novo preview ficou pronto, observe `draft_status` voltar a `COMPLETED` e `draft_render_id` mudar; use o novo `draft_preview_url`.
- Renders concorrentes do mesmo capítulo: só o mais recente atualiza o estado visível.
- As opções do capítulo (`render_options`) são reaproveitadas na aprovação: legendas queimadas nos finais usam o `caption_style` salvo, e o plano de câmera calculado no rascunho é reutilizado no final (sem reanalisar o vídeo).
- Um `final: true` direto neste endpoint também funciona, mas o caminho recomendado é aprovar (seção 9), que renderiza todos os finais em lote e marca o vídeo como `COMPLETED`.

## 8. Templates (logo e título)

Um template define logo e caixa de título aplicados no vídeo vertical **final**. Placeholders no título: `{chapter_title}` e `{video_title}`.

Listar (o padrão aparece primeiro):

```bash
curl -sS -H "X-API-Key: $API_KEY" "$BASE_URL/api/templates"
```

Criar (`multipart/form-data`) — `POST /api/templates` (e `PUT /api/templates/{id}` para editar, com os mesmos campos):

```bash
curl -sS -X POST "$BASE_URL/api/templates" -H "X-API-Key: $API_KEY" \
  -F "title=Jornal da Noite" \
  -F "title_macro={chapter_title}" \
  -F "logo_x_percent=68" -F "logo_y_percent=5" \
  -F "logo_width_percent=27" -F "logo_height_percent=10" \
  -F "title_x_percent=10" -F "title_y_percent=18" \
  -F "title_width_percent=80" -F "title_height_percent=12" \
  -F "title_font_size=54" \
  -F "is_default=true" \
  -F "logo=@logo.png"
# → 201 {"id": "...", "title": "Jornal da Noite"}
```

Todas as geometrias são **percentuais** da tela vertical e devem caber nela (`x + largura ≤ 100`, `y + altura ≤ 100`), senão `422`. Apenas um template pode ser o padrão.

Resolução do template na aprovação, para cada capítulo:

1. `template_ids[chapter_id]` no corpo da aprovação (id de template); se ausente, o `template_id` salvo no capítulo.
2. `"none"` força **sem template** (escolha persistente).
3. Vazio/`null` usa o **template padrão**, se existir.

Um snapshot do template é gravado no capítulo na aprovação, então edições posteriores no template não alteram finais já aprovados.

## 9. Passo 5 — Aprovar

Só é possível com o vídeo em `PENDING_REVIEW`.

```bash
curl -sS -X POST "$BASE_URL/api/video/$VIDEO_ID/approve" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
        "chapter_ids": ["chapter_001", "chapter_003"],
        "template_ids": {"chapter_001": "TEMPLATE_ID", "chapter_003": "none"}
      }'
# → 202 {"status": "RENDERING_FINALS"}
```

- `chapter_ids`: 1 a 100 capítulos existentes; os demais **não** serão renderizados.
- `template_ids`: opcional; capítulos omitidos seguem a regra de resolução da seção 8.
- Erros: `409` se os capítulos ainda não estão prontos ou já foram aprovados (a aprovação trava o vídeo; chame **uma vez**); `400` capítulo/template inexistente; `422` corpo inválido.

Acompanhe com `GET /api/video/{id}` até `status = COMPLETED`. Se virar `ERROR`, use `retry-finals` (seção 5).

## 10. Passo 6 — Baixar os cortes

Para cada capítulo aprovado (`final_status = COMPLETED`):

| Tipo | Arquivo |
|---|---|
| `draft` | rascunho vertical 1080×1920 (sem legendas/template) |
| `final` | vertical 1080×1920 com legendas e template |
| `horizontal` | 1920×1080 (16:9), mesmo início/fim do capítulo, MP4 H.264/AAC |

```http
GET /api/video/{video_id}/chapters/{chapter_id}/{draft|final|horizontal}/download
```

A rota responde `307` redirecionando para uma URL assinada (válida por 60 min). Recomenda-se **não** reenviar a API key ao domínio de armazenamento: capture a URL do redirecionamento e baixe sem o cabeçalho.

```bash
URL=$(curl -sS -o /dev/null -w '%{redirect_url}' \
  -H "X-API-Key: $API_KEY" \
  "$BASE_URL/api/video/$VIDEO_ID/chapters/chapter_001/final/download")
curl -sS -o chapter_001_vertical.mp4 "$URL"
```

Alternativa: ler `final_preview_url` / `horizontal_preview_url` do `GET /api/video/{id}` (também expiram em 60 min).

Respostas: `404 {"error": "Render indisponível"}` se o arquivo ainda não existe; `400` para tipo inválido. Se o corte horizontal falhar, `horizontal_status` fica `ERROR` com `horizontal_error`, mas o final vertical continua disponível.

## 11. Exemplo completo

### Bash

```bash
#!/usr/bin/env bash
set -euo pipefail
: "${BASE_URL:?}" "${API_KEY:?}"
H=(-H "X-API-Key: $API_KEY")

wait_status() {  # $1=video_id  $2=status desejado
  while :; do
    s=$(curl -sS "${H[@]}" "$BASE_URL/api/video/$1" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], d.get("error",""), sep="|")')
    st=${s%%|*}
    echo "status: $st"
    [[ "$st" == "$2" ]] && return 0
    [[ "$st" == "ERROR" ]] && { echo "falha: ${s#*|}" >&2; return 1; }
    sleep 30
  done
}

VIDEO_ID=$(curl -sS -X POST "$BASE_URL/api/videos" "${H[@]}" \
  -F "file=@entrevista.mp4" -F "title=Entrevista" | python3 -c 'import sys,json; print(json.load(sys.stdin)["video_id"])')
echo "video_id=$VIDEO_ID"

wait_status "$VIDEO_ID" PENDING_REVIEW

IDS=$(curl -sS "${H[@]}" "$BASE_URL/api/video/$VIDEO_ID" | python3 -c '
import sys,json
print(json.dumps([c["id"] for c in json.load(sys.stdin)["chapters"]]))')

curl -sS -X POST "$BASE_URL/api/video/$VIDEO_ID/approve" "${H[@]}" \
  -H "Content-Type: application/json" -d "{\"chapter_ids\": $IDS}"

wait_status "$VIDEO_ID" COMPLETED

for id in $(echo "$IDS" | python3 -c 'import sys,json; print(*json.load(sys.stdin))'); do
  for kind in final horizontal; do
    url=$(curl -sS -o /dev/null -w '%{redirect_url}' "${H[@]}" \
      "$BASE_URL/api/video/$VIDEO_ID/chapters/$id/$kind/download")
    [[ -n "$url" ]] && curl -sS -o "${id}_${kind}.mp4" "$url"
  done
done
```

### Python (`requests`)

```python
import os, time, requests

BASE = os.environ["BASE_URL"].rstrip("/")
S = requests.Session()
S.headers["X-API-Key"] = os.environ["API_KEY"]


def call(method, path, **kw):
    r = S.request(method, f"{BASE}{path}", **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r


def wait_for(video_id, target, interval=30, timeout=3 * 3600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        video = call("GET", f"/api/video/{video_id}").json()
        print("status:", video["status"])
        if video["status"] == target:
            return video
        if video["status"] == "ERROR":
            raise RuntimeError(f"{video.get('error_type')}: {video.get('error')}")
        time.sleep(interval)
    raise TimeoutError(target)


def download(video_id, chapter_id, kind, dest):
    r = S.get(f"{BASE}/api/video/{video_id}/chapters/{chapter_id}/{kind}/download", allow_redirects=False)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    with requests.get(r.headers["Location"], stream=True) as dl:  # sem X-API-Key
        dl.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in dl.iter_content(1 << 20):
                f.write(chunk)
    return True


with open("entrevista.mp4", "rb") as f:
    video_id = call("POST", "/api/videos", files={"file": f}, data={"title": "Entrevista"}).json()["video_id"]

video = wait_for(video_id, "PENDING_REVIEW")
chapter_ids = [c["id"] for c in video["chapters"]]  # filtre aqui o que quiser aprovar

call("POST", f"/api/video/{video_id}/approve", json={"chapter_ids": chapter_ids})
wait_for(video_id, "COMPLETED")

for cid in chapter_ids:
    for kind in ("final", "horizontal"):
        download(video_id, cid, kind, f"{cid}_{kind}.mp4")
```

## 12. Limpeza, referência e solução de problemas

Excluir um vídeo (capítulos, renders e arquivos): `DELETE /api/video/{id}` → `{"ok": true}`.

### Resumo dos endpoints

| Método | Rota | Uso |
|---|---|---|
| `POST` | `/api/videos` | upload multipart |
| `POST` | `/api/upload/init` | iniciar upload resumível |
| `POST` | `/api/upload/finalize` | concluir upload e iniciar processamento |
| `POST` | `/api/import/gcs` | importar de bucket (idempotente) |
| `GET` | `/api/videos` | listar vídeos (`?status=`) |
| `GET` | `/api/video/{id}` | vídeo + capítulos + URLs assinadas |
| `GET` | `/api/video/{id}/source-url` | URL assinada do vídeo de origem |
| `GET` | `/api/video/{id}/subtitles.srt` | transcrição |
| `POST` | `/api/video/{id}/retry` | retomar pipeline |
| `POST` | `/api/video/{id}/retry-finals` | refazer apenas finais com falha |
| `POST` | `/api/video/{id}/regenerate-chapters` | novas sugestões do Gemini (substitui todos) |
| `POST` | `/api/video/{id}/chapters` | adicionar capítulo |
| `PUT` | `/api/video/{id}/chapters/{chapter_id}` | editar capítulo |
| `DELETE` | `/api/video/{id}/chapters/{chapter_id}` | excluir capítulo |
| `POST` | `/api/video/{id}/chapters/{chapter_id}/render` | novo preview/render |
| `POST` | `/api/video/{id}/approve` | gerar finais (vertical + horizontal) |
| `GET` | `/api/video/{id}/chapters/{chapter_id}/{kind}/download` | redireciona ao arquivo (`draft`/`final`/`horizontal`) |
| `GET` / `POST` / `PUT` | `/api/templates`, `/api/templates/{id}` | templates |
| `DELETE` | `/api/video/{id}` | excluir vídeo |

### Solução de problemas

| Sintoma | Causa provável / ação |
|---|---|
| `401 Não autorizado` | `X-API-Key` ausente ou diferente de `SECURITY_TOKEN` do servidor. |
| `403` em `/api/prompts` ou `/api/users` | Rotas só para admin com sessão de navegador. |
| `429 Limite diário de vídeos atingido` | Limite por dono (API key = `"api"`). Aguarde o dia seguinte (UTC) ou ajuste `DAILY_VIDEO_LIMIT`. |
| `400 Formato não suportado` | Use uma das extensões da seção 3. |
| Vídeo em `ERROR` | Leia `error`/`error_type` e chame `retry`; se a falha foi nos finais, `retry-finals`. |
| `ERROR` por duração | Vídeo maior que `MAX_VIDEO_DURATION_SECONDS`. |
| `409` em `approve` | Vídeo fora de `PENDING_REVIEW` ou já aprovado. |
| `409` em `upload/finalize` | O processamento já começou; consulte o status. |
| `404 Render indisponível` | O arquivo ainda não foi gerado; aguarde o `*_status` ficar `COMPLETED`. |
| Download/preview retorna 403 do storage | URL assinada expirada (60 min); busque outra via `GET /api/video/{id}` ou `/download`. |
| `horizontal_status: ERROR` | O corte 16:9 falhou, mas o vertical final está válido; veja `horizontal_error`. |
| Segurança em produção | Nunca implante com o `SECURITY_TOKEN` padrão (`development-only-change-me`); guarde a chave em secret e não a exponha em clientes públicos. |
