# ADR-0001: Tempo como backend de trace, em vez de Jaeger + Cassandra

Status: aceito
Data: 2026-05-11
Decisao aceita em: 2026-05-18
Decisor: autor (SRE), revisado com o time que operaria a stack
Contexto relacionado: `jaeger/README.md`, `otel/collector-config.yaml`,
`docker-compose.yml`

## Contexto

Precisamos de backend de trace com estas condicoes, na ordem em que pesaram na
decisao:

1. **100% open source**, sem recurso pago escondido na arquitetura. Esse e um
   requisito do projeto, nao preferencia: a stack tem que poder ser replicada
   por quem nao tem orcamento de observabilidade.
2. O time de oncall e pequeno. Cada componente de infraestrutura com estado
   adiciona rotinas de operacao: upgrade, backup, planejar capacidade,
   discussao de fragmentacao. A conta real de "adotar X" e "quantas conversas
   novas por trimestre".
3. Log ja estava resolvido com Loki, com chunk em object storage/filesystem e
   retencao por `table_manager`. Reuso de modelo de armazenamento tem valor
   operacional concreto: um bucket, uma politica de lifecycle, um tipo de
   incidente de disco.
4. Precisamos de **service graph** (quem chama quem, com que taxa de erro) sem
   instalar um segundo sistema.
5. Precisamos de busca por atributo com custo previsivel. O modelo historico de
   "indexar toda tag" produz um indice que cresce com a criatividade dos
   desenvolvedores, e o custo aparece em momento inoportuno.

As opcoes que estavam na mesa de verdade:

| Opcao | Estado adicional | Busca | Custo de indice |
| --- | --- | --- | --- |
| Jaeger all-in-one + memory | nenhum | fraca, volatil | n/a |
| Jaeger + badger | um node local | limitada | baixo |
| Jaeger + Elasticsearch | cluster de busca | boa | alto e visivel |
| Jaeger + Cassandra | cluster distribuido | boa, com `index tag` | alto, e voce opera Cassandra |
| Tempo + filesystem/S3 | zero alem do storage | TraceQL | controlado por atributo indexado |

## Decisao

Usar **Grafana Tempo** como backend primario de trace no stack, com
`storage.trace.backend: local` no ambiente de estudo e caminho de producao em
object storage (S3 compativel), mantendo o **Jaeger all-in-one de pe no compose
apenas para comparacao**, sem receber span em producao.

Consequencias diretas desta escolha, especificas o suficiente para auditar:

- O pipeline de traces do Collector exporta para `otlp/tempo` (`tempo:4317`).
- `query_frontend.traceql_2.engine: parca` em `tempo/trace-frontend.yaml`, com
  `search.duration_slo` explícito, porque busca de trace sem SLO de duracao vira
  OOM no query-frontend.
- O `metrics_generator` do Tempo fica ligado e exporta para o Prometheus por
  remote write: e dele que vem `trace_service_graph_requests_total`, usada no
  painel de correlacao.
- A retencao de trace fica em `compactor.compaction.block_retention: 48h` -
  deliberadamente menor que a de log, e isso e uma troca explicita (abaixo).

Nao decidimos usar o Alloy nem o agente de cluster do Grafana: menos um runtime
para explicar. Nao decidimos usar o Jaeger em modo `memory` como secundario
permanente em producao: so no ambiente local.

## Consequencias

**Positivas**

- Um unico modelo de storage para log e trace: mesmo bucket, mesma politica de
  retencao, mesmo tipo de incidente de disco.
- TraceQL permite filtrar por atributo sem indexar tudo; a politica de tags
  (ADR-0004) e o que controla o custo, nao o oposto.
- Service graph sai do proprio backend, sem Jaeger dedicado para topologia.
- Grafana como superficie unica: Loki -> Tempo por `derivedFields` em um clique,
  o que reduz o tempo de triagem medido no runbook de latencia.

**Negativas (e o que fizemos a respeito)**

- **Perdemos maturidade de UI do Jaeger** para comparar dois execucoes de um
  mesmo trace. Mitigacao: manter Jaeger no compose e o caminho de troca de
  exporter documentado em `jaeger/README.md`.
- **TraceQL e menos conhecido** pela comunidade; a curva aparece na primeira
  semana. Mitigacao: os runbooks trazem a query pronta, com fallback por HTTP
  API quando o tooling nao esta instalado.
- **Retencao curta de trace** significa que incidente antigo fica sem arvore:
  48h de trace contra 14 dias de log e metrica. Mitigacao: o log carrega
  `trace_id`, `span_id` e `outcome`, que e o suficiente para reconstruir a
  historia; e a decisao de retencao fica visivel em `docs/custos.md` em vez de
  implicita.
- **Um fornecedor a menos para negociar** em cenario de suporte. Nao e um problema
  tecnico, mas em ambiente com contrato de suporte pago seria. Aqui, nao ha
  contrato - e isso esta declarado no README.

## Como medir que acertou

Cinco medidas, todas coletaveis na propria stack, sem ferramenta nova:

1. **Tempo de triagem por correlacao** (o numero que importa): do clique no log
   ate a arvore de trace aberta. Objetivo operacional: menos de 30 segundos em
   ambiente local. Como medir: cronometrar nos simulacros de incidente que rodam
   o `rb-latencia.md`, anotando no registro do exercicio.
2. **Cobertura de propagacao**: percentual de requests com
   `propagated=true` no log da app. Se cair, o problema nao e o backend de
   trace - e a esteira, e o Tempo nao pode consertar.
   ```bash
   docker compose exec prometheus promtool query instant http://localhost:9090 \
     'sum(rate(http_requests_total[5m]))'
   ```
   (a serie de propagacao vem do Loki via `logcli`, porque e campo de log, nao
   label de metrica - decisao de cardinalidade do ADR-0004.)
3. **Custo de armazenamento por semana de trace** comparado com o de log. Tempo
   deveria ficar em ordem de grandeza parecida com Loki para o mesmo volume de
   eventos; se passar 5x, revisar `max_traces_per_user` e a retencao.
4. **Numero de componentes de estado operados**: alvo = 1 tipo de storage. Se
   aparecer um segundo (por exemplo, Cassandra para indexar span), este ADR foi
   revertido na pratica e precisa ser reaberto.
5. **Incidentes resolvidos sem abrir o backend de trace**: metrica invertida,
   util. Se ninguem usa o Tempo em producao depois de 3 meses, o investimento em
   tracing esta na camada errada - e a resposta e reduzir sampling, nao adicionar
   outro sistema.

Criterio de reversao: este ADR e revogado se (a) a necessidade de retencao de
trace maior que 30 dias se tornar contratual, com busca por tag em todas as
dimensoes, ou (b) o custo do object storage para trace ultrapassar o custo de
operar Cassandra sem ganhar nada em troca. As duas condicoes sao verificaveis
com as medidas 3 e 4 acima.
