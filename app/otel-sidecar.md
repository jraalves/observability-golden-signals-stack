# Da app de exemplo para a aplicação real: instrumentação OTel e sidecar

Este arquivo existe porque `app/server.py` **não é o caminho de produção**. Ele é
o caminho didático: instrumentação escrita à mão em stdlib, sem dependência
externa, para que o contrato de telemetria fique visível (decisão registrada em
`docs/decisoes/ADR-0005.md`). Numa aplicação real, você troca essa camada pelo
SDK oficial e o resto do stack continua igual.

Todo dado gerado aqui é sintético.

## 1. O que a app de exemplo faz hoje, e quem faz o quê no caminho real

| Preocupação | Neste repositório (`app/server.py`) | Aplicação real |
| --- | --- | --- |
| Geração de `trace_id`/`span_id` | `new_trace_id`, `new_span_id` | `opentelemetry-sdk` (gerador com CSPRNG) |
| Propagação de contexto | `parse_traceparent` / `build_traceparent` | propagador W3C `tracecontext` embutido no SDK |
| Criação de span de servidor | `build_server_span` no handler | instrumentação do framework (ASGI/WSGI/HTTP) |
| Export de trace | `OtlpJsonExporter` (OTLP/HTTP JSON, thread própria) | OTLP/gRPC ou OTLP/HTTP protobuf com lote e retry |
| Métrica de request | contador e histograma renderizados à mão | `opentelemetry-sdk` metrics, ou exporter Prometheus direto |
| Métrica de processo | leitura de `/proc` e `resource` | `opentelemetry-instrumentation-system-metrics` ou node/cAdvisor |
| Log com `trace_id` | `Logger.emit` com `trace_id` no corpo | `LoggingHandler` do OTel ou `structlog` + processor de contexto |
| Esquema de tags | `app/tagging.py` importado pela app | mesma tabela, aplicada como resource attributes |

A coluna da direita muda o *como*; a coluna da esquerda define o *o quê* - e é o
"o quê" que os painéis, as recording rules e os runbooks deste repositório
consomem.

## 2. O contrato que precisa continuar valendo

Se você trocar a instrumentação, nada no stack muda **desde que** estas quatro
coisas continuem verdadeiras:

1. **Nomes de métrica e labels.** `http_requests_total{service, journey, route,
   status, status_class}`, `http_request_duration_seconds_bucket{..., le}` com
   buckets cumulativos e `le` terminando em `+Inf`, `dependency_requests_total`,
   `app_slo_availability_ratio`, `app_slo_latency_seconds`. As recording rules em
   `prometheus/rules/golden-signals.yml` referenciam exatamente esses nomes.
2. **`/health` fora do SLI.** A app de exemplo não soma `/health` no contador que
   alimenta a taxa de erro. Instrumentação automática de framework costuma contar
   tudo: configure a exclusão de rota, senão o alerta de erro dilui.
3. **Contexto propagado de ponta a ponta.** Requisição que chega com
   `traceparent` válido continua o mesmo trace; a resposta devolve o
   `traceparent` do span de servidor, que é o que o `loadgen.py` usa para
   encadear a jornada. Sem isso, o painel de correlação mostra dois traços
   cortados - o problema descrito no `ADR-0002`.
4. **`trace_id` no corpo do log, e nunca como label de métrica nem de stream.**
   Ver `docs/custos.md` para o preço de ignorar esta linha.

## 3. Configuração do sidecar (Kubernetes)

O padrão recomendado é **sidecar por pod**: o Collector dentro do pod fala OTLP
com a aplicação em `localhost`, aplica processors e exporta para o Collector de
cluster (ou direto para os backends, em ambiente pequeno).

```yaml
# deploy.yaml (trecho) - nomes e valores sao sinteticos
spec:
  template:
    metadata:
      annotations:
        # 10% dos traces: o custo de trace cresce com o produto entre taxa e
        # tamanho medio do span, ver secao 6 de docs/custos.md
        instrumentation.opentelemetry.io/sample-ratio: "0.1"
    spec:
      containers:
        - name: app
          image: registry.local/checkout-api:1.4.2
          env:
            - name: OTEL_SERVICE_NAME
              value: checkout-api
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: >-
                deployment.environment.name=local-sintetico,
                journey=checkout,team=payments-core,
                owner=payments-core-oncall,cost_center=sintetico-plataforma
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: http://localhost:4317
            - name: OTEL_TRACES_EXPORTER
              value: otlp
            - name: OTEL_METRICS_EXPORTER
              value: otlp
            - name: OTEL_PROPAGATORS
              value: tracecontext,baggage
            - name: OTEL_EXPORTER_OTLP_HEADERS
              valueFrom:
                secretKeyRef:
                  name: otel-export
                  key: headers
        - name: otel-collector
          image: docker.io/otel/opentelemetry-collector-contrib:0.108.0
          args: ["--config=/etc/otel/config.yaml"]
          volumeMounts:
            - name: collector-config
              mountPath: /etc/otel
              readOnly: true
```

