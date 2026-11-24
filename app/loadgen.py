#!/usr/bin/env python3
"""Gerador de trafego SINTETICO para o ambiente local do stack.

Nada aqui representa carga real de usuario: o objetivo e dar serie temporal
para os quatro golden signals, mantendo o controle das variaveis. Taxa,
duracao, seed e destino sao configuraveis, e a propagacao de ``traceparent``
entre os passos de uma jornada e o que faz o painel de correlacao ter dado
cross-service para mostrar.

Uso:

    python3 app/loadgen.py --rate 5 --duration 60 \
        --step http://localhost:8081/auth --step http://localhost:8082/pay

    python3 app/loadgen.py --requests 50 --step http://localhost:8080/pay

Stdlib only: ``urllib`` para as chamadas, ``threading`` para manter a taxa, e
``app.stats`` para o resumo em linha, que e o mesmo calculo que o runbook usa.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

try:  # importado como pacote
    from . import stats, tagging
except ImportError:  # executado como script
    import stats
    import tagging

STOP = threading.Event()
DEFAULT_RATE = 5.0


def build_opener() -> urllib.request.OpenerDirector:
    """Opener sem redirect automatico: 3xx em API e sinal, nao convenience."""
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    opener.addheaders = [("User-Agent", "obs-stack-loadgen/sintetico")]
    return opener


class JourneyResult:
    """Acumulado de resultados para o resumo periodico em stdout."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.durations_ms: list[float] = []
        self.statuses: list[int] = []
        self.by_target: dict[str, dict[str, float]] = {}
        self.completed = 0
        self.connection_errors = 0

    def record(self, target: str, duration_ms: float, status: int | None) -> None:
        with self.lock:
            self.durations_ms.append(duration_ms)
            if status is None:
                self.connection_errors += 1
                self.statuses.append(599)
            else:
                self.statuses.append(status)
            bucket = self.by_target.setdefault(
                target, {"requests": 0.0, "errors": 0.0, "p95_ms": 0.0}
            )
            bucket["requests"] += 1
            if status is None or status >= 500:
                bucket["errors"] += 1

    def summary(self) -> dict[str, object]:
        with self.lock:
            summary = stats.summarize(self.durations_ms, self.statuses)
            summary["journeys"] = float(self.completed)
            summary["connection_errors"] = float(self.connection_errors)
            summary["by_target"] = {
                target: dict(values) for target, values in sorted(self.by_target.items())
            }
            return summary

    def tail(self, n: int = 20000) -> None:
        with self.lock:
            del self.durations_ms[:-n]
            del self.statuses[:-n]


