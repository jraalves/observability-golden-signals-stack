# RB-LATENCIA - degradacao de latencia (p95/p99 acima do objetivo)

Familia: **latency**. Dono primario: time do servico que apareceu no label
`team` do alerta. Escopo: `LatencyP95AboveObjective`, `LatencyP99Critical`,
`LatencyDependencyDominant`, `SloLatencyBurnRateFast`, `SloLatencyBurnRateSlow`.

Tempo alvo deste runbook: 10 minutos para saber *onde* esta a cauda (nosso
processo, dependencia, ou capacidade), antes de discutir *por que*.

## Gatilho

Voce foi chamado por um destes caminhos:

- alerta `LatencyP95AboveObjective` (warning, 10m) ou `LatencyP99Critical`
  (critical, 5m) no Alertmanager, com `signal: latency` no payload;
- `SloLatencyBurnRateFast` - aqui a decisao nao e "se vai piorar", e quanto do
  budget de 14 dias ja foi;
- alguem do produto dizendo "esta lento" sem alerta - nesse caso confirme o
  alerta antes de investigar: se p95 esta plano, o problema nao e latencia do
  servico (veja RB-THROUGHPUT para queda de volume e RB-TAXA-ERRO para erro com
  aparencia de lentidao).

Primeiro movimento: abra o painel `Golden Signals - ambiente sintetico`
(uid `obs-golden-signals`) e olhe **p50 junto com p95**. p50 parado e p95
subindo e cauda (dependencia, lock, GC, retry). p50 subindo junto e custo
mediano de todo request - deploy, codigo, ou capacidade.

## Hipoteses em ordem de custo de verificacao

| # | Hipotese | Custo de checar | Sinal que confirma |
| --- | --- | --- | --- |
| 1 | E cauda ou e todo o request? | 30 s | p50 vs p95 no mesmo grafico |
| 2 | E um servico ou a jornada inteira? | 60 s | `journey` e `service` no mesmo painel |
| 3 | E a dependencia simulada (timeout/retry)? | 90 s | `LatencyDependencyDominant` + `service:dependency_latency_p95_5m` |
| 4 | E deploy recente? | 2 min | `app_info{version=...}` mudou na janela |
| 5 | E saturacao (CPU/fila/FD)? | 2 min | `service:process_cpu:cores5m`, `service:http_requests:queue_pressure` |
| 6 | E a esteira de telemetria mentindo (scrape atrasado)? | 3 min | `up == 0`, `scrape_duration_seconds` alto, staleness |
| 7 | E o gerador de carga sintetica (taxa/concorrencia)? | 3 min | `loadgen` report vs QPS medido |

Ordem tem proposito: 1 a 3 consomem so leitura de grafico e uma query. 4 a 7
pedem comando.

## Diagnostico

### 1. Confirmar que o sinal existe no Prometheus (promtool)

```bash
# p95 atual por servico - mesma serie do alerta, sem passar pela UI
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:http_request_duration_seconds:p95_5m'

# p50 e p99 juntos: separa "cauda" de "todo request"
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:http_request_duration_seconds:p50_5m'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))'

# a serie esta fresca mesmo? scrape atrasado produz grafico mentiroso
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'time() - max(scrape_timestamp) by (job)' 2>/dev/null || \
docker compose exec prometheus promtool query instant http://localhost:9090 'up'
```

Se o `promtool` acusar erro de autenticacao ou conexao, o ambiente nao esta de
pe: va para `RB-SATURACAO`, secao "esteira", porque alerta sem dado fresco e o
pior tipo de incidente.

### 2. Ver se a dependencia domina a latencia

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:dependency_latency_p95_5m / service:http_request_duration_seconds:p95_5m'
```

Acima de 0,6 a cauda nao e sua: e o `dependency` do label. No compose, a
dependencia e simulada com `time.sleep` - em ambiente real este e o ponto em
que se abre o painel do dono da dependencia, com o `trace_id` na mao.

### 3. Escolher uma amoza representativa com Tempo (curl + tempo-cli)

```bash
# traco mais lento da janela, direto pela API do Tempo
docker compose exec tempo curl -s \
  'http://tempo:3200/api/search?query=%7B%20.kind%20%3D%202%20%7D&limit=5&maxDuration=300s&minDuration=1s' \
  | python3 -c 'import json,sys; [print(t["traceId"], t.get("rootTraceName"), t.get("durationMs")) for t in json.load(sys.stdin).get("traces", [])]'

# se o seu tooling tiver tempo-cli, o mesmo resultado vem como
docker run --rm --network observability-golden-signals --entrypoint tempo-cli \
  docker.io/grafana/tempo:2.6.0 query --address http://tempo:3200 <trace_id>
```

Na UI: painel `Correlacao de incidente` (uid `obs-correlacao`), painel 4, que
lista os tracos por duracao. Clique no mais lento e olhe o **waterfall**: a
span filha `call <dependencia>` consome quanto do total? Se a resposta for "a
maior parte", a hipotese 3 e a conclusao.

### 4. Ver o que o processo pensava naquele momento (logcli / API do Loki)

```bash
# logcli dentro do container do Loki
docker compose exec loki logcli --address http://localhost:3100 query \
  '{service="checkout-api", environment="local-sintetico"} | json | duration_ms > 1200' --limit 20

