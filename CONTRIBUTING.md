# Contribuindo com o Plexus Vertical Cuts

Obrigado por ajudar a melhorar o Plexus Vertical Cuts. Este guia explica como preparar um ambiente de desenvolvimento, entender o repositório, testar alterações e enviar uma contribuição.

## Antes de começar

Abra uma issue antes de iniciar uma funcionalidade grande, mudança arquitetural ou mudança de comportamento. Para pequenas correções de bugs e melhorias de documentação, um pull request pode ser aberto diretamente.

Ao contribuir, você concorda que seu trabalho será licenciado sob a [Apache License 2.0](LICENSE) do projeto.

## Pré-requisitos

O desenvolvimento local requer:

- Python 3.12
- FFmpeg e ffprobe
- Google Cloud CLI
- Docker para testar imagens de container ou deploys
- Um projeto GCP com faturamento ativo para testes ponta a ponta na nuvem

A aplicação usa Cloud Run, Cloud Functions Gen2, Firestore, Cloud Storage, Speech-to-Text v2, Gemini, Pub/Sub, Eventarc, Artifact Registry e Secret Manager. A maioria dos testes unitários não exige recursos de nuvem implantados.

## Estrutura do repositório

| Caminho | Função |
|---|---|
| `app/` | Aplicação FastAPI, interface web, autenticação, persistência e orquestração |
| `tests/` | Testes da aplicação principal |
| `transcode_function/` | Worker de normalização de vídeo em CPU/GPU e seus testes |
| `vertical_render_function/` | Renderizador de vídeo vertical e seus testes |
| `functions/process_gcs_bucket/` | Importador opcional de Pub/Sub do GCS |
| `functions/process_S3_bucket/` | Importador opcional de S3 para Pub/Sub |
| `scripts/` | Utilitários compartilhados de shell e manutenção |
| `setup_gcp.sh` | Configuração idempotente de recursos no GCP |
| `run_all_local.sh` | Orquestrador de desenvolvimento local |
| `deploy_all.sh` | Orquestrador de implantação de todos os componentes configurados |

Consulte o [README.md](README.md) para a arquitetura, estados de processamento, modelo de dados e visão geral da API.

## Preparar um ambiente local

Crie um único ambiente virtual na raiz do repositório. Ele inclui dependências da aplicação, workers, funções e testes.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
cp .env.example .env
```

Revise o [ENV_GUIDE.md](ENV_GUIDE.md) antes de editar o `.env`. No mínimo, fluxos locais que acessam o Google Cloud precisam de configurações válidas de projeto, armazenamento, autenticação e IA.

Autentique as Application Default Credentials quando for necessário comportamento com acesso à nuvem:

```bash
gcloud auth application-default login
```

Nunca versione `.env`, arquivos JSON de service account, chaves de API, secrets OAuth, credenciais AWS, vídeos gerados ou outros dados sensíveis. Prefira Application Default Credentials localmente e identidade de workload nos serviços implantados.

## Executar o projeto localmente

Inicie a aplicação e todos os workers com:

```bash
./run_all_local.sh
```

Os endpoints padrão são:

| Componente | URL |
|---|---|
| Aplicação | `http://127.0.0.1:8080` |
| Documentação OpenAPI | `http://127.0.0.1:8080/docs` |
| Worker de transcode | `http://127.0.0.1:8081` |
| Health check do renderizador vertical | `http://127.0.0.1:8082/health` |
| Importador GCS | `http://127.0.0.1:8083` |
| Importador S3 | `http://127.0.0.1:8084` |

Pressione `Ctrl+C` para encerrar os processos iniciados pelo script. Os importadores opcionais iniciam mesmo sem buckets de monitoramento configurados, mas ignoram ou rejeitam eventos até serem configurados.

Para executar os serviços separadamente, use:

```bash
./transcode_function/run_local.sh
./vertical_render_function/run_local.sh
./run_local.sh
./functions/process_gcs_bucket/run_local.sh
./functions/process_S3_bucket/run_local.sh
```

## Fazer uma alteração

