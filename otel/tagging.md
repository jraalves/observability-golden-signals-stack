# Esquema de tags: aplicação, jornada, ambiente e dono

Este documento é a definição do contrato de telemetria do stack. Ele vem antes
das regras de alerta e antes dos painéis: sem tag estável, `expr` bom não
existe, porque a série de ontem tem outro nome hoje.

A regra prática que uso: **toda série temporal precisa responder quatro
perguntas sem abrir o código** — quem é o serviço, qual jornada ele serve, em
que ambiente está e quem responde por ele. O resto é detalhe de investigação.

## 1. Atributos de resource (OTel) e labels (Prometheus)

| Chave OTel (resource) | Label Prometheus | Exemplo sintético | Origem |
| --- | --- | --- | --- |
| `service.name` | `service` | `checkout-api` | env `SERVICE` |
| `service.version` | `version` (só em `app_info`) | `1.4.2-sintetico` | env `VERSION` |
| `deployment.environment.name` | `environment` | `local-sintetico` | env `ENVIRONMENT` |
| `journey` (atributo custom) | `journey` | `checkout` | env `JOURNEY` |
| `team` (atributo custom) | `team` | `payments-core` | env `TEAM` |
| `owner` (atributo custom) | `owner` (só em `app_info`) | `pagamentos-dev` | env `OWNER`, default `<team>-oncall` |
| `cloud.region` | `region` (só em `app_info`) | `local-1` | env `REGION` |

Chaves de alto cardinalidade (`route`, `status`, `dependency`, `status_class`)
ficam **nas métricas de request**, nunca no resource. Colar `route` no resource
do OTel é o jeito mais rápido de multiplicar a quantidade de `time series` por
acidente, porque resource attribute vai junto com cada span e cada metric
exportada.

Validação em runtime: `app/tagging.py` define o schema e `validate_tags()` é
chamada em cada linha de log e em cada pipeline do Collector. O Collector
rejeita e conta (`otlp_receiver_refused_logs_total`) em vez de ingestar série
fora do padrão — série fora do padrão é caro duas vezes: no armazenamento e na
triagem.

## 2. Por que `journey` existe como dimensão de primeira classe

`service` responde "qual processo está ruim". `journey` responde "qual
promessa ao usuário está quebrada". São perguntas diferentes, e a segunda é a
que decide prioridade.

Exemplo sintético do ambiente local: uma jornada `checkout` é
`auth-api /auth` → `checkout-api /pay`. Se o `auth-api` degrada, o `checkout`
morre inteiro e os dois serviços continuam "saudáveis" olhando por serviço. Sem
a dimensão `journey`, o alerta `LatencyP95AboveObjective` acende para um serviço
e o oncall gasta 20 minutos provando que o cliente afetado é outro.

A etiqueta é um slug controlado (`login`, `checkout`, `descoberta`,
`notificacao`). A lista vive em `app/tagging.py::KNOWN_JOURNEYS`; jornada nova
entra por PR, porque dimensionar livremente é o mesmo erro de cardinalidade de
um label `usuario_id`.

## 3. Correlação: `trace_id` é o join, `X-Request-Id` é o recibo

Aqui está o coração do stack. Três backends separados só viram uma história se
existe uma chave que os três aceitam.

- **`trace_id` (W3C `traceparent`, 16 bytes)** é a chave de join causal. Ela é
  gerada uma vez na borda, propagada em cada salto e escrita em **todo** log,
  em **todo** span e, quando necessário, em métrica com exemplar. Com ela:
  Loki → Tempo é um clique (`derivedFields`), Tempo → Loki é uma busca por
  atributo, e Grafana mostra as três vistas no mesmo intervalo de tempo.
- **`X-Request-Id`** é o recibo do cliente. Ele sobrevive ao que o `trace_id` nao sobrevive: retries do SDK com novo trace, jobs em lote que
  processam várias requisições de uma vez, chamadas que nem chegaram a gerar
  span (proxy 413, WAF), e a conversa com quem abriu o chamado — que cita o id
  que apareceu na tela, não 32 hex digits.

Na prática os dois campos ficam juntos no log, e o runbook manda pesquisar
primeiro por `X-Request-Id` (para achar o evento) e depois por `trace_id` (para
ver a árvore). Um sem o outro sempre vira busca por texto, e busca por texto
entre serviços é o que faz incidente durar hora em vez de minuto.

Propagação no ambiente local: `app/loadgen.py` gera um `trace_id` por jornada e
usa o `span_id` retornado pelo serviço anterior como pai do próximo salto. É
por isso que o painel de correlação mostra duas spans em uma mesma árvore sem
nenhum SDK no meio.

### Formato dos campos no log

```json
{"timestamp":"2026-11-24T11:58:41.207Z","level":"error","service":"checkout-api",
 "environment":"local-sintetico","journey":"checkout","team":"payments-core",
 "owner":"pagamentos-dev-oncall","message":"POST /pay","request_id":"9f2c...",
 "trace_id":"7c1d...32hex","span_id":"1a2b...16hex","parent_span_id":"...",
 "route":"/pay","status":502,"status_class":"5xx","duration_ms":1043.2,
 "outcome":"dependency_error","error_type":"dependency_timeout",
 "dependency":"gateway-pagamentos","propagated":true}
```

`propagated:false` é o marcador mais útil do log. Ele significa "cheguei sem
`traceparent` válido", e é a métrica que mostra o buraco real da esteira — um
serviço que não propaga contexto quebra a correlação de tudo que está abaixo
dele. Um alerta de `propagated=false` em alta taxa vale mais que dez painéis de
latência.

## 4. Regras que o Collector aplica

`otel/collector-config.yaml` faz o trabalho pesado para o esquema valer sem
depender de disciplina individual:

1. `memory_limiter` primeiro, sempre; depois `attributes/*` e `resource/*`.
2. Normaliza `service.name` e injeta `journey`, `team`, `environment` quando o
   SDK esqueceu, lendo de env do processo (a app de exemplo já injeta; em app
   real o Collector conserta o que faltou).
3. **Reduz** atributos que não precisam virar label: `http.url` vira
   `http.route` (agrupado), `user_agent.original` é descartado.
4. Rejeita logs sem `trace_id` quando o pipeline é de request (`filter` com
   conta de rejeição, não `drop` silencioso).
5. Renomeia para o vocabulário do Prometheus: `service.name` → `service`,
   `deployment.environment.name` → `environment`.

## 5. O que muda no custo

Dimensão tem preço. Os números e a conta em aritmética explícita estão em
[`docs/custos.md`](../docs/custos.md). A regra de bolso que aplico aqui:
**novo label em métrica de request passa por revisão de cardinalidade; novo
atributo em span não passa, mas precisa de TTL curto no Tempo.**
