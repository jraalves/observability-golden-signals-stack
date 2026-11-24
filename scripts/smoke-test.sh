#!/usr/bin/env bash
#
# smoke-test do stack: sobe a aplicacao de exemplo em porta alta local, gera
# trafego sintetico com o loadgen, checa saude, confere o /metrics e conta
# quantas linhas de log saem com trace_id.
#
#   bash scripts/smoke-test.sh              # porta 18080
#   PORT=19000 bash scripts/smoke-test.sh   # porta explicita
#   KEEP_LOG=1 bash scripts/smoke-test.sh   # nao apaga o log gerado
#
# Sai com 0 no sucesso e com 1 em qualquer falha. Nao precisa de Docker, de
# rede externa nem de dependencia instalada: usa so a stdlib do Python 3.
# Todo dado gerado aqui e sintetico (SEED declarada).
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"

PORT="${PORT:-18080}"
HOST="127.0.0.1"
JOURNEYS=50            # pedidos sinteticos do smoke-test: 50 jornadas, 2 passos cada
RATE="${RATE:-40}"
TIMEOUT_HEALTH="${TIMEOUT_HEALTH:-20}"
SERVICE="smoke-checkout"
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obs-smoke.XXXXXX")"
APP_LOG="$RUN_DIR/$SERVICE.log"
APP_OUT="$RUN_DIR/stdout.log"
LOAD_OUT="$RUN_DIR/loadgen.log"
APP_PID=""
FALHAS=0

fim() {
  local code=$?
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
  if [ "$code" -ne 0 ] || [ "$FALHAS" -gt 0 ]; then
    echo "FALHA: smoke-test com problema (exit=$code, falhas_acumuladas=$FALHAS)"
    echo "       log em: $RUN_DIR"
    exit 1
  fi
  if [ -z "${KEEP_LOG:-}" ]; then
    rm -rf "$RUN_DIR"
  else
    echo "log preservado em: $RUN_DIR"
  fi
  echo "OK: smoke-test passou"
}
trap fim EXIT

checa() {
  local rotulo="$1" ok="$2" detalhe="${3:-}"
  if [ "$ok" = "0" ]; then
    printf '  [ok]   %-46s %s\n' "$rotulo" "$detalhe"
  else
    printf '  [FAIL] %-46s %s\n' "$rotulo" "$detalhe"
    FALHAS=$((FALHAS + 1))
  fi
}

http_status() {
  python3 - "$1" <<'PY'
import sys, urllib.request, urllib.error
try:
    with urllib.request.urlopen(sys.argv[1], timeout=4) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PY
}

http_json() {
  python3 - "$1" "$2" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=4) as r:
        doc = json.load(r)
except Exception as exc:
    print("erro: %s" % exc)
    raise SystemExit(0)
print(doc.get(sys.argv[2], ""))
PY
}

echo "smoke-test :: observability-golden-signals-stack (dados sinteticos)"
echo "repo:  $ROOT"
echo "porta: $PORT   jornadas: $JOURNEYS   dir: $RUN_DIR"
echo

# 1. subir a aplicacao ------------------------------------------------------
echo "[1] subindo a aplicacao de exemplo"
python3 app/server.py \
  --port "$PORT" --host "$HOST" --service "$SERVICE" \
  --team payments-core --journey checkout \
  --error-rate 0.02 --latency-factor 1 --seed 2026 \
  --log-dir "$RUN_DIR" >"$APP_OUT" 2>&1 &
APP_PID=$!

