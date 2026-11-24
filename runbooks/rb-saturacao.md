# RB-SATURACAO - recurso perto do limite (CPU, memoria, disco, fila, pods)

Familia: **saturation**. Dono primario: time do recurso que saturou - que nem
sempre e o time do servico que aparece no alerta. Escopo:
`SaturationCpuAboveLimit`, `SaturationMemoryNearLimit`,
`SaturationFileDescriptorsHigh`, `SaturationQueuePressureCritical`,
`InfraCpuSaturation`, `InfraMemorySaturation`, `InfraDiskSpaceLow`,
`InfraDiskAlmostFull`, `InfraTSDBHeadSeriesHigh`, `InfraPodRestartLoop`,
`InfraPodNotReady`, `InfraTargetDown`, `InfraPipelineComponentDown`,
`InfraOtelExporterFailures`, `InfraOtelDataDropped`, `InfraLokiWriteFailures`,
`InfraTempoFlushFailures`, `InfraAlertNotificationFailures`,
`InfraRuleEvaluationFailures`, `InfraPrometheusConfigReloadFailed`.

Este runbook cobre dois mundos com o mesmo metodo: **aplicacao** (o processo
esta sem ar) e **esteira de telemetria** (Prometheus/Loki/Tempo/Alertmanager
sem disco, sem memoria ou sem conseguir entregar alerta). O segundo e o que
transforma incidente pequeno em incidente cego.

## Gatilho

- `SaturationQueuePressureCritical` ou `SaturationCpuAboveLimit`: pagina, porque
  saturacao antecede erro em minutos;
- `SaturationMemoryNearLimit` / `SaturationFileDescriptorsHigh`: warning com
  tendencia - nao pagina, mas nao se ignora;
- `InfraDiskAlmostFull` no Prometheus, Loki ou Tempo: **a escrita de telemetria
  vai parar**, e o proximo incidente sera investigado no escuro;
- `InfraAlertNotificationFailures`: o Alertmanager nao consegue avisar ninguem.
  Este e o alerta mais importante do stack, porque e o que impede os outros de
  funcionarem;
- `InfraPodRestartLoop` / `InfraTargetDown`: o alvo sumiu do scrape. Sem serie
  nova, todo alerta por ausencia de dado vira ruido - o rule file trata isso
  com `absent()` justamente para nao deixar vazio virar verde.

## Hipoteses em ordem de custo de verificacao

| # | Hipotese | Custo | Confirmacao |
| --- | --- | --- | --- |
| 1 | E o processo ou o host/container? | 30 s | `service:process_cpu:cores5m` vs `instance:system_cpu_utilization:ratio5m` |
| 2 | E tendencia de vazamento ou pico? | 60 s | memoria em 6h vs 1h |
| 3 | E fila de request (concorrencia) ou CPU? | 60 s | `queue_pressure` com CPU plana |
| 4 | E disco de telemetria? | 90 s | `instance:system_filesystem_utilization:ratio` |
| 5 | E cardinalidade explodindo no TSDB? | 2 min | `prometheus_tsdb_head_series` |
| 6 | E o scrape/export caindo (Collector, Loki, Tempo)? | 2 min | `pipeline:*` + `job:scrape_up:ratio` |
| 7 | E o notification (webhook sink inexistente)? | 3 min | `pipeline:alertmanager_notify_failures:increase10m` |

## Diagnostico

### 1. Delimitar o recurso (promtool)

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:process_cpu:cores5m'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:process_memory:bytes'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:http_requests:queue_pressure'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'service:process_open_fds:ratio'

# tendencia: vazamento aparece na curva de 6h, pico nao
docker compose exec prometheus promtool query range --since=6h --step=5m \
  http://localhost:9090 'service:process_memory:bytes' | tail -20
```

Leitura que importa: `queue_pressure > 1` com CPU < 0.5 nao e "preciso de mais
CPU", e concorrencia mal dimensionada ou request preso em algo lento
(timeout de dependencia - veja RB-LATENCIA). Adicionar replica aqui so
multiplica o mesmo gargalo.

### 2. Olhar o host por cima da serie de aplicacao

```bash
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'instance:system_cpu_utilization:ratio5m'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'instance:system_filesystem_utilization:ratio'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'instance:system_memory_utilization:ratio'

