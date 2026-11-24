# Notas de acesso: porta local, port-forward e o que cada comando do runbook precisa

Os runbooks deste repositório assumem **o ambiente do `docker-compose.yml`
rodando na mesma máquina**, com as portas do host já publicadas. Nesse caso não
existe port-forward nenhum: `http://localhost:9090` é o Prometheus, e os comandos
estão prontos para colar.

Quando o stack está em outro host (cluster de estudo, VM remota), a tabela abaixo
é o mapeamento entre o nome de serviço interno - que é o que as configs usam - e
a porta local que os comandos dos runbooks esperam.

Todo ambiente descrito aqui é sintético.

## 1. Mapa de portas

| Serviço | Nome DNS interno | Porta no container | Porta no host (compose) | Serve |
| --- | --- | --- | --- | --- |
| Prometheus | `prometheus` | 9090 | 9090 | UI, `/api/v1/*`, `/metrics` |
| Alertmanager | `alertmanager` | 9093 | 9093 | UI, `/api/v2/alerts` |
| Grafana | `grafana` | 3000 | 3000 | painéis, `/api/health` |
| Loki | `loki` | 3100 | 3100 | `/loki/api/v1/*`, LogQL via UI |
| Tempo | `tempo` | 3200 | 3200 | `/api/trace`, `/api/search`, TraceQL |
| OTel Collector (internas) | `otel-collector` | 8888, 8889, 13133 | 8888, 8889 | métricas próprias, métricas OTLP exportadas, health |
| OTel Collector (ingestão) | `otel-collector` | 4317, 4318 | 4317, 4318 | OTLP gRPC e HTTP |
| checkout-api (app) | `sample-app` | 8080 | 8082 | `/pay`, `/auth`, `/health`, `/metrics` |
| auth-api | `auth-api` | 8080 | 8081 | idem, outra identidade |
| search-api | `search-api` | 8080 | 8083 | idem |
| notification-worker | `notification-worker` | 8080 | 8084 | idem |
| Jaeger (comparação) | `jaeger` | 16686 | 16686 | UI de trace alternativa |

Os nomes mudam de propósito: `sample-app` é o **nome do serviço no compose** e
`checkout-api` é o **valor do label `service`** declarado em `SERVICE`. Um runbook
que filtra por `service="checkout-api"` funciona mesmo quando o container se
chama outra coisa - e é essa separação que o `ADR-0004` protege.

## 2. Port-forward em Kubernetes

```bash
NAMESPACE=observability

kubectl -n $NAMESPACE port-forward svc/prometheus 9090:9090 &
kubectl -n $NAMESPACE port-forward svc/grafana   3000:3000 &
kubectl -n $NAMESPACE port-forward svc/loki      3100:3100 &
kubectl -n $NAMESPACE port-forward svc/tempo     3200:3200 &
kubectl -n $NAMESPACE port-forward svc/alertmanager 9093:9093 &
```

Três avisos que evitam perda de tempo:

1. `port-forward` **não suporta requisição HTTP/2 de longa duração bem** e cai
   em silêncio. Para consulta que varre janela grande, rode o comando de dentro
   do pod em vez do túnel:
   ```bash
   kubectl -n $NAMESPACE exec deploy/prometheus -- \
     promtool query instant http://prometheus:9090 'prometheus_tsdb_head_series'
   ```
2. **Não transforme `localhost` em config.** As configs versionadas usam o nome
   do serviço (`prometheus:9090`, `loki:3100`), e o validador estrutural recusa
   healthcheck escrito com `localhost` para os serviços do compose. `localhost`
   aparece nos comandos de *diagnóstico* porque ali ele é o túnel da sua estação,
   não a config do sistema.
3. **Uma sessão por serviço.** Port-forward não é balanceador: se o pod reinicia,
   o túnel morre e o comando seguinte falha com `connection reset`. Reabra o
   túnel antes de concluir que o serviço caiu.

## 3. Túnel para um único host remoto

```bash
# tudo de uma vez: mesmas portas do compose
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 -L 9093:localhost:9093 \
    -L 3100:localhost:3100 -L 3200:localhost:3200 -L 8889:localhost:8889 \
    usuario@host-do-stack
```

Depois disso, os comandos dos runbooks funcionam sem edição - é para isso que
eles foram escritos com `http://localhost:PORT`.

## 4. Tradução dos comandos dos runbooks

| Comando do runbook | Presumindo compose local | Presumindo cluster |
| --- | --- | --- |
| `promtool query instant http://localhost:9090 '<PromQL>'` | funciona como está | exige o túnel do item 2 |
| `curl -s http://localhost:9093/api/v2/alerts` | funciona | `kubectl exec` no pod do Alertmanager |
| `logcli -q '{service="checkout-api"} \| json'` | `docker compose exec loki logcli ...` com `LOKI_ADDR=http://loki:3100` | túnel em 3100 |
| `curl 'http://localhost:3200/api/search?...'` | funciona | dentro do pod do Tempo |
| `curl http://localhost:8082/health` | funciona (app no host 8082) | `kubectl port-forward svc/sample-app 8082:8080` |
| `docker compose ps` / `docker compose logs -f` | só fazem sentido no ambiente local | em cluster, leia `kubectl logs --previous` |

O ponto comum: **o comando é o mesmo, o endereço é que muda**. Por isso os
runbooks não escrevem endereço de cluster em lugar nenhum.

## 5. Conferência rápida antes de culpar o alerta

```bash
for u in \
  http://localhost:9090/-/ready \
  http://localhost:3100/ready \
  http://localhost:3200/ready \
  http://localhost:9093/-/ready \
  http://localhost:3000/api/health \
  http://localhost:8889/metrics \
  http://localhost:8082/health ; do
  code=$(python3 -c "
import sys, urllib.request, urllib.error
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=3).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print('sem-resposta')
" "$u")
  printf '%-40s %s\n' "$u" "$code"
done
```

Se qualquer linha desta lista estiver sem resposta, o `InfraTargetDown` ou o
`InfraPipelineComponentDown` que você está olhando é sinal de esteira, não de
aplicação - é a primeira hipótese do `runbooks/rb-saturacao.md` e do
`runbooks/index.md` ("alerta de esteira tem prioridade sobre alerta de negócio").