# fallback pela API HTTP, sempre funciona sem depender de binario na imagem
curl -sG 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service="checkout-api"} | json | trace_id!=""' \
  --data-urlencode 'limit=20' --data-urlencode 'start='$(date -d '-10 minutes' +%s)'000000000' \
  --data-urlencode 'end='$(date +%s)'000000000' | python3 -m json.tool | head -40
```

O campo que interessa no log e `duration_ms` contra `outcome`:
`dependency_error` com `duration_ms` perto de `LATENCY_FACTOR*base + 400` e
timeout, e nao lentidao generica.

### 5. Descartar a esteira e o gerador de carga

```bash
# Collector exportando para Tempo com falha? entao o trace que voce nao ve existe
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'pipeline:otel_export_failures:rate5m'
docker compose logs --since 10m otel-collector | tail -40

# carga sintetica caiu? entao a cauda nova que voce ve e ruido de amostra pequena
docker compose logs --since 10m load-generator | tail -20
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:http_requests:rate5m'
```

`service:http_requests:rate5m` baixo e p95 alto e **quase sempre amostra
pequena**, nao degradacao. Esta e a hipotese que mais fazrollback errado.

## Acao

Nao ha acao automatica neste runbook. Cada item abaixo e uma mudanca de estado
do ambiente e, quando marcada, **exige aprovacao explicita de um segundo
humano** no canal do incidente antes de executar.

| Situacao confirmada | Acao | Aprovacao |
| --- | --- | --- |
| Deploy novo na janela, p50 e p95 subiram juntos | Rollback para a `version` anterior do `app_info` | **sim, obrigatoria** |
| Dependencia dominando com timeout | Habilitar degradacao de funcionalidade / desligar feature flag do caminho lento | **sim, obrigatoria** |
| `queue_pressure` acima de 1 com CPU plana no ambiente sintetico | Aumentar concorrencia/replicas do servico | **sim, obrigatoria** |
| CPU saturada no processo | Aumentar `limits.cpu`; depois investigar por que | **sim, obrigatoria** |
| Amostra pequena / gerador parado | Reanimar `load-generator`; nenhuma mudanca no servico | nao |
| Esteira com falha de export | Reiniciar Collector (`docker compose restart otel-collector`) e abrir issue | nao, mas registre |

Comando do compose, ambiente local:

```bash
docker compose restart otel-collector
docker compose up -d --scale load-generator=1 load-generator
docker compose restart sample-app        # destrutivo para o trace em curso: aprovacao
```

Em Kubernetes (mesmo contrato de tags, servicos reais), os comandos equivalentes:

```bash
kubectl -n obs get pods -l app.kubernetes.io/name=checkout-api -o wide
kubectl -n obs top pods -l app.kubernetes.io/name=checkout-api
kubectl -n obs rollout history deployment/checkout-api
kubectl -n obs rollout undo deployment/checkout-api          # destrutivo: aprovacao
kubectl -n obs delete pod <pod> --grace-period=30            # destrutivo: aprovacao
```

Depois de qualquer acao: anote `trace_id` do request que originou a decisao no
thread do incidente. Sem isso, a revisao nao consegue reconstruir o que voce
viu.

## Criterio de saida

Voce pode encerrar o incidente quando **todos** os itens abaixo estiverem
verificados, na ordem:

1. `service:http_request_duration_seconds:p95_5m` abaixo do objetivo declarado
   (`slo:latency:objective_seconds`) por 3 janelas consecutivas de avaliamento;
2. `slo:latency:burn_rate:1h` voltou para <= 1 (consumo sustentavel);
3. os alertas da familia `latency` sumiram do `ALERTS{alertstate="firing"}`:
   ```bash
   docker compose exec prometheus promtool query instant http://localhost:9090 \
     'count(ALERTS{alertstate="firing", signal="latency"})'
   ```
4. `p99/p95` voltou a razao habitual (a cauda sumiu, nao apenas o corte do
   alerta);
5. o erro nao migrou para outra familia (confira `service:http_error_ratio:rate5m`
   - latencia "resolvida" virando timeout e erro e incidente nao resolvido).

Comunicacao de saida: `o que estava lento, para quem, desde quando, o que foi
feito, e o que fica como follow-up`, com o `trace_id` do exemplo no lugar da
descrição.

## O que aprender depois

- Se a causa foi **dependencia dominando a cauda**: o ADR
  [`ADR-0002`](../docs/decisoes/ADR-0002-correlacao-por-trace-id.md) define por
  que a resposta nao e "logar mais", e sim manter o `traceparent` propagado para
  que o waterfall mostre o filho lento. Medida de acerto: `propagated=false` em
  queda no Loki.
- Se a causa foi **deploy novo**: `ADR-0003` (burn rate em vez de threshold
  fixo) - o alerta de 10m pode ter demorado; discuta janela mais curta para o
  fast burn no gate de release, nao um threshold fixo paralelo.
- Se voce **perdeu tempo distinguindo cauda de custo medio**: o painel 1 do
  `obs-golden-signals` deveria ter p50 desde o comeco; ajuste o painel via PR e
  nao pela UI (`allowUiUpdates: false` existe para isso).
- Se a **amostra pequena** te enganou: registre em `docs/custos.md` a relacao
  entre taxa de trafego sintetica e ruido do quantil, e considere `for: 10m`
  maior para o alerta de p95 nesse servico especifico - por servico, nao global.
- Se a **esteira falhou** (export com erro, scrape atrasado): va para
  `RB-SATURACAO` e trate telemetria como servico critico, com alerta proprio.