# o proprio container, sem depender de exporter
docker stats --no-stream $(docker compose ps --services --filter status=running) 2>/dev/null | head
docker compose ps
docker compose logs --since 10m --tail 40 otel-collector
```

Se `instance:system_filesystem_utilization:ratio` estiver acima de 0.85 em
`/prometheus`, `/loki` ou `/var/tempo`, a prioridade muda: disco cheio em
telemetria nao "degrada", ele apaga a visibilidade.

### 3. TSDB e cardinalidade (diagnostico caro, e por isso fica aqui)

```bash
docker compose exec prometheus promtool tsdb analyze /prometheus 2>/dev/null | head -30
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'prometheus_tsdb_head_series'
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'topk(5, count by (__name__) ({__name__=~".+"}))'

# quantas series por label - a pergunta que paga ou economiza o proximo mes
docker compose exec prometheus promtool query instant http://localhost:9090 \
  'count by (journey) (http_requests_total)'
```

Se `prometheus_tsdb_head_series` subiu em degrau na mesma hora em que um novo
servico entrou no scrape, a hipotese 5 esta confirmada e `docs/custos.md` tem a
conta do que fazer com ela.

### 4. Esteira de telemetria como servico (curl + logcli)

```bash
# Collector de pe? (extension health_check no :13133)
curl -s -o /dev/null -w 'collector %{http_code}\n' http://localhost:13133/
# OTLP HTTP aceita? (o 200 aqui e o que a app de exemplo espera)
curl -s -o /dev/null -w 'otlp %{http_code}\n' -X POST http://localhost:4318/v1/traces \
  -H 'Content-Type: application/json' -d '{"resourceSpans":[]}'
# Loki e Tempo prontos?
curl -s http://localhost:3100/ready; echo; curl -s http://localhost:3200/ready; echo
# Alertmanager: config carregada e silence store ok
curl -s http://localhost:9093/api/v2/status | python3 -m json.tool | head -25

# o que os containers gravaram de erro?
docker compose exec loki logcli --address http://localhost:3100 query \
  '{service="otel-collector"} |~ "error|export" ' --limit 20
```

`InfraAlertNotificationFailures` quase sempre aqui: o `webhook-sintetico` aponta
para `http://localhost:9999/hooks/observabilidade`, um destino **inventado de
proposito** para nao existir endpoint real versionado. Nesse caso o certo e
assumir que notificacao nao sai, e nunca "consertar" o alerta para apagar a
falha de notificacao.

### 5. Kubernetes, se o servico estiver la

```bash
kubectl top pods -n obs --sort-by=cpu | head
kubectl top nodes
kubectl -n obs describe pod <pod> | grep -A5 "Last State\|Reason"
kubectl -n obs get pod <pod> -o jsonpath='{.spec.containers[*].resources}'; echo
kubectl -n obs get events --field-selector reason=OOMKilling -w
kubectl -n obs get --raw '/api/v1/namespaces/monitoring/services/prometheus:9090/proxy/api/v1/targets?stateDate=active' >/dev/null && echo proxy ok
```

`OOMKilling` com memoria em rampa de 6h e vazamento: reiniciar o pod trata o
sintoma e **nao** trata a causa, entao reinicio aqui so com aprovacao e com
issue aberto no mesmo movimento.

## Acao

Tudo abaixo muda estado do ambiente. **Aprovacao humana obrigatoria** nos itens
marcados; o critério é simples: se pode descartar dado, derrubar processo ou
aumentar custo, precisa de um segundo humano.

