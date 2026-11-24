# Jaeger neste stack: por que ele está de pé, e por que não é o padrão

O `docker-compose.yml` sobe `jaegertracing/all-in-one` ao lado do Tempo. Isso é
deliberado e tem duas razões honestas:

1. servir de **comparacao lado a lado** durante o estudo — a UI de trace do
   Jaeger é a referência mental de todo mundo que aprendeu tracing antes de
   2023, e poder abrir as duas no mesmo dado evita conclusão por hábito;
2. registrar o **caminho de substituição**: trocar o exporter
   `otlp/tempo` por `otlp/jaeger` no Collector é uma linha, e o ambiente
   continua funcionando.

Não subo os dois em paralelo no mesmo pipeline: span duplicado em dois backends
de trace é custo dobrado e duas verdades sobre o mesmo request.

## Opções de armazenamento do Jaeger

| Backend | Quando faz sentido | O que custa |
| --- | --- | --- |
| `memory` | demo, CI, laptop | nada, mas zero retenção e zero sobrevivência a restart |
| `badger` | single-node, edge, ambiente pequeno | disco local; retém em restart; não escala horizontalmente |
| `elasticsearch`/`opensearch` | já se roda um cluster de busca para logs | índice de span grande, shard management, mapeamento de campos que exige upgrade cuidado |
| `cassandra` | escala horizontal de escrita com TTL por coluna | um cluster para operar, `repair`, JVM, schema, e um oncall que saiba Cassandra |

O ponto que decide a discussão não é "Jaeger é ruim". É o formato do problema
em ambientes onde log já é um serviço: com Jaeger + Cassandra você opera
**dois** bancos de dados distribuídos (Cassandra para span, Elasticsearch/Loki
para log), com dois modelos de retenção e duas conversas de upgrade diferentes,
e a correlação entre eles ainda depende de `trace_id` propagado do mesmo jeito.
Ou seja: o ganho de ter Jaeger não elimina o trabalho de correlação, mas
adiciona um storage.

## Por que Tempo foi escolhido aqui

- **Um backend de objeto para log e trace.** Loki guarda chunk de log em
  filesystem/S3; Tempo guarda block de trace em filesystem/S3. No compose local
  os dois usam `local`, e no caminho de produção os dois apontariam para o mesmo
  bucket. Cassandra some da equação.
- **TraceQL em vez de busca por índice.** O Jaeger clássico depende de índice de
  spans (Elasticsearch/Cassandra com `index tag`), e o custo do índice cresce
  com o número de tags. TraceQL consulta o block e usa um *index de atributos*
  gerado pelo `metrics_generator`/block storage, o que combina com a política
  deste projeto de não indexar atributo de alto cardinalidade.
- **Service graph nativo.** O `metrics_generator` do Tempo emite as séries de
  arestas de serviço, que é o que aparece no painel
  `Correlacao de incidente` — sem instalar um Jaeger separado só para topologia.
- **Grafana como superfície única de triagem.** O `derivedFields` do Loki aponta
  para o datasource Tempo; a jornada log→trace→métrica acontece em uma aba. Com
  Jaeger haveria um segundo portal e um segundo conjunto de permissões.

## O que se perde ao trocar Jaeger por Tempo

Para não fingir que a escolha é grátis:

- **Ecossistema de ingestão**: Jaeger aceita Thrift/zipkin antigo com mais
  maturidade; neste stack, quem ainda emite Zipkin v2 aponta para o Collector,
  que converte para OTLP — o Collector, e não o backend, resolve legado.
- **UI de diff de trace e comparação**: a do Jaeger é mais madura para
  comparação entre duas execuções de um mesmo trace.
- **Percepção de "padrão de mercado"**: em entrevista e em auditoria, Jaeger é
  nome conhecido; Tempo ainda precisa ser explicado. É um custo político, não
  técnico.

## Como usar o Jaeger do compose mesmo assim

Troque no `otel/collector-config.yaml` o exporter do pipeline de traces:

```yaml
exporters:
  otlp/jaeger:
    endpoint: jaeger:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      exporters: [otlp/jaeger]   # em vez de [otlp/tempo, file/traces-debug]
```

Depois `docker compose up -d otel-collector` e abra `http://localhost:16686`.
O `max_traces` de memória do all-in-one está limitado por `MEMORY_MAX_TRACES`
no compose; ao terminar a comparação, reverta o exporter — os painéis do
Grafana apontam para o Tempo e deixariam de ter dado.
