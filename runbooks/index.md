# Indice de runbooks: alerta -> runbook -> painel

Matriz gerada a partir dos rule files versionados, nao de memoria. Se um alerta novo
aparece aqui sem linha, `python3 scripts/validate.py` falha - e assim que esta tabela
nao apodrece. O caminho de cada alerta tambem esta em `annotations.runbook_url` nas
regras, entao o payload do webhook do Alertmanager ja chega com o link.

Como ler a tabela: **Alerta** e o que chegou, **Runbook** e o procedimento, **Painel**
e por onde comecar antes de digitar qualquer comando, **Primeiro comando** e a
verificacao de menor custo que ja separa duas hipoteses. Os comandos assumem o
ambiente local do `docker-compose.yml`; em ambiente proprio veja
[`scripts/port-forward-notes.md`](../scripts/port-forward-notes.md).

## Regras do jogo

- **Nenhum alerta sem runbook.** Se voce precisa de um alerta, precisa de 15 linhas de
  procedimento. `scripts/validate.py` cobra `annotations.runbook_url` em toda regra de
  alerta e aponta para um arquivo que existe neste diretorio.
- **Saturacao e traffic sempre antes de latencia.** Latencia e a consequencia visivel;
  a causa costuma estar em capacidade ou em ausencia de dado.
- **Alerta de esteira (`Infra*`) tem prioridade sobre alerta de negocio.** Sem dado, os
  outros alertas viram adivinhacao. `InfraAlertNotificationFailures` e o mais alto deste
  stack por esse motivo.
- **Acao destrutiva exige aprovacao de um segundo humano**, com o `trace_id` de exemplo
  registrado no thread do incidente. Esta regra esta escrita em cada runbook, nao so aqui.
- **Todo numero exibido nos paineis deste ambiente e sintetico.** Os thresholds tambem:
  sao pontos de partida para discussao, nao SLA de ninguem.

## Familia: latencia

Runbook unico: [`rb-latencia.md`](rb-latencia.md). Objetivo do procedimento: separar
cauda de custo medio, e nosso de dependencia, em menos de 3 minutos.

| Alerta | Severity | Signal | Runbook | Painel | Primeiro comando |
| --- | --- | --- | --- | --- | --- |
| `LatencyP95AboveObjective` | warning | `latency` | [rb](rb-latencia.md) | `obs-golden-signals` p1/p10 + `obs-correlacao` p1 | `promtool query instant http://localhost:9090 'service:http_request_duration_seconds:p95_5m'` |
| `LatencyP99Critical` | critical | `latency` | [rb](rb-latencia.md) | `obs-golden-signals` p1/p10 + `obs-correlacao` p1 | `promtool query instant http://localhost:9090 'service:http_request_duration_seconds:p95_5m'` |
| `LatencyDependencyDominant` | warning | `latency` | [rb](rb-latencia.md) | `obs-golden-signals` p1/p10 + `obs-correlacao` p1 | `promtool query instant http://localhost:9090 'service:http_request_duration_seconds:p95_5m'` |
| `SloLatencyBurnRateFast` | critical | `slo` | [rb](rb-latencia.md) | `obs-golden-signals` p1/p10 + `obs-correlacao` p1 | `promtool query instant http://localhost:9090 'service:http_request_duration_seconds:p95_5m'` |
| `SloLatencyBurnRateSlow` | warning | `slo` | [rb](rb-latencia.md) | `obs-golden-signals` p1/p10 + `obs-correlacao` p1 | `promtool query instant http://localhost:9090 'service:http_request_duration_seconds:p95_5m'` |

## Familia: erros

Runbook unico: [`rb-taxa-erro.md`](rb-taxa-erro.md). Primeiro corte: 4xx nao e
indisponibilidade. Segundo corte: erro nosso x erro de dependencia.

| Alerta | Severity | Signal | Runbook | Painel | Primeiro comando |
| --- | --- | --- | --- | --- | --- |
| `ErrorRatioHigh` | warning | `errors` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |
| `ErrorRateCritical` | critical | `errors` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |
| `DependencyErrorRatioHigh` | warning | `errors` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |
| `SloAvailabilityBurnRateFast` | critical | `slo` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |
| `SloAvailabilityBurnRateMedium` | critical | `slo` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |
| `SloAvailabilityBurnRateSlow` | warning | `slo` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |
| `SloErrorBudgetExhausted` | warning | `slo` | [rb](rb-taxa-erro.md) | `obs-slo-budget` p1 + `obs-correlacao` p2/p3/p5 | `promtool query instant http://localhost:9090 'service:http_error_ratio:rate5m'` |

## Familia: traffic