| Causa | Acao | Aprovacao |
| --- | --- | --- |
| Fila/concorrencia sem CPU (queue_pressure) | Aumentar workers/replicas do servico e revisar timeout | **obrigatoria** |
| CPU em plateau com p50 crescendo | Aumentar `limits.cpu` do pod (vertical) antes de horizontal | **obrigatoria** |
| Memoria em rampa de vazamento | Rollout com limite maior + **heapsnapshot antes de matar o pod** | **obrigatoria** |
| Disco de telemetria acima de 0.9 | Reduzir retencao (`--storage.tsdb.retention.time`) ou liberar volume | **obrigatoria** (perda de historico) |
| Cardinalidade explodindo | Relabeling/`metric_relabel_configs` no job ofensor; nunca `drop` silencioso sem ADR | **obrigatoria** |
| Collector com falha de export | `docker compose restart otel-collector` (perde o buffer de 2000 spans) | **obrigatoria** |
| Loki/Tempo sem escrita | Liberar disco antes de reiniciar; depois confirmar `/ready` | **obrigatoria** |
| Notification failure no Alertmanager | Trocar/validar receiver; **silenciar o alerta de notificacao jamais** | nao |
| Pod em restart loop | `kubectl -n obs rollout undo` e assumir o deploy anterior | **obrigatoria** |

```bash
# ambiente local
docker compose restart otel-collector
docker compose restart tempo
docker compose down --volume loki-data          # DESTRUTIVO: perde log, aprovacao + backup
docker compose exec prometheus wget -q -O- http://localhost:9090/-/reload   # recarrega config
```

Em Kubernetes, para aliviar pressão sem destruir o histórico:

```bash
kubectl -n obs patch deployment checkout-api -p '{"spec":{"template":{"spec":{"containers":[{"name":"app","resources":{"limits":{"cpu":"1500m"}}}]}}}}'   # aprovacao
kubectl -n obs delete pod <pod> --grace-period=30            # aprovacao
kubectl -n obs rollout undo deployment/notification-worker   # aprovacao
```

Antes de qualquer `down --volume` ou exclusao de block do Tempo/Loki: copie o
`trace_id` e o intervalo de tempo do incidente para o thread. A telemetria que
voce esta prestes a apagar e a unica testemunha do que aconteceu.

## Criterio de saida

1. o recurso que saturou voltou abaixo do threshold com **tendencia** plana ou
   caindo por 3 janelas (`service:process_cpu:cores5m`,
   `instance:system_filesystem_utilization:ratio`, `queue_pressure`);
2. `job:scrape_up:ratio == 1` para todos os jobs - nenhuma serie sumiu no
   caminho;
3. `pipeline:otel_export_failures:rate5m`, `pipeline:loki_append_failures:rate5m`
   e `pipeline:tempo_flush_failures:rate5m` zerados por 15 minutos;
4. `pipeline:alertmanager_notify_failures:increase10m == 0`;
5. o alerta da familia `saturation` saiu de `firing` **sem** ter virado
   `ErrorRatioHigh` ou `LatencyP95AboveObjective`:
   ```bash
   docker compose exec prometheus promtool query instant http://localhost:9090 \
     'count(ALERTS{alertstate=~"firing", signal=~"saturation|errors|latency"})'
   ```
6. se a acao foi reducao de retencao ou de cardinalidade, isso esta registrado
   no incidente e no ADR-0004 - visibilidade reduzida e decisao com dono.

## O que aprender depois

- **Cardinalidade**: `ADR-0004` (padronizacao de tags por aplicacao/jornada) tem
  a secao de custo; `docs/custos.md` mostra a aritmética. Se o crescimento foi
  degrau apos um servico novo, a resposta e o relabeling do job, nao "aumentar o
  Prometheus".
- **Saturacao virou erro**: se `queue_pressure` estava alto e terminou em 503,
  o threshold do alerta de saturacao esta tarde demais - discuta `for:` menor
  para esse servico, com base no tempo medio entre saturar e falhar medido no
  historico (aqui: sintético).
- **Falta de visibilidade da propria esteira**: cada serie `pipeline:*` deste
  rule file existe porque uma versao minha tentou debugar telemetria sem
  telemetria. Se alguma delas estava zerada e mesmo assim voce ficou cego, o
  proximo passo e cobertura de scrape, nao dashboard.
- **Reinicio resolveu o sintoma**: abra follow-up com heap/profiling. Um runbook
  que termina em restart recorrente esta documentando a ausencia de uma causa.
