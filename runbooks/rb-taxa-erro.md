# RB-TAXA-ERRO - erro acima do budget (5xx, burn rate de disponibilidade)

Familia: **errors**. Dono primario: time do servico no label `team`. Escopo:
`ErrorRatioHigh`, `ErrorRateCritical`, `DependencyErrorRatioHigh`,
`SloAvailabilityBurnRateFast/Medium/Slow`, `SloErrorBudgetExhausted`.

Regra de ouro que este runbook aplica: **erro de cliente (4xx) nao e
indisponibilidade**. Se o painel de erro subiu, a primeira decisao e de qual
classe se trata, porque o caminho de acao e completamente diferente.

## Gatilho

- `SloAvailabilityBurnRateFast` (14,4x em 1h + 6x em 5m) - pagina agora;
- `SloAvailabilityBurnRateMedium` (6x em 6h + 3x em 5m) - pagina, mas com
  chance de ser degradacao parcial;
- `ErrorRatioHigh` (warning) / `ErrorRateCritical` (critical) - sinal bruto,
  sem SLO declarado; use este quando o servico ainda nao tem objetivo
  formalizado;
- `DependencyErrorRatioHigh` - provavel chamada a servico de fora;
- chamado humano com `X-Request-Id` - siga o fluxo do identificador, nao a
  descricao subjetiva do sintoma.

Abra o painel `SLO e Error Budget - ambiente sintetico` (uid
`obs-slo-budget`) e depois `Correlacao de incidente` (uid `obs-correlacao`).
Voce precisa responder tres perguntas em ordem: *quanto do budget ja foi*,
*em qual rota*, *e em qual dependencia*.

## Hipoteses em ordem de custo de verificacao

| # | Hipotese | Custo | Confirmacao |
| --- | --- | --- | --- |
| 1 | E 5xx nosso ou 4xx do cliente? | 30 s | `route:http_error_ratio:rate5m` por `status_class` |
| 2 | E um servico ou a jornada? | 60 s | label `journey` no mesmo painel |
| 3 | Foi um deploy na virada da curva? | 90 s | `count by (version) (app_info)` no tempo T |
| 4 | E dependencia (502/timeout) ou nosso (503/500)? | 90 s | `outcome` e `error_type` no log |
| 5 | E saturacao virando erro? | 2 min | `queue_pressure`, CPU, `process_open_fds` |
| 6 | E o budget que acabou por consumo acumulado, nao por evento novo? | 2 min | burn rate 14d vs 1h |
| 7 | E a esteira (scrape/parse) gerando falso 5xx? | 3 min | `up`, `job:scrape_up:ratio`, `pipeline:loki_append_failures:rate5m` |

A hipotese 6 e a que mais muda a decisao: **budget esgotado nao e incidente, e
politica de release**. Voce nao "apaga" budget estourado com acao no ambiente.

## Diagnostico

### 1. Separar classe de status antes de qualquer coisa

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'sum by (service, status_class) (rate(http_requests_total[5m]))'

docker compose exec prometheus promtool query instant http://localhost:9090 \
  'route:http_error_ratio:rate5m'
```

Se o `status_class` dominante for `4xx`, encerre a parte de incidente e abra
um problema de UX/contrato de API: o alerta nao deveria ter paginado - verifique
se alguem colocou 4xx no `expr` (o `golden-signals.yml` exclui de proposito).

### 2. Queimar a hipotese de deploy

```bash
# qual versao estava no ar ha 1h e qual esta agora
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'count by (version, service) (app_info)'
docker compose exec prometheus promtool query range --since=2h http://localhost:9090 \
  'count by (version) (app_info)' | head -20
```

Duas `version` coexistindo na janela e rollback ou rollout progressivo
parado. Nesse caso o proximo passo e por rota, porque o erro costuma morar em
uma rota especifica do build novo.

### 3. Do agregado ao request individual

```bash
# rota/status que mais contribuem agora
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'topk(10, route:http_errors:rate5m)'

# pegar um trace_id de erro real no Loki (campo que a app escreve sempre)
docker compose exec loki logcli --address http://localhost:3100 query \
  '{service="checkout-api", environment="local-sintetico"} | json | level="error" | error_type!=""' \
  --limit 10 | head -30

# mesmo resultado sem logcli, pela API HTTP
curl -sG 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service=~"checkout-api|auth-api"} | json | status >= 500' \
  --data-urlencode 'limit=10' --data-urlencode 'start='$(date -d '-15 minutes' +%s)'000000000' \
  --data-urlencode 'end='$(date +%s)'000000000' \
  | python3 -c 'import json,sys; [print(v[0], v[1][:220]) for s in json.load(sys.stdin)["data"]["result"] for v in s["values"]]'