def call_step(
    opener: urllib.request.OpenerDirector,
    target: str,
    method: str,
    trace_id: str,
    parent_span_id: str,
    request_id: str,
    timeout: float,
) -> tuple[int | None, str, float]:
    """Executa um passo da jornada propagando o contexto W3C.

    Retorna (status, span_id_do_proximo_pai, duracao_ms). O span_id vem do
    ``traceparent`` da resposta: e assim que a jornada inteira fica na mesma
    arvore de trace no Tempo.
    """
    traceparent = f"00-{trace_id}-{parent_span_id or '0' * 16}-01"
    request = urllib.request.Request(
        target,
        data=b'{"origem":"loadgen-sintetico"}' if method == "POST" else None,
        method=method,
        headers={
            "traceparent": traceparent,
            "X-Request-Id": request_id,
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read()
            next_parent = response.headers.get("traceparent", traceparent)
            status = response.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx: conta, mas nao interrompe
        try:
            exc.read()
        except Exception:  # pragma: no cover
            pass
        next_parent = exc.headers.get("traceparent", traceparent) if exc.headers else traceparent
        status = exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return None, traceparent, (time.perf_counter() - started) * 1000.0
    duration_ms = (time.perf_counter() - started) * 1000.0
    parts = next_parent.split("-")
    next_span = parts[2] if len(parts) == 4 else "0" * 16
    return status, next_span, duration_ms


def run_journey(
    opener: urllib.request.OpenerDirector,
    steps: list[tuple[str, str]],
    rng: random.Random,
    result: JourneyResult,
    journey: str,
    timeout: float,
) -> None:
    """Uma jornada completa: mesma trace_id, um span pai por passo."""
    with result.lock:
        trace_id = "".join(rng.choice("0123456789abcdef") for _ in range(32))
        request_id = "".join(rng.choice("0123456789abcdef") for _ in range(16))
    parent_span_id = ""
    for method, target in steps:
        status, parent_span_id, duration_ms = call_step(
            opener, target, method, trace_id, parent_span_id, request_id, timeout
        )
        result.record(target, duration_ms, status)
    with result.lock:
        result.completed += 1


def log_line(level: str, message: str, **fields: object) -> None:
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "service": "load-generator",
        "environment": os.environ.get("ENVIRONMENT", "local-sintetico"),
        "journey": tagging.normalize_journey(os.environ.get("JOURNEY", "checkout")),
        "team": "sre-core",
        "owner": "sre-core-oncall",
        "message": message,
        "sintetico": True,
    }
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--step", action="append", default=[], metavar="URL",
                        help="URL de um passo da jornada; repita para encadear (mesmo trace_id)")
    parser.add_argument("--auth-step", action="append", default=[], help="atalho: POST em URL")
    parser.add_argument("--rate", type=float, default=float(os.environ.get("RATE", DEFAULT_RATE)),
                        help="jornadas por segundo (sintetico)")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("CONCURRENCY", 4)))
    parser.add_argument("--duration", type=float, default=float(os.environ.get("DURATION", 0)),
                        help="segundos de execucao; 0 = ate receber SIGTERM")
    parser.add_argument("--requests", type=int, default=int(os.environ.get("REQUESTS", 0)),
                        help="numero exato de jornadas e para (usado pelo smoke-test)")
    parser.add_argument("--journey", default=os.environ.get("JOURNEY", "checkout"))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("SEED", 1337)))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("TIMEOUT", 3.0)))
    parser.add_argument("--report-every", type=float, default=10.0, help="segundos entre resumos")
    parser.add_argument("--once", action="store_true", help="executa uma unica jornada e sai")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    steps: list[tuple[str, str]] = [("GET", url) for url in args.step]
    steps += [("POST", url) for url in args.auth_step]
    if not steps:
        # Ambiente local do compose: jornada de checkout ponta a ponta.
        base_auth = os.environ.get("AUTH_URL", "http://localhost:8081")
        base_checkout = os.environ.get("CHECKOUT_URL", "http://localhost:8082")
        steps = [("POST", f"{base_auth}/auth"), ("POST", f"{base_checkout}/pay")]

    rng = random.Random(args.seed)
    result = JourneyResult()
    opener = build_opener()

    def _stop(signum, frame) -> None:  # pragma: no cover - via sinal externo
        STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, RuntimeError):  # pragma: no cover - fora da main thread
            pass

    log_line(
        "info",
        "gerador sintetico iniciado",
        journey=tagging.normalize_journey(args.journey),
        steps=[target for _, target in steps],
        rate=args.rate,
        concurrency=args.concurrency,
        duration_s=args.duration or None,
        requests=args.requests or None,
    )

    rate = args.rate if args.rate > 0 else 1.0
    interval = 1.0 / rate
    started = time.monotonic()
    deadline = started + args.duration if args.duration > 0 else None
    launched = 0
    next_tick = started
    last_report = started
    threads: list[threading.Thread] = []

    while not STOP.is_set():
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break
        if args.requests and launched >= args.requests:
            break
        if args.once:
            run_journey(opener, steps, rng, result, args.journey, args.timeout)
            launched += 1
            break
        if now >= next_tick:
            # Uma jornada por thread mantem a taxa sem acumular atraso quando a
            # latencia do destino sobe - que e exatamente quando se quer carga.
            thread = threading.Thread(
                target=_safe_journey,
                args=(opener, steps, rng, result, args.journey, args.timeout),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            launched += 1
            next_tick += interval
            if next_tick < now - (2.0 * interval):
                next_tick = now + interval
        threads = [t for t in threads if t.is_alive()]
        if len(threads) > max(1, args.concurrency) * 4:
            time.sleep(0.05)
        if now - last_report >= args.report_every:
            summary = result.summary()
            log_line("info", "resumo sintético", launched=launched, **summary)
            result.tail()
            last_report = now
        time.sleep(min(0.01, interval) if interval else 0.01)

    for thread in threads:
        thread.join(timeout=args.timeout + 1.0)
    final = result.summary()
    elapsed = max(1e-6, time.monotonic() - started)
    log_line(
        "info",
        "gerador sintético encerrado",
        launched=launched,
        elapsed_s=round(elapsed, 2),
        jornadas_por_s=round(final["journeys"] / elapsed, 2),
        **final,
    )
    if args.requests:
        # Modo do smoke-test: saida maquina-legivel com o essencial.
        print(
            "LOADGEN_RESULT "
            + json.dumps({"launched": launched, "statuses": final, "targets": [t for _, t in steps]})
        )
    return 0


def _safe_journey(opener, steps, rng, result, journey, timeout) -> None:
    """Nunca deixa uma jornada excepcionada derrubar o gerador."""
    try:
        run_journey(opener, steps, rng, result, journey, timeout)
    except Exception as exc:  # pragma: no cover - rede instavel em laptop
        log_line("warn", "jornada interrompida", erro=str(exc)[:200])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
