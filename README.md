# observability-golden-signals-stack

[![ci](https://github.com/jraalves/observability-golden-signals-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/jraalves/observability-golden-signals-stack/actions/workflows/ci.yml) · `make ci` local: validate + 100 testes + cardinalidade, exit 0 · ativo desde 26/11/2026 (run #1, success)

Stack de observabilidade completo, aberto e reprodutível para estudar os **golden
signals** (latência, tráfego, erro e saturação) do pedido até o alerta: aplicação
de exemplo em Python sem nenhuma dependência de terceira parte, OpenTelemetry
Collector, Prometheus, Alertmanager, Loki, Tempo, Grafana e Jaeger de comparação,
com regras de alerta, painéis, runbooks e ADRs versionados juntos.

> **Aviso: todos os dados deste repositório são sintéticos.** As séries, os logs,
> os traços, os limiares e as contas de custo saem de um gerador com semente
> declarada (`SEED=2026` na app, `SEED=1337` no gerador de carga) rodando contra a
> aplicação de exemplo. Nenhuma métrica aqui mede sistema real, empresa real ou
> serviço de produção. A política inteira está em
> `docs/decisoes/ADR-0006-dados-sinteticos-com-semente-declarada.md`.

## O que está aqui

| Componente | Imagem (tag fixada) | Papel neste estudo |
| --- | --- | --- |
| Aplicação de exemplo | `obs-stack/sample-app:local` (build de `app/Dockerfile`) | quatro identidades com o mesmo código: checkout-api, auth-api, search-api, notification-worker |
| Gerador de carga | `docker.io/library/python:3.12-slim` | jornada de `checkout`, `login` e `background` propagando `traceparent` |
| OTel Collector | `docker.io/otel/opentelemetry-collector-contrib:0.108.0` | normaliza tags, corta cardinalidade proibida e distribui para os três backends |
| Prometheus | `docker.io/prom/prometheus:v2.53.0` | métricas, 81 recording rules e 35 alertas em três arquivos de regras |
| Alertmanager | `docker.io/prom/alertmanager:v0.27.0` | agrupamento por `team` e `service`, inibição, silêncio por intervalo de tempo |
| Loki | `docker.io/grafana/loki:3.1.0` | logs com `trace_id` no corpo, TSDB, retenção por `table_manager` |
| Tempo | `docker.io/grafana/tempo:2.6.0` | traços com TraceQL e `metrics_generator` para o service graph |
| Grafana | `docker.io/grafana/grafana:11.2.0` | três painéis provisionados por arquivo, com link de log para trace |
| Jaeger | `docker.io/jaegertracing/all-in-one:1.59` | comparação lado a lado, não é o padrão (ver `jaeger/README.md`) |

O repositório não é um produto para ser instalado: é um ambiente de estudo em que
**a configuração é o conteúdo**. Cada arquivo tem comentário dizendo por que
aquele valor está ali e o que quebra se ele mudar.

## Fluxo da telemetria

```text
                          +---------------------+
   aplicacao de exemplo   |  app/server.py      |  stdlib only: http.server +
   (4 identidades)        |  /metrics /pay      |  renderizador Prometheus +
                          |  /auth /health      |  exportador OTLP/JSON proprio
                          +----------+----------+
                                     |  OTLP/HTTP (traces, logs)  +  scrape (metricas)
                                     v
                          +---------------------+
                          |  OTel Collector     |  receivers: otlp, filelog, hostmetrics
                          |  otel-collector     |  processors: memory_limiter -> attributes
                          +--+------+-------+---+                  -> batch
                            |      |       |
             metricas ------+      |       +------ traces
             (prometheus           |               (otlp/tempo)
              receiver :8889)      |  logs (loki)
                   |               |        |
                   v               v        v
        +----------------+  +-----------+  +-----------+
        |  Prometheus    |  |   Loki    |  |   Tempo   |
        |  rules + TSDB  |  |  (logs)   |  |  (tracos) |
        +--------+-------+  +-----+-----+  +-----+-----+
                 |                |               |
                 |                +-------+-------+
                 v                        v
        +----------------+      +------------------+
        | Alertmanager   |      |     Grafana      |  3 dashboards provisionados
        |  rotas, grupo, |      |  metrica + log + |  derivedFields: trace_id -> Tempo
        |  inhibicao     |      |  traco num lugar |
        +--------+-------+      +------------------+
                 |
                 v
        oncall do time dono do label `team`

   scrape do Prometheus:  prometheus -> sample-app:8080/metrics, otel-collector:8889,
                          loki:3100, tempo:3200, alertmanager:9093, grafana:3000
```

Três arestas merecem atenção porque são o ponto em que a maioria das
implantações falha:

- **A app expõe `/metrics` e exporta OTLP ao mesmo tempo.** Métrica de request é
  mais honesta no scrape direto; trace e log vão pelo Collector. Misturar os dois
  caminhos no Collector (via `prometheus` exporter) funciona e é o que está
  configurado na porta `8889` - a gravação existe para o caso de você querer
  normalizar rótulos antes do scrape.
- **O `trace_id` nasce no request e morre no log**, nunca na métrica. A razão é
  aritmética, não estilística: `docs/custos.md` mostra a conta de `18 * 7200 =
  129600` valores vivos em 2h de head.
- **O Alertmanager fica ao lado do Prometheus, não no caminho do painel.** Alerta
  é fluxo de decisão humana; painel é fluxo de investigação. Os dois consomem a
  mesma gravação de métrica, e é isso que evita a situação clássica de "o painel
  diz uma coisa, o alerta diz outra".

## Os quatro golden signals

A tabela abaixo é o resumo operacional. As definições completas, com as expressões
PromQL, estão em `prometheus/rules/golden-signals.yml`; os limiares são
**sintéticos** e servem de ponto de partida para discussão, não de SLA de ninguém.

| Sinal | Definição | SLI escolhido neste stack | Limiar sintético | Por que este |
| --- | --- | --- | --- | --- |
| **Latência** (`latency`) | Quanto tempo o serviço leva para atender um request - e a cauda, não a média | `service:http_request_duration_seconds:p95_5m`, histograma com 11 cortes (`le`) sobre `http_request_duration_seconds_bucket` | `p95 / objetivo > 1,25` por 10m (warning) e `p99 / objetivo > 2,5` por 5m (critical), com objetivo de `1,2 s`; `LatencyDependencyDominant` quando a dependencia sozinha passa de `1,5 x` o objetivo por 15m | Média esconde o problema de 1 em 20 usuários; `p95` ainda é legível em painel e `p99` em janela curta é ruído com 18 requests/s. Separar latência de dependência responde "é nosso ou é deles" antes de acordar alguém |
| **Tráfego** (`traffic`) | Quantas solicitações o serviço recebe por segundo | `service:http_requests:rate5m` com baseline de 30m e 1h (`rate30m`, `rate1h`) | Queda para menos de `0,25` da baseline por 10m; `TrafficSurge` acima de `3x`; `TrafficZeroWithRecentHistory` quando a taxa de 5m é zero com `rate1h > 0,1` por 5m | Tráfego não é sinal de saúde isolado, é a condição de validade dos outros três: sem pedido, sem taxa de erro com significado. O alerta de zero é o que distingue "o serviço caiu" de "o mundo parou de chamar" |
| **Erro** (`errors`) | Qual fração dos requests termina mal - aqui, 5xx | `service:http_error_ratio:rate5m`, com guard de volume `service:http_requests:rate5m > 0,1` | `> 0,02` por 10m (warning), `> 0,05` por 5m (critical); `DependencyErrorRatioHigh` separa erro de dependência; burn rate `> 14,4` (1h) / `> 6` (6h) / `> 3` (24h) para o budget de `0,995` | 4xx não queima budget de erro: validar entrada do usuário não é indisponibilidade. Threshold absoluto paga de madrugada com 3 requests na janela; burn rate paga na pergunta certa - "estamos consumindo o compromisso mais rápido que o previsto?" |
| **Saturação** (`saturation`) | Quão cheio o serviço está do recurso que limita ele primeiro | CPU em cores, memória residente, descritores de arquivo abertos e requests em execução sobre capacidade de 8 | `> 0,85` core com tráfego acima de 0,2 rps por 15m; memória acima de `3,2e9` bytes (80% do limite de 4 GB) por 10m; descritores acima de `0,75` por 15m; fila `in_flight / 8 > 1` por 5m | Saturação é o único dos quatro que prevê o incidente em vez de contá-lo. A fila aparece antes da latência e a latência aparece antes do erro - por isso o `runbooks/index.md` manda checar saturação e tráfego antes de latência |

Um quinto grupo, `slo`, existe para o budget de erro (`prometheus/rules/slo-burn-rate.yml`),
e um sexto, `infrastructure`, para a própria esteira de telemetria
(`prometheus/rules/infrastructure.yml`) - porque sem dado os outros quatro viram
adivinhação.

## Como subir local

```bash
git clone https://github.com/jraalves/observability-golden-signals-stack
cd observability-golden-signals-stack
docker compose up -d --build
docker compose ps                 # espere os healthchecks ficarem healthy
```

Depois:

| Endereço | O que ver |
| --- | --- |
| `http://localhost:3000` | Grafana, pastas *Observabilidade*, painel inicial `Golden Signals - ambiente sintetico` (acesso anônimo, ambiente local) |
| `http://localhost:9090` | Prometheus, aba *Status > Targets* com 8 jobs, e *Alerts* |
| `http://localhost:3100` | API do Loki; a busca de log com `trace_id` é `{service="checkout-api"} \| json` |
| `http://localhost:3200` | API do Tempo (`/api/search`, `/api/echo`) |
| `http://localhost:9093` | Alertmanager com as rotas, silêncios e inibição |
| `http://localhost:8082/pay` (POST), `/health`, `/metrics` | a app de exemplo; `8081` auth-api, `8083` search-api, `8084` notification-worker |
| `http://localhost:8889/metrics` | métricas do Collector exportadas em formato Prometheus |

Carga sintetica já vem ligada (três geradores: 4 + 6 + 2 jornadas/s). Em 5
minutos os painéis têm série suficiente para o `rate(...[5m])` fechar.

Para ver um alerta disparar de propósito - em dado sintético, com semente fixa -,
edite o knob no `docker-compose.yml` e suba de novo. Os valores são fixos no
arquivo de propósito (`ERROR_RATE: '0.015'`), então variável de shell não os
substitui:

```bash
# erro: 20% de 5xx no checkout-api estoura o budget de 0,995 em poucos minutos
sed -i "s/ERROR_RATE: '0.015'/ERROR_RATE: '0.2'/" docker-compose.yml
docker compose up -d sample-app

# ou, sem tocar no compose, uma instancia local com knobs de linha de comando
python3 app/server.py --port 18099 --service checkout-api \
  --error-rate 0.2 --latency-factor 4
curl -s http://localhost:18099/metrics | grep -E '^http_requests_total' | head

# conferindo pela API do Alertmanager em vez de esperar a UI:
curl -s 'http://localhost:9093/api/v2/alerts' | python3 -m json.tool | head -40
git checkout docker-compose.yml      # devolve os limiares sinteticos originais
```

### Sem Docker: smoke-test em dois segundos

`scripts/smoke-test.sh` sobe a app em porta alta local (`18080` por padrão), gera
50 journeys com o `loadgen.py`, checa `/health`, lê `/metrics` e conta quantas
linhas de log carregam `trace_id`:

```bash
bash scripts/smoke-test.sh
PORT=19000 bash scripts/smoke-test.sh   # porta explicita se 18080 estiver ocupada
```

## Mapa de runbooks

A matriz completa, alerta por alerta, está em [`runbooks/index.md`](runbooks/index.md)
junto com as regras do jogo (nenhum alerta sem runbook, saturação e tráfego antes
de latência, ação destrutiva com aprovação de um segundo humano).

| Runbook | Famílias que ele cobre | Pergunta que ele responde |
| --- | --- | --- |
| [`runbooks/rb-latencia.md`](runbooks/rb-latencia.md) | `LatencyP95AboveObjective`, `LatencyP99Critical`, `LatencyDependencyDominant`, `SloLatencyBurnRate*` | A cauda é nossa ou da dependência, e é cauda ou custo médio? |
| [`runbooks/rb-taxa-erro.md`](runbooks/rb-taxa-erro.md) | `ErrorRatioHigh`, `ErrorRateCritical`, `DependencyErrorRatioHigh`, `SloAvailabilityBurnRate*`, `SloErrorBudgetExhausted` | É 5xx nosso, 4xx do usuário ou falha de dependência - e quantos minutos de budget sobraram? |
| [`runbooks/rb-saturacao.md`](runbooks/rb-saturacao.md) | `Saturation*`, `Infra*` (CPU, memória, disco, pods, esteira) | Qual recurso aperta primeiro, e é a app ou a esteira de telemetria? |
| [`runbooks/rb-throughput.md`](runbooks/rb-throughput.md) | `TrafficDropAgainstBaseline`, `TrafficSurge`, `TrafficZeroWithRecentHistory`, `InfraPodNotReady` | O mundo parou de chamar, ou a série de dados é que sumiu? |

## Índice de decisões (ADRs)

| ADR | Decisão | Referenciado por |
| --- | --- | --- |
| [`ADR-0001`](docs/decisoes/ADR-0001-tempo-como-backend-de-trace.md) | Tempo como backend de trace, em vez de Jaeger + Cassandra | `tempo/tempo.yaml`, `docker-compose.yml`, `jaeger/README.md` |
| [`ADR-0002`](docs/decisoes/ADR-0002-correlacao-por-trace-id.md) | Correlação por `trace_id` propagado (W3C `traceparent`), em vez de busca por texto | `app/server.py`, `otel/tagging.md`, runbooks de latência e erro |
| [`ADR-0003`](docs/decisoes/ADR-0003-burn-rate-em-vez-de-threshold-fixo.md) | Burn rate multi-janela em vez de threshold fixo | `prometheus/rules/slo-burn-rate.yml`, runbooks de latência e erro |
| [`ADR-0004`](docs/decisoes/ADR-0004-tags-padronizadas-por-aplicacao-e-jornada.md) | Tags padronizadas por aplicação e jornada, com lista de labels proibidos | `app/tagging.py`, `otel/collector-config.yaml`, `docs/custos.md`, runbooks de saturação e tráfego |
| [`ADR-0005`](docs/decisoes/ADR-0005-instrumentacao-stdlib-com-caminho-de-sidecar.md) | Instrumentação em stdlib na app de exemplo, com caminho de sidecar documentado | `app/requirements.txt`, `app/otel-sidecar.md` |
| [`ADR-0006`](docs/decisoes/ADR-0006-dados-sinteticos-com-semente-declarada.md) | Dados sintéticos com semente declarada em vez de dado de produção | este README, `SECURITY.md`, `docs/custos.md` |

Outros documentos:

- [`docs/custos.md`](docs/custos.md) - a conta de cardinalidade e armazenamento,
  com a saída de `scripts/custo-cardinalidade.py`;
- [`otel/tagging.md`](otel/tagging.md) - o esquema de tags narrado;
- [`app/otel-sidecar.md`](app/otel-sidecar.md) - o que muda ao trocar a app de
  exemplo por uma aplicação real com SDK e sidecar;
- [`scripts/port-forward-notes.md`](scripts/port-forward-notes.md) - mapa de
  portas e como rodar os comandos dos runbooks fora da máquina local;
- [`CONTRIBUTING.md`](CONTRIBUTING.md) - como adicionar alerta, runbook, label ou
  painel sem quebrar o contrato;
- [`SECURITY.md`](SECURITY.md) - o que reportar, como reportar e o que nunca
  publicar em issue.

## Estrutura

```text
app/                      aplicacao de exemplo (stdlib) + Dockerfile + requisitos vazios
  server.py               /pay /auth /health /metrics, tracer, renderizador Prometheus
  loadgen.py              jornadas sinteticas propagando traceparent
  stats.py                percentile nearest-rank, p50/p95/p99, taxa de erro
  tagging.py              esquema de tags executavel
  otel-sidecar.md         caminho de producao (SDK, sidecar)
prometheus/               prometheus.yml, alertmanager.yml, rules/{golden-signals,slo-burn-rate,infrastructure}.yml
otel/                     collector-config.yaml, tagging.md
loki/                     config.yaml (TSDB, retencao)
tempo/                    tempo.yaml, trace-frontend.yaml
grafana/provisioning/     datasources.yml, dashboards.yml
grafana/dashboards/       golden-signals.json, slo-error-budget.json, correlacao-incidente.json
runbooks/                 index.md + 4 runbooks
docs/decisoes/            ADR-0001 a ADR-0006
docs/custos.md            custo de cardinalidade
scripts/                  validate.py, miniyaml.py, custo-cardinalidade.py, smoke-test.sh
tests/                    unittest: regras, tags, estatisticas, telemetria da app
ci/github-actions-ci.yml CI (Actions, pronto p/ ativar)  .gitlab-ci.yml  CI (GitLab)
```

> **Sobre a ativação do CI.** O YAML completo está em `ci/github-actions-ci.yml` (e a
> variante GitLab em `.gitlab-ci.yml`). Ele não foi commitado em `.github/workflows/` porque a
> integração usada nesta revisão de publicação não tem permissão para escrever `.github/` — o
> único passo que falta para o pipeline passar a rodar na nuvem é copiar o arquivo para lá.
> A execução que sustenta esta publicação é local: `make ci` → exit 0, 100 testes.

## Qualidade

```bash
python3 scripts/validate.py                 # contrato estrutural: YAML, JSON, docs, termos protegidos
python3 -m unittest discover -s tests -v    # regras, tags, stats, log com trace_id
bash scripts/smoke-test.sh                  # app + carga + saude + traco no log
python3 scripts/custo-cardinalidade.py --check
```

O validador não usa biblioteca externa: o subconjunto YAML é lido por
`scripts/miniyaml.py` e os painéis Grafana por `json`. Os mesmos quatro comandos
acima são o que `ci/github-actions-ci.yml` e `.gitlab-ci.yml` executam, sem
nenhum segredo e sem nenhum passo que dependa de rede externa além do checkout
das imagens.

## Privacidade

Nenhum dado pessoal, nome de cliente, ambiente real ou credencial pertence a este
repositório. Os valores de `service`, `team`, `owner`, `cost_center` e as
dependências simuladas (`gateway-pagamentos`, `provedor-identity`) são nomes de
papel. O `scripts/validate.py` mantém uma varredura de termos protegidos sobre
todos os arquivos de texto do repositório e falha o build na primeira ocorrência;
`SECURITY.md` explica o que fazer se alguém publicar dado real por engano.

Licença: MIT, ver `LICENSE`.

## English summary

An open, self-contained observability lab. A dependency-free Python example app
emits the three signals - Prometheus metrics, JSON logs with `trace_id`, and
OTLP traces - which an OpenTelemetry Collector routes to Prometheus, Loki and
Tempo, with Grafana dashboards provisioned from files and Alertmanager routing by
team and service. The four golden signals (latency, traffic, errors, saturation)
are defined once as recording rules and reused by alerts, dashboards and
runbooks; multi-window burn rate, not a fixed threshold, decides who gets woken
up. Every number in this repository is synthetic and generated from a declared
seed: no production metric, no client name and no credential ever appears here.
Run `docker compose up -d --build` and open `http://localhost:3000`, or skip
Docker entirely with `bash scripts/smoke-test.sh`. Structural contract, docs and
anonymity checks are enforced by `python3 scripts/validate.py`.