Runbook unico: [`rb-throughput.md`](rb-throughput.md). Cuidado com "zero":
ausencia de serie e um incidente diferente de ausencia de cliente.

| Alerta | Severity | Signal | Runbook | Painel | Primeiro comando |
| --- | --- | --- | --- | --- | --- |
| `TrafficDropAgainstBaseline` | warning | `traffic` | [rb](rb-throughput.md) | `obs-golden-signals` p3/p8 | `promtool query instant http://localhost:9090 'job:scrape_up:ratio'` |
| `TrafficSurge` | info | `traffic` | [rb](rb-throughput.md) | `obs-golden-signals` p3/p8 | `promtool query instant http://localhost:9090 'job:scrape_up:ratio'` |
| `TrafficZeroWithRecentHistory` | critical | `traffic` | [rb](rb-throughput.md) | `obs-golden-signals` p3/p8 | `promtool query instant http://localhost:9090 'job:scrape_up:ratio'` |

## Familia: saturation (aplicacao)

Runbook unico: [`rb-saturacao.md`](rb-saturacao.md). Saturacao antecede erro; o
objetivo e agir antes do `ErrorRatioHigh` existir.

| Alerta | Severity | Signal | Runbook | Painel | Primeiro comando |
| --- | --- | --- | --- | --- | --- |
| `SaturationFileDescriptorsHigh` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `SaturationMemoryNearLimit` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `SaturationQueuePressureCritical` | critical | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `SaturationCpuAboveLimit` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `InfraCpuSaturation` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `InfraMemorySaturation` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `InfraDiskSpaceLow` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `InfraDiskAlmostFull` | critical | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |
| `InfraTSDBHeadSeriesHigh` | warning | `saturation` | [rb](rb-saturacao.md) | `obs-golden-signals` p9 + `obs-correlacao` p2 | `promtool query instant http://localhost:9090 'service:http_requests:queue_pressure'` |

## Familia: infraestrutura e esteira de telemetria

Mesmo runbook de saturacao, porque o metodo e identico: delimitar recurso, confirmar
que a acao nao apaga a testemunha, e medir saida por tendencia. Estes alertas sao os
que protegem a capacidade de ver os outros.

| Alerta | Severity | Signal | Runbook | Painel | Primeiro comando |
| --- | --- | --- | --- | --- | --- |
| `InfraTargetDown` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraPipelineComponentDown` | critical | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraOtelExporterFailures` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraOtelDataDropped` | critical | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraLokiWriteFailures` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraTempoFlushFailures` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraAlertNotificationFailures` | critical | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraRuleEvaluationFailures` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraPrometheusConfigReloadFailed` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraPodRestartLoop` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |
| `InfraPodNotReady` | warning | `infrastructure` | [rb](rb-saturacao.md) | `obs-golden-signals` p7 + `/targets` do Prometheus | `promtool query instant http://localhost:9090 'pipeline:otel_export_failures:rate5m'` |

## Cobertura por arquivo de regras

| Rule file | Grupos | Alertas | Runbooks referenciados |
| --- | --- | --- | --- |
| `prometheus/rules/golden-signals.yml` | 5 | 13 | `rb-latencia.md`, `rb-saturacao.md`, `rb-taxa-erro.md`, `rb-throughput.md` |
| `prometheus/rules/slo-burn-rate.yml` | 4 | 6 | `rb-latencia.md`, `rb-taxa-erro.md` |
| `prometheus/rules/infrastructure.yml` | 4 | 16 | `rb-saturacao.md` |

Total: 35 alertas, 4 runbooks executaveis.

## Antes de abrir qualquer chamado

1. Confirme que o dado esta fresco: `job:scrape_up:ratio` e o timestamp da ultima serie.
2. Confirme a janela do alerta (`for:`) e se o `expr` usa recording rule ou consulta crua.
3. Procure no `ALERTS{alertstate="firing"}` se o que arde e um servico ou uma familia
   inteira - uma familia inteira e quase sempre causa comum (borda, deploy de base,
   esteira de telemetria).
4. So depois disso abra o runbook correspondente, com o painel ja em tela.

## Painéis deste repositorio

| uid | titulo | uso no incidente |
| --- | --- | --- |
| `obs-golden-signals` | Golden Signals - ambiente sintetico | triagem: qual dos quatro sinais saiu do regime |
| `obs-slo-budget` | SLO e Error Budget - ambiente sintetico | decisao: isso justifica pagina ou justifica congelar release |
| `obs-correlacao` | Correlacao de incidente: metrica, log e trace | investigacao: do agregado ao request individual |

Os tres JSON estao em `grafana/dashboards/` e sao provisionados pelo arquivo, sem
edicao pela UI (`allowUiUpdates: false`). Mudanca de painel e PR, com os mesmos testes
dos rule files.