1. Crie um branch a partir do `main` mais recente.
2. Mantenha a alteração focada e evite reformatação ou refatoração não relacionadas.
3. Siga os padrões existentes nos módulos e testes vizinhos.
4. Adicione ou atualize testes para mudanças de comportamento e correções de bugs.
5. Execute o conjunto de testes relevantes mais pequeno durante o desenvolvimento e, antes de abrir um pull request, execute o conjunto completo.

Convenções adicionais do projeto:

- Use type hints e modelos Pydantic para dados de requisição, resposta e configuração em Python.
- Mantenha os handlers de rota do FastAPI enxutos; coloque a lógica de serviços externos e processamento em `app/services/` quando prático.
- Preserve a compatibilidade entre workers locais e seus entry points implantados em Cloud Run ou Functions.
- Trate operações de Firestore e Pub/Sub como recuperáveis e idempotentes.
- Construa comandos FFmpeg como listas de argumentos em vez de strings de shell.
- Mantenha a interface da aplicação em português brasileiro, exceto quando uma mudança mais ampla de localização for proposta.
- Não enfraqueça autenticação, verificações de allowlist, validação de entrada, tratamento de secrets ou controles de IAM da nuvem para fazer um teste ou deploy passar.

## Testar suas alterações

Execute as verificações da aplicação principal:

```bash
./test.sh
```

Execute um teste focado durante o desenvolvimento:

```bash
python -m pytest -q tests/test_app.py
python -m pytest -q tests/test_app.py::nome_do_teste
```

Cada componente implantável tem seu próprio conjunto de testes:

```bash
./transcode_function/test.sh
./vertical_render_function/test.sh
./functions/process_gcs_bucket/test.sh
./functions/process_S3_bucket/test.sh
```

Antes de enviar um pull request, execute todos os conjuntos e valide todos os scripts de shell:

```bash
./test_all.sh
```

Atualmente não há comando separado de lint ou verificação de tipos estática. Siga o estilo existente e garanta que `./test_all.sh` seja bem-sucedido.

## Alterações de GCP e implantação

Recursos de nuvem podem gerar custos e implantações afetam ambientes compartilhados. Não execute scripts de setup ou implantação em um projeto compartilhado sem autorização.

Para validar a configuração obrigatória do GCP sem criar ou alterar recursos:

```bash
./setup_gcp.sh --check
```

Os comandos completos de setup e implantação são:

```bash
./setup_gcp.sh
./deploy_all.sh
```

Ao alterar código de implantação:

- Mantenha os scripts idempotentes quando possível.
- Preserve a operação em CPU quando o suporte opcional a GPU estiver desativado.
- Verifique tanto configurações opcionais vazias quanto preenchidas.
- Não versione URLs, IDs de projeto, credenciais ou secrets de um ambiente privado em `.env.example`.
- Documente novas variáveis de ambiente tanto em `.env.example` quanto em `ENV_GUIDE.md`.

## Diretrizes de commit e pull request

Escreva subjects de commit curtos e descritivos que expliquem a intenção da alteração. Mantenha os commits revisáveis e evite incluir artefatos gerados.

Um pull request deve incluir:

- Uma descrição clara do problema e da solução
- Links de issues relacionadas, quando aplicável
- Testes adicionados ou atualizados
- Os comandos de verificação executados
- Capturas de tela ou uma breve gravação para mudanças visíveis na interface
- Impacto de configuração, migração, segurança ou implantação

Antes de enviar, inspecione os arquivos staged e confirme que não há credenciais ou arquivos locais incluídos:

```bash
git status
git diff --staged
```

Revisores podem solicitar alterações por correção, segurança, manutenibilidade, cobertura de testes ou consistência com o escopo inicial intencionalmente simples do projeto.

## Reportar problemas de segurança

Não divulgue vulnerabilidades exploráveis, credenciais ou detalhes de infraestrutura privada em uma issue pública. Contate os mantenedores privadamente e inclua apenas as informações necessárias para reproduzir e avaliar o problema com segurança.
