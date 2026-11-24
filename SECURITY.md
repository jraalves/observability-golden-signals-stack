# Security policy

## O que este repositório é, e o que ele nunca é

Um ambiente de estudo de observabilidade: aplicação de exemplo em Python
(stdlib apenas), Prometheus, Alertmanager, OpenTelemetry Collector, Loki, Tempo,
Grafana e Jaeger de comparação, ligados por `docker-compose.yml`.

Ele **não é** um serviço exposto à internet, **não processa dado de pessoa
física** e **não contém credencial de ambiente nenhum**. Todo dado gerado aqui é
sintético, com semente declarada (ver `docs/decisoes/ADR-0006.md`).

## Escopo do relatório

Reporte:

- vulnerabilidade na aplicação de exemplo (`app/server.py`, `app/loadgen.py`) que
  permita execução de código, leitura de arquivo fora de `LOG_DIR` ou SSRF via
  `OTEL_EXPORTER_OTLP_ENDPOINT`;
- erro no `scripts/validate.py` que faça o validador aprovar conteúdo que deveria
  rejeitar (inclusive na varredura de termos protegidos);
- configuração nos arquivos versionados que exponha porta administrativa de forma
  mais ampla do que o `docker-compose.yml` documenta;
- versão de componente com CVE conhecida que este repositório fixe por tag
  (`prom/prometheus:v2.53.0`, `grafana/loki:3.1.0`, `grafana/tempo:2.6.0`,
  `otel/opentelemetry-collector-contrib:0.108.0`, `grafana/grafana:11.2.0`,
  `prom/alertmanager:v0.27.0`, `jaegertracing/all-in-one:1.59`).

Não reporte (não é vulnerabilidade, é decisão de projeto):

- Grafana com acesso anônimo habilitado: é `GF_AUTH_ANONYMOUS_ENABLED=true`
  **apenas** porque o ambiente local é de estudo. Em produção a configuração é o
  contrário, e isso está escrito no `docker-compose.yml`.
- Ausência de autenticação entre componentes no compose local: a rede é isolada
  por `networks: observability`, sem exposição além das portas listadas em
  `scripts/port-forward-notes.md`.
- Cardinalidade ou volume de dados sintéticos: ver `docs/custos.md`.

## Como reportar

1. **Prefira uma issue privada do tipo vulnerability no GitHub**
   (`github.com/jraalves/observability-golden-signals-stack/security/advisories/new`).
   Isso mantém a discussão fora do histórico público até haver correção.
2. Se o canal acima não estiver disponível, escreva para o mantenedor pelo
   perfil público em `github.com/jraalves`, indicando a versão afetada (tag ou
   commit) e o componente.
3. **Não abra issue pública com prova de conceito funcional** antes de haver
   correção ou 90 dias de prazo.

Prazo de resposta pretendido: 5 dias úteis para acusar recebimento, 30 dias para
uma primeira análise. Este é um projeto de estudo mantido em tempo parcial, e é
justo dizer isso em vez de prometer SLA.

## O que não publicar em issue nem em PR

Este é o motivo real de a seção existir: o caminho mais comum de vazamento aqui
não é código vulnerável, é gente colando dado de ambiente próprio para explicar o
problema.

- Logs reais, mesmo parcialmente: remova `trace_id` original, URL com parâmetro,
  cabeçalho e corpo.
- Captura de tela de painel com nome de cliente, servidor ou ambiente real.
- Arquivo `.env`, `kubeconfig`, certificado, chave privada, token de acesso de
  plataforma de codigo, token de mensageria, chave de provedor de nuvem, string de
  conexao com banco.
- Nome de empresa, cliente ou empregador em texto, caminho de arquivo ou valor de
  campo. O validador mantém uma varredura de termos protegidos sobre todos os
  arquivos de texto do repositório e falha o build na primeira ocorrência:
  `python3 scripts/validate.py`.

Para reproduzir um problema, use a aplicação de exemplo com os knobs sintéticos
(`ERROR_RATE`, `LATENCY_FACTOR`, `SEED`) e anexe a saída de
`bash scripts/smoke-test.sh`. Um relatório que passa no smoke-test local é, por
construção, livre de dado de terceiros.

## Hardening recomendado se você copiar o stack

| Item | Local (este repositório) | Produção |
| --- | --- | --- |
| Grafana | acesso anônimo com papel Admin | SSO, RBAC, `auth.anonymous` desligado |
| Prometheus | sem autenticação, regras e alvos versionados | apenas leitura via proxy com autenticação; API de administracao desligada (`--web.enable-admin-api` ausente) |
| Alertmanager | sem autenticação, roteamento sem canal externo | autenticação no ingress, integracoes com token guardado em secret manager, nunca em git |
| Loki / Tempo | filesystem local, `auth_enabled: false` | object storage com credencial de runtime, `auth_enabled: true` com tenant por time |
| OTel Collector | portas 4317/4318 na rede interna | TLS, autenticação por mTLS ou token, `memory_limiter` dimensionado, `health_check` em porta local apenas |
| Aplicação | env var plana | origem de configuração com rotação, sem segredo em imagem |

## Política de versão

Corrigimos a branch `main` e, quando houver aviso publicado, a tag mais recente
anterior. Não há contrato de compatibilidade entre versões: este repositório é
material de estudo, e um `docker compose pull` pode mudar comportamento de painel.