```

Copie um `trace_id` de 32 hex e va para o proximo passo. Se o `error_type` for
`dependency_timeout`, voce ja sabe que o proximo telefone e de outro time.

### 4. Ver a arvore do erro no Tempo

```bash
docker compose exec tempo curl -s 'http://tempo:3200/api/trace/<trace_id_hex>' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(s["name"], s["status"]["code"], s["durationNano"]//1000000, "ms") for b in d.get("batches",[]) for s in b.get("scopeSpans",[{}])[0].get("spans",[])]'

# alternativa com a API v2 e ordenacao por erro
curl -sG 'http://localhost:3200/api/search' --data-urlencode 'query={ status.code = 2 }' \
  --data-urlencode 'limit=20' --data-urlencode 'maxDuration=300s' | python3 -m json.tool | head -30

# e, se o binario estiver disponivel no seu host:
docker run --rm --network observability-golden-signals --entrypoint tempo-cli \
  docker.io/grafana/tempo:2.6.0 analyse traces --address http://tempo:3200 --since=15m
```

O que voce esta procurando no waterfall: o span com `error.type` marcado e se
ele e SERVER (seu codigo) ou CLIENT (dependencia). `kind = 2` e server,
`kind = 3` e client - o ADR-0002 existe para que esta distincao seja possivel
sem ler log de dez servicos.

### 5. Confirmar que nao e saturacao disfarcada

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:http_requests:queue_pressure'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:process_open_fds:ratio'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'job:scrape_up:ratio'
```

`queue_pressure` alto + 503 + p95 alto e capacidade. Siga para
`RB-SATURACAO` a partir da secao de acao, mantendo o `trace_id` que voce ja
tem.

## Acao

Nenhuma acao aqui roda sozinha. Itens marcados com **aprovacao obrigatoria**
exigem um segundo humano confirmando no canal do incidente, com o `trace_id` de
exemplo na mensagem.

| Causa confirmada | Acao | Aprovacao |
| --- | --- | --- |
| Erro novo apos deploy, em uma rota | Rollback do deploy (`kubectl rollout undo` / `docker compose up -d --force-recreate sample-app`) | **obrigatoria** |
| Dependencia externa com 502/timeout | Acionar o time dono, ativar cache/degraded mode, **desligar retry agressivo** | **obrigatoria** |
| Budget esgotado sem evento novo | Congelar release do servico ate a janela de 14d rolar; comunicar explicitamente | **obrigatoria** (impacta roadmap) |
| Saturacao | Escalar replicas/CPU antes de mexer em codigo | **obrigatoria** |
| Falso positivo de 4xx no alerta | Corrigir `expr` via PR com teste no rule file; nao silenciar o alerta | nao |
| Esteira com perda (`pipeline:otel_export_failures:rate5m` alto) | Reiniciar Collector e tratar como incidente de observabilidade | nao, mas registre |

```bash
# ambiente local (compose)
docker compose restart sample-app                    # destrutivo: aprovacao obrigatoria
docker compose up -d --build sample-app              # rollback de imagem: aprovacao
docker compose exec alertmanager amtool silence add alertname=ErrorRateCritical \
  --alertmanager.url=http://localhost:9093 --duration=30m --comment="incidente #sintetico, causa em analise"

# kubernetes
kubectl -n obs rollout undo deployment/checkout-api                  # aprovacao
kubectl -n obs rollout history deployment/checkout-api
kubectl -n obs get events --sort-by=.lastTimestamp | tail -20        # leitura, sem risco
```

O `amtool silence add` e uma acao de **higiene, nao de resolucao**: silencio com
`--duration` curto, comentario ligando ao incidente, e um ticket para remover o
silencio. Silencio longo sem dueno e o jeito mais seguro de perder um incidente
de verdade.

## Criterio de saida

1. `service:http_error_ratio:rate5m` por servico voltou ao regime anterior ao
   evento por 3 janelas consecutivas;
2. `slo:availability:burn_rate:1h <= 1` (consumo sustentavel de volta);
3. nenhum alerta `signal=errors` em `firing`:
   ```bash
   docker compose exec prometheus promtool query instant http://localhost:9090 \
     'count(ALERTS{alertstate="firing", signal="errors"})'
   ```
4. as silences criadas foram expiradas ou removidas (`amtool silence query`);
5. o erro **nao migrou** de familia: confira `service:http_request_duration_seconds:p95_5m`
   - resolver 503 cortando timeout sem capacidade nova so empurra o problema.

## O que aprender depois

- **Budget esgotado sem incidente** e o gatilho classico para discutir o objetivo,
  nao o alerting: `ADR-0003` (burn rate em vez de threshold fixo) tem a secao
  "como medir que acertou" exatamente para essa conversa.
- Se voce levou tempo demais para separar **erro nosso de dependencia**:
  `ADR-0002` (correlacao por `trace_id`) - a medida de acerto e o tempo entre
  "pegar um trace_id" e "saber o nome do servico culpado" no proximo incidente.
- Se o alerta paginou por **4xx**: o `expr` de `ErrorRatioHigh` e o contrato;
  qualquer PR que mude a definicao de "request bom" mexe em todos os SLOs e
  precisa de revisao do time de SRE, documentado em `CONTRIBUTING.md`.
- Se o erro era **saturacao mal classificada**: `ADR-0004` e o espaco para
  discutir se label de rota em excesso mascara o sinal, e `docs/custos.md` tem a
  conta de cardinalidade.
