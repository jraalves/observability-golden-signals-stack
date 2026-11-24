# ADR-0004: tags padronizadas por aplicação e jornada, com lista explícita de labels proibidos

Status: aceito
Data: 2026-06-15
Decisao aceita em: 2026-06-22
Decisor: autor (SRE) com os times que publicam telemetria
Contexto relacionado: `app/tagging.py`, `otel/tagging.md`,
`otel/collector-config.yaml`, `docs/custos.md`, `runbooks/rb-saturacao.md`,
`runbooks/rb-throughput.md`

## Contexto

Um stack de observabilidade não falha por falta de dado: falha por dado com
nome errado. Os quatro sintomas que esta decisão combatem, todos vistos em
ambiente que opera Prometheus mais Loki mais Tempo juntos:

1. **Painel vazio depois de deploy.** O label `service` mudou de
   `checkout-api` para `checkout_api` num refactor de variavel de ambiente, e o
   `sum by (service, journey)` parou de casar com a recording rule.
2. **Saturação de TSDB por criatividade.** Uma `route` com o path completo
   (`/pay?id=91827`) transforma cada request em uma serie nova. O Prometheus
   morre de sucesso: tudo foi gravado, nada consulta.
3. **Busca de log lenta e cara.** Label de stream com conteúdo de mensagem faz o
   índice do Loki crescer com o texto, e a consulta que precisa de 15 minutos de
   janela varre o cluster inteiro.
4. **Correlação impossível.** Metrica diz `service=checkout-api`, log diz
   `app=checkout`, span diz `service.name=checkout-api-v3`. Três sistemas
   corretos, nenhuma junção possível.

A alternativa descartada era a mais comum: um documento de convenção em wiki,
revisado em code review por boa vontade. A taxa de conformidade disso é
próxima de zero depois da primeira sprint - não porque o time seja descuidado,
mas porque ninguém escreve telemetria pensando no custo de índice de terceiros.

## Decisao

O esquema de tags passa a existir **em código, versionado no repositório**, em
`app/tagging.py`, e é a única fonte da verdade:

- `REQUIRED_TAGS = (service, environment, journey, team)`: o par mínimo que
  permite cruzar os três sinais. Uma aplicação que não entrega os quatro não é
  on-bordada no stack.
- `JOURNEYS = (checkout, login, descoberta, notificacao)` com
  `normalize_journey()` fechando o vocabulário em `outra`. Jornada é dimensão de
  negócio e por isso tem conjunto finito; texto livre aqui é custo de índice.
- `FORBIDDEN_METRIC_LABELS`: `trace_id`, `request_id`, `user_id`, `customer_id`,
  `pod`, `pod_ip`, `http.url`, `http.target`, `sql.statement`, `error.message`,
  `session`. Alto cardinalidade por construção: cada valor novo é uma serie na
  memoria do Prometheus.
- `FORBIDDEN_LOG_STREAM_LABELS` é o conjunto acima mais `message`, `body` e
  `stacktrace`. Conteúdo de linha não é indexado como label de stream: o
  `trace_id` fica **no corpo** do log e é extraído pelo pipeline.
- `validate_tags()` e `cardinality_risk()` são chamados pela aplicação a cada
  registro emitido (`app/server.py`, `Logger.emit`) e pelos testes. O Collector
  repete o corte com `attributes` e `span` processors em
  `otel/collector-config.yaml`.

Duas decisões derivadas, explicitadas porque são as que geram discussão:

- **`trace_id` é campo de log, não label de métrica nem label de stream.** Ele é
  o join entre os três sinais e por isso precisa estar no corpo do log
  (extraível por parser e linkável pelo `derivedFields` do Grafana), mas jamais
  multiplicar series.
- **Rotulo livre passa por `labelize()`** antes de virar label: minúsculo, sem
  caractere especial, colapsado em slug. O custo de um slug é previsível; o
  custo de string de usuário não é.

## Consequencias

Boas:

- Adicionar um label deixa de ser decisão invisível: mexe em `app/tagging.py`,
  aparece no diff, e os testes de `tests/test_tagging.py` falham se o vocabulário
  for violado.
- Os painéis sobrevivem a rename: o `by (service, journey)` é garantido pelo
  schema, não pela sorte.
- A conta de custo de `docs/custos.md` fica auditável, porque o número de valores
  possíveis por label é conhecido.
- A correlação log-para-trace funciona em um clique no Grafana, o que reduz o
  tempo de triagem medido no `runbooks/rb-latencia.md`.

Custosas:

- Perda seletiva de detalhe. Um `http.target` completo é útil para debugar um
  request; aqui ele mora no log e no atributo do span, não na métrica. Essa é a
  troca inteira da decisão: métrica agregada barata, detalhe no log.
- `journey = outra` precisa ser monitorado. Se a taxa de `outra` subir, o time
  inventou jornada nova sem atualizar o vocabulário - o painel 8 do
  `golden-signals.json` mostra a distribuição e é o lugar de olhar.
- `validate_tags` roda no caminho quente do log e retorna lista de avisos em vez
  de lançar exceção. Um registro fora do schema nunca derruba um request; ele
  chega com `tag_schema_warnings` e vira bug do produtor do log.
- O Collector precisa ficar em sincronia com o módulo Python. É um ponto único
  de falha de processo: `tests/test_tagging.py` confere as chaves, e mudar um
  dos dois lados sem o outro quebra o CI.

## Como medir que acertou

1. **Serie ativa por label.** A pergunta do runbook de saturação:
   ```bash
   curl -s 'http://localhost:9090/api/v1/label/__name__/values' >/dev/null
   promtool query instant http://localhost:9090 \
     'count({__name__=~"http_requests_total|http_request_duration_seconds_bucket"}) by (job)'
   ```
   Criterio: `count(prometheus_tsdb_head_series)` não cresce degrau depois de
   deploy que não mudou trafego.
2. **Alerta de ausencia de serie.** Contar `TrafficZeroWithRecentHistory` e
   `InfraTargetDown` causados por label rename nos últimos 90 dias. Criterio:
   zero. Se for maior que zero, o schema não está sendo validado no CI de quem
   publica.
3. **Cobertura do vocabulário:** fração de logs com `journey != "outra"`.
   ```text
   sum(rate(...)) by (journey)   # painel 8
   ```
   medido no Loki com `logcli`, porque `journey` de log não é label de métrica:
   ```bash
   logcli -q '{service="checkout-api"} | json | journey="outra"' --limit 1000
   ```
   Criterio: menos de 10% em `outra`; acima disso, revisar `JOURNEYS`.
4. **Rejeição no Collector:** `otel_collector_refused_data_points_total` e
   `otelcol_processor_drop_data_points` em `http://localhost:8889/metrics`.
   Criterio: nenhum descarte por atributo proibido depois da primeira semana de
   adoção por serviço.
5. **Custo real:** comparar o crescimento semanal de disco do Prometheus com a
   projeção de `docs/custos.md`. Desvio acima de 2x indica label novo entrando
   sem revisão.
