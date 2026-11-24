# RB-THROUGHPUT - traffic fora do regime (queda, ausencia ou pico)

Familia: **traffic**. Dono primario: time da jornada que apareceu no label
`journey` - e nao necessariamente o time do servico que sumiu do grafico.
Escopo: `TrafficDropAgainstBaseline`, `TrafficZeroWithRecentHistory`,
`TrafficSurge`.

Traffic e o sinal mais facil de interpretar errado. Queda a zero quase nunca e
"ninguem esta usando": ou a borda parou de entregar, ou a metrica parou de
chegar. Aumento subito quase nunca e sucesso de marketing: ou e retry de
cliente com timeout curto, ou e scraper, ou e um reprocessamento em lote.

## Gatilho

- `TrafficZeroWithRecentHistory`: **pagina**. Serie existia e virou zero; ou o
  servico parou de receber, ou o scrape parou de coletar. As duas sao incidentes;
- `TrafficDropAgainstBaseline`: queda sustentada contra a basica da mesma hora
  nos dias anteriores (a regra compara `rate(...[1h])` com o mesmo periodo
  defasado de 24h e 48h);
- `TrafficSurge`: acima de 3x a basica, com risco de virar saturacao - o
  proximo alerta desta lista costuma ser `SaturationQueuePressureCritical`.

Nenhum dos tres fala de erro nem de latencia. Por isso o primeiro movimento e
sempre cruzar com as outras familias antes de concluir qualquer coisa.

## Hipoteses em ordem de custo de verificacao

| # | Hipotese | Custo | Confirmacao |
| --- | --- | --- | --- |
| 1 | O alerta e de ausencia de serie, nao de valor zero? | 30 s | `absent()` no alerta + `up` do job |
| 2 | O gerador de carga (ou o cliente) parou? | 60 s | `docker compose ps` e log do `load-generator` |
| 3 | E a borda (proxy/gateway/DNS) ou o servico? | 90 s | `curl` direto no container vs pela porta do host |
| 4 | E uma jornada especifica ou todas? | 90 s | `service:http_requests:rate5m` por `journey` |
| 5 | E deploy novo que mudou o nome do metric/label? | 2 min | `app_info`, `count by (version)` |
| 6 | E retry amplificado (surge com erro junto)? | 3 min | ratio entre QPS do servico e do vizinho |
| 7 | E scrape atrasado/perdido no Collector? | 3 min | `job:scrape_up:ratio`, `pipeline:otel_enqueue_failures:rate5m` |

A hipotese 1 e a mais barata e a mais comum: quem escreve `absent()` em rule
file ja perdeu tempo demais distinguindo "zero" de "sem dado".

## Diagnostico

### 1. A serie existe? (promtool, antes de qualquer teoria)

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:http_requests:rate5m'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'job:scrape_up:ratio'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'count by (service, journey) (http_requests_total)'

# o ultimo ponto de dado de cada servico - se estiver atrasado, e esteira
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'timestamp(http_requests_total)'
```

`count by (service)` menor que o esperado significa serie sumida, e serie
sumida no meio do dia e quase sempre **mudanca de label** (deploy novo com
`journey` vazio, `service` renomeado) - hipotese 5.

### 2. Borda ou servico?

```bash
# direto no container do servico (rede interna do compose)
docker compose exec sample-app python3 -c \
  "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/health', timeout=3).status)"
# pela porta publicada, ou seja, atravessando o proxy do Docker
curl -s -o /dev/null -w 'host->sample-app %{http_code} em %{time_total}s\n' http://localhost:8082/health
# e o tráfego real que o servico viu, na janela
curl -sG 'http://localhost:3100/loki/api/v1/query' \
  --data-urlencode 'query=sum(rate({service="checkout-api"} | json | duration_ms > 0 [5m]))'
```

Os dois primeiros deram 200 e o Prometheus nao ve request? Entao o servico
esta rodando e o caminho de telemetria esta morto - hipotese 7, e este e o unico
caso em que o incidente e de observabilidade e nao de produto.

### 3. A jornada ou o servico?

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'sum by (journey) (service:http_requests:rate5m)'
docker compose exec prometheus promtool query range --since=3h --step=5m http://localhost:9090 \
  'sum by (journey) (service:http_requests:rate1m)' | tail -15
```

Jornada `checkout` caiu e `login` esta normal: o gargalo esta no passo comum
(`auth-api`) ou no passo exclusivo (`/pay`). Aqui vale a pena ler o painel 8 do
`obs-golden-signals`, que e exatamente esta serie.

### 4. Verificar o gerador de carga sintetica