Dois pontos que costumam quebrar a adoção:

- **`OTEL_RESOURCE_ATTRIBUTES` é onde o esquema de tags entra no mundo real.**
  Os quatro campos obrigatórios de `app/tagging.py` (`service`, `environment`,
  `journey`, `team`) precisam virar atributos de resource, ou os painéis ficam
  sem a dimensão que o `by (service, journey)` das recording rules espera.
- **O secret de exportação mora no cluster, não no repositório.** Este arquivo
  mostra `valueFrom` de propósito: não há valor de credencial versionado em
  lugar nenhum deste projeto - nem no `docker-compose.yml`, nem no
  `otel/collector-config.yaml`.

## 4. Configuração mínima do Collector do sidecar

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 75
    spike_limit_percentage: 15
  batch:
    send_batch_size: 8192
    timeout: 3s
  attributes/ensure_schema:
    actions:
      - key: service.name
        action: upsert
        value: checkout-api
      - key: journey
        action: upsert
        value: checkout
      - key: team
        action: upsert
        value: payments-core
  filter/drop_health:
    metrics:
      exclude:
        match_type: strict
        metric_names:
          - http_requests_total
exporters:
  otlphttp/cluster:
    endpoint: http://otel-collector.observability.svc.cluster.local:4318

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, attributes/ensure_schema, batch]
      exporters: [otlphttp/cluster]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, attributes/ensure_schema, batch]
      exporters: [otlphttp/cluster]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, attributes/ensure_schema, batch]
      exporters: [otlphttp/cluster]
```

O arquivo de referência do ambiente local é `otel/collector-config.yaml`, que já
faz o papel de `attributes` e do corte de cardinalidade; o esquema está narrado
em `otel/tagging.md`.

**Atenção ao `filter/drop_health` acima:** ele é um exemplo do que *não* fazer às
pressas. Derrubar a métrica inteira de uma rota também remove o denominador do
SLI. No stack de exemplo a exclusão é feita na app, antes de contar - que é o
comportamento correto, e é o que `ADR-0004` registra.

## 5. Como validar a troca sem quebrar o stack

Depois de substituir a instrumentação, rode a mesma conferência que o
`scripts/smoke-test.sh` faz, contra o ambiente novo:

```bash
# 1. os nomes de metrica que as recording rules esperam existem?
curl -s http://localhost:9090/api/v1/label/__name__/values \
  | grep -Eo 'http_requests_total|http_request_duration_seconds_bucket|app_slo_[a-z_]+' | sort -u

# 2. toda linha de log saiu com trace_id?
logcli -q '{service="checkout-api"} | json | trace_id!=""' --limit 20

# 3. a jornada continua em um unico trace cross-service?
curl -s 'http://localhost:3200/api/search?tags=journey%3Dcheckout&limit=5' >/dev/null

# 4. a propagacao continua funcionando (o numero que o runbook de latencia cobra)?
logcli -q '{service="checkout-api"} | json | propagated="false"' --since 30m --limit 5
```

Se os itens 1 e 2 passam, nada neste repositório precisa mudar: dashboards,
regras e runbooks continuam valendo. Se o item 4 piora, o problema é o
propagador, não o backend - e é exatamente o diagnóstico da seção 3 do
`runbooks/rb-latencia.md`.

## 6. Quando NÃO usar sidecar

- **Runtime serverless com limitação de memória por função:** o Collector pesa;
  melhor exportar direto do SDK para o backend ou usar um Collector de cluster.
- **Aplicação com alto volume de spans e sampling de cabeça já resolvido no
  SDK:** um sidecar por pod multiplica o custo de infraestrutura pela contagem de
  réplicas sem agregar nada.
- **Ambiente onde a equipe não opera o Collector:** nesse caso, o
  `otel/collector-config.yaml` deste repositório serve de documentação do que o
  operador precisa te entregar, e não de um arquivo para aplicar às cegas.