pronto=1
for _ in $(seq 1 "$TIMEOUT_HEALTH"); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "  o processo da aplicacao morreu; saida:"
    tail -20 "$APP_OUT" || true
    exit 1
  fi
  if [ "$(http_status "http://$HOST:$PORT/health")" = "200" ]; then
    pronto=0
    break
  fi
  sleep 1
done
checa "health check responde 200 em ${TIMEOUT_HEALTH}s" "$pronto"

outcome=$(http_json "http://$HOST:$PORT/health" "outcome")
status=$(http_json "http://$HOST:$PORT/health" "status")
checa "/health devolve outcome=health_ok e status=200" \
  "$([ "$outcome" = "health_ok" ] && [ "$status" = "200" ] && echo 0 || echo 1)" \
  "outcome=$outcome status=$status"

# 2. gerar trafego sintetico ------------------------------------------------
echo "[2] gerando trafego sintetico com app/loadgen.py"
if python3 app/loadgen.py \
  --requests "$JOURNEYS" --rate "$RATE" --concurrency 8 \
  --auth-step "http://$HOST:$PORT/auth" \
  --step "http://$HOST:$PORT/pay" \
  --journey checkout --seed 1337 --timeout 5 \
  --report-every 30 >"$LOAD_OUT" 2>&1; then
  rc=0
else
  rc=$?
  tail -5 "$LOAD_OUT" || true
fi
checa "loadgen terminou com exit 0" "$rc"

linha=$(grep '^LOADGEN_RESULT' "$LOAD_OUT" | tail -1 || true)
launched=$(printf '%s' "$linha" | python3 -c "import json,sys; print(json.loads(sys.stdin.read().split('LOADGEN_RESULT ',1)[1])['launched'])" 2>/dev/null || echo 0)
checa "as $JOURNEYS jornadas sinteticas foram lancadas" \
  "$([ "$launched" -ge "$JOURNEYS" ] && echo 0 || echo 1)" "launched=$launched"

reqs=$(printf '%s' "$linha" | python3 -c "import json,sys; print(int(json.loads(sys.stdin.read().split('LOADGEN_RESULT ',1)[1])['statuses']['requests']))" 2>/dev/null || echo 0)
checa "os 2 passos por jornada chegaram na app" \
  "$([ "$reqs" -ge $((JOURNEYS * 2)) ] && echo 0 || echo 1)" "requests=$reqs"

# 3. conferir o /metrics ----------------------------------------------------
echo "[3] conferindo a exposicao Prometheus"
metricas=$(python3 - "http://$HOST:$PORT/metrics" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=4) as r:
    corpo = r.read().decode("utf-8")
esperadas = (
    "http_requests_total",
    "http_request_duration_seconds_bucket",
    "http_request_duration_seconds_count",
    "app_slo_availability_ratio",
    "app_slo_latency_seconds",
    "process_open_file_descriptors",
)
faltando = [m for m in esperadas if m not in corpo]
print(" ".join(faltando) if faltando else "todas")
PY
)
checa "o /metrics traz as familias que as recording rules esperam" \
  "$([ "$metricas" = "todas" ] && echo 0 || echo 1)" "presentes: $metricas"

series=$(python3 - "http://$HOST:$PORT/metrics" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=4) as r:
    linhas = r.read().decode("utf-8").splitlines()
print(len([l for l in linhas if l and not l.startswith("#")]))
PY
)
checa "o /metrics tem linhas de serie suficientes" \
  "$([ "$series" -ge 50 ] && echo 0 || echo 1)" "series_expostas=$series"

# 4. correlacao: trace_id no log -------------------------------------------
echo "[4] contando linhas de log com trace_id"
contar() {
  local arquivo="$1" padrao="$2" n
  n=$(grep -c -- "$padrao" "$arquivo" 2>/dev/null || true)
  printf '%s' "${n:-0}"
}
total_log=$(contar "$APP_LOG" '')
com_trace=$(contar "$APP_LOG" '"trace_id"')
propagado=$(contar "$APP_LOG" '"propagated": true')
checa "todo log de request carrega trace_id" \
  "$([ "$com_trace" -ge "$((JOURNEYS * 2))" ] && echo 0 || echo 1)" \
  "linhas_com_trace_id=$com_trace de $total_log"
checa "a jornada propagou o mesmo trace entre os 2 servicos virtuais" \
  "$([ "$propagado" -ge "$JOURNEYS" ] && echo 0 || echo 1)" \
  "linhas_com_propagated_true=$propagado"

distinct=$(python3 - "$APP_LOG" <<'PY'
import json, sys
valores = set()
for linha in open(sys.argv[1], encoding="utf-8"):
    try:
        doc = json.loads(linha)
    except json.JSONDecodeError:
        continue
    tid = doc.get("trace_id")
    if tid:
        valores.add(tid)
print(len(valores))
PY
)
checa "cada jornada gerou um trace_id distinto" \
  "$([ "$distinct" -ge "$JOURNEYS" ] && echo 0 || echo 1)" "trace_ids_distintos=$distinct"

# 5. resumo -----------------------------------------------------------------
echo
echo "resumo: $JOURNEYS jornadas sinteticas, $reqs requests, $total_log linhas de log,"
echo "        $com_trace com trace_id, $propagado com contexto propagado."
echo "        p95 e taxa de erro conferidos no corpo do /pay:"
python3 - "http://$HOST:$PORT/pay" <<'PY'
import json, sys, urllib.request
req = urllib.request.Request(sys.argv[1], data=b'{"origem":"smoke-test"}', method="POST",
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=5) as r:
    doc = json.load(r)
print("        window_p95_ms=%s window_error_rate=%s (janela da app, dado sintetico)"
      % (doc.get("window_p95_ms"), doc.get("window_error_rate")))
PY

[ "$FALHAS" -eq 0 ] || exit 1
