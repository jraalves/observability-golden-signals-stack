# ADR-0005: instrumentação em stdlib na app de exemplo, com caminho de sidecar documentado

Status: aceito
Data: 2026-07-06
Decisao aceita em: 2026-07-13
Decisor: autor (SRE)
Contexto relacionado: `app/server.py`, `app/otel-sidecar.md`,
`app/requirements.txt`, `otel/collector-config.yaml`, `docs/custos.md`

## Contexto

A aplicação de exemplo precisa produzir os três sinais (métrica, log, trace) com
a mesma semântica que uma aplicação real produziria, e o repositório precisa
continuar rodando com `python3 app/server.py` em qualquer máquina com Python
3.12.

Havia três caminhos possíveis:

| Opção | O que significa | O que quebra |
| --- | --- | --- |
| SDK oficial do OpenTelemetry | `opentelemetry-api`, `opentelemetry-sdk`, exporter OTLP/HTTP | exige `pip install` de cinco pacotes; a app deixa de rodar sozinha |
| Instrumentação automática por agente | `opentelemetry-instrument python3 app/server.py` | depende de biblioteca de terceiros e não roda em runtime restrito; esconde exatamente o contrato que este projeto quer mostrar |
| Exportador manual em stdlib | `http.server`, `json`, `threading`, `urllib` e OTLP/JSON escrito à mão | o código de instrumentação é nosso e precisa ser revisado como tal |

O objetivo pedagógico decide: quem estuda este repositório precisa ver **o que é
um span**, **o que é contexto propagado** e **o que é formato de exposição do
Prometheus**, e não apenas ligar uma biblioteca que faz isso por fora. Um curso
que termina em `@app.route` decorado por mágica não ensina por que a propagação
falha quando o header `traceparent` é perdido no meio da jornada.

Custo realista da opção escolhida, registrado antes da decisão e não depois:
o exportador manual precisa implementar corretamente `traceId`/`spanId` em
base64 no OTLP/JSON, buckets de histograma cumulativos no `/metrics`, e controle
de concorrência no buffer de export. Isso é código nosso, com bugs nossos.

## Decisao

A app de exemplo é **stdlib only**, sem nenhuma dependência de terceira parte
(`app/requirements.txt` fica versionado vazio de propósito como contrato: se
alguém adicionar um pacote ali, o diff obriga a justificar). O essencial do
contrato fica explícito em `app/server.py`:

- `new_trace_id`/`new_span_id`/`parse_traceparent`/`build_traceparent`: W3C
  Trace Context com protocolo `00`, 32 hex no trace e 16 no span; header
  inválido recomeça o trace e loga `propagated=false`.
- `Metrics.render()`: exposição no formato do Prometheus - `HELP`, `TYPE`,
  contador por rota e classe de status, histograma com buckets cumulativos em
  `HISTOGRAM_BUCKETS_SECONDS`, gauge de requests em execução e os dois SLOs
  publicados como serie (`app_slo_availability_ratio`,
  `app_slo_latency_seconds`), que é o que alimenta o burn rate do ADR-0003.
- `OtlpJsonExporter`: thread de fundo com buffer, `retry` limitado e contador de
  falha (`otlp_export_failures_total`). Falha de export nunca derruba request.
- `Logger.emit`: JSON de uma linha no stdout, com espelho em arquivo quando
  `LOG_DIR` está definido, sempre com o esquema do ADR-0004 validado antes de
  escrever.

Para ambiente real, o caminho correto está documentado em
`app/otel-sidecar.md`: SDK oficial ou agente de instrumentação, exportando para o
Collector como sidecar no pod, com as mesmas quatro tags obrigatórias vindas de
recursos do OTel. Este ADR decide a app de exemplo, não a arquitetura de
produção de quem copia o stack.

## Consequencias

Boas:

- `python3 app/server.py` funciona sem índice de pacote, sem proxy e sem wheel
  compilado. O smoke-test sobe a app em porta alta sem instalar nada.
- O contrato de telemetria fica legível: um revisor consegue ver se o histograma
  é cumulativo e se o `traceparent` da resposta continua o trace do pedido.
- A superfície exposta no `/metrics` é a mesma que o `prometheus-client` geraria,
  então o scrape e as recording rules não sabem que a app é artesanal.

Custosas:

- **Não é um SDK.** Não há contexto implícito, `span.end()` automático,
  sampling configurável por amostragem de cabeça nem W3C `tracestate` propagado.
  Quem for para produção troca esta camada, e o `app/otel-sidecar.md` diz onde.
- Mais linhas de código para revisitar (`app/server.py` é o maior arquivo do
  repositório) e mais chance de erro de forma de exposição, mitigado por testes
  em `tests/` e pelo validador estrutural.
- O export OTLP é HTTP/JSON, não gRPC: funciona com o Collector, mas não é o
  transporte mais eficiente. O Collector já aceita os dois.
- A métrica de processo (`process_cpu_seconds_total`,
  `process_resident_memory_bytes`, `process_open_file_descriptors`) é lida da
  stdlib (`resource`, `/proc`) com fallback: em container sem `/proc` montado o
  sinal some e o alerta de saturação fica cego - comportamento documentado, não
  escondido.

## Como medir que acertou

1. **Zero dependência verificável:** o validador falha se
   `app/requirements.txt` ganhar linha não-comentário. Rodar
   `python3 -m py_compile app/*.py` com `PYTHONNOUSERSITE=1` e sem `site-packages`
   do projeto: se importar, temos dependência escondida.
2. **Contrato de exposição conferido no próprio servidor:**
   ```bash
   python3 app/server.py --port 18090 --log-dir /tmp/obs &
   python3 app/loadgen.py --requests 50 --rate 50 --step http://127.0.0.1:18090/pay
   curl -s http://127.0.0.1:18090/metrics | grep -E '^(http_request_duration_seconds_bucket|app_slo)' | head
   ```
   Objetivo: buckets cumulativos monotonicos, `app_slo_* presente`, nenhum nome
   de label fora do ADR-0004.
3. **Propagação medida no log, não na UI:** fração de linhas com
   `propagated=true` quando o trafego vem do `loadgen.py` (que propaga) contra
   `curl` simples (que não propaga). `scripts/smoke-test.sh` faz a contagem de
   linhas com `trace_id`; o teste unitário cobre o parser do header.
4. **Custo da instrumentação:** `latency_factor` e `HANDLER_OVERHEAD_MS` existem
   para que a sobrecarga do handler seja declarada em um lugar só. Comparar
   `service:http_request_duration_seconds:avg_5m` com a soma dos tempos de
   dependencia simulada; a diferença deve bater com a constante documentada.
5. **Revisão de escopo:** se um dia a app precisar de sampling por cabeça,
   contexto implícito ou `tracestate`, isto é um novo ADR substituindo o 0005,
   não um patch silencioso no exportador.