```bash
docker compose ps load-generator load-generator-login load-generator-background
docker compose logs --since 15m load-generator | grep -E "resumo|launched" | tail -10
docker compose logs --since 15m load-generator | python3 -c '
import sys, json
for line in sys.stdin:
    if line.startswith("{"):
        d = json.loads(line)
        if "jornadas_por_s" in d:
            print(d["timestamp"], d["jornadas_por_s"], d.get("error_rate"), d.get("connection_errors"))
'
```

`connection_errors` crescendo no log do gerador, com QPS caindo no Prometheus, e
a assinatura de "o loadgen nao esta conseguindo conectar" - que pode ser o
servico, a rede do Docker, ou o `TIMEOUT` configurado abaixo da latencia atual.

### 5. Confirmar se o surge e retry

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'sum by (service, journey) (service:http_requests:rate1m)'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'sum by (service) (service:http_error_ratio:rate5m)'
docker compose exec loki logcli --address http://localhost:3100 query \
  '{service="checkout-api"} | json | outcome="dependency_error"' --limit 10

# um trace do surge: mesmo request_id aparecendo em saltos diferentes?
curl -sG 'http://localhost:3200/api/search' --data-urlencode 'query={ .kind = 2 }' \
  --data-urlencode 'limit=5' --data-urlencode 'maxDuration=60s' | python3 -m json.tool | head -20
```

`request_id` repetido com `trace_id` diferente e o padrao de cliente com retry
agressivo: cada retry ganha trace novo, entao o volume sobe e a latencia media
parece "melhorar" porque os requests curtos (cacheados) sao maioria.

## Acao

| Causa | Acao | Aprovacao |
| --- | --- | --- |
| Serie sumida apos deploy (label mudou) | Reverter `SERVICE`/`JOURNEY`/metric name **no deploy** ou ajustar recording rule via PR | **obrigatoria** |
| Esteira de telemetria parada | `docker compose restart otel-collector` / verificar `--web.enable-remote-write-receiver` | nao |
| Loadgen parado | `docker compose up -d load-generator` | nao |
| Borda (proxy/gateway/DNS) | Acionar o time de borda; **nao escalar o servico** - ele nao esta recebendo | nao |
| Retry amplificado | Aumentar `backoff`/desligar retry automatico no cliente, ou adicionar idempotencia | **obrigatoria** |
| Surge legitimo de pico | Escalar horizontal antes de chegar em `queue_pressure > 1` | **obrigatoria** |
| Tráfego de scraper/bot indesejado | Rate limit na borda por rota; comunicar antes de bloquear | **obrigatoria** |

```bash
# ambiente local
docker compose up -d load-generator
docker compose restart otel-collector
docker compose logs -f --tail 20 load-generator
# kubernetes: escalar sem mexer em codigo
kubectl -n obs scale deployment checkout-api --replicas=6        # aprovacao
kubectl -n obs get hpa -w
kubectl -n obs rollout undo deployment/checkout-api              # aprovacao
```

Para o caso de retry: nenhum aumento de capacidade resolve amplificação de
cliente. Se voce escalar 3x e o problema era retry, vai pagar 3x e continuar
com o mesmo erro. Esta ordem de decisao esta em `docs/custos.md`.

## Criterio de saida

1. `service:http_requests:rate5m` por jornada de volta à banda histórica
   (dentro de 30% da basica de 24h/48h) por 2 janelas de 15 minutos;
2. `job:scrape_up:ratio == 1` e `count by (service) (http_requests_total)` com o
   numero esperado de servicos - ou seja, o sinal voltou **porque o dado
   voltou**, nao porque alguem editou o alerta;
3. se foi surge, a taxa voltou a <= 1,5x a basica e `queue_pressure < 1`;
4. `service:http_error_ratio:rate5m` nao subiu durante a intervencao;
5. registro no incidente de qual era a causa: borda, telemetria, carga sintetica
   ou demanda real. As quatro levam a acoes diferentes de longo prazo.

## O que aprender depois

- Se a causa foi **label que mudou no deploy**: `ADR-0004` (tags padronizadas).
  A medida de acerto e simples: quantos alertas de ausencia de serie existiram
  nos ultimos 90 dias. Se for maior que zero, o schema em `app/tagging.py` nao
  esta sendo validado no CI de quem publica.
- Se foi **retry amplificado**: o ADR-0002 (propagacao de contexto) ajuda porque
  `X-Request-Id` repetido com `trace_id` diferente e exatamente o que torna o
  padrao detectavel em minutos.
- Se foi **telemetria e ninguem percebeu**: cada alerta `Infra*` de
  `infrastructure.yml` deveria ter impedido isso; discuta cobertura de scrape e
  os self-metrics do Collector.
- Se foi **demanda real** e voce nao tinha basica: o proximo passo e alerta de
  capacity planning, nao de incidente - e ele precisa de janela maior que 1h.
