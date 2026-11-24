#!/usr/bin/env python3
"""Aplicacao de exemplo do stack de observabilidade (Python stdlib only).

Quatro servicos identicos sobem a partir deste arquivo, trocando apenas as
env vars ``SERVICE``/``JOURNEY``/``TEAM``. O codigo e pequeno de proposito: o
objetivo nao e ser uma aplicacao interessante, e ser um alvo de telemetria
previsivel, onde cada um dos quatro golden signals pode ser injetado e
investigado.

O que este arquivo garante, e do que o resto do stack depende:

* ``X-Request-Id`` e ``traceparent`` (W3C) gerados ou propagados por request;
* log estruturado em JSON no stdout, com ``trace_id`` em todo request;
* /metrics em formato de exposicao do Prometheus, com histograma, contador por
  classe de status, saturacao de processo e os objetivos de SLO declarados;
* export de spans em OTLP/JSON para o Collector sem dependencia de terceira
  parte (ver app/otel-sidecar.md para o por que);
* latencia com jitter deterministico (``random.Random(seed)``), para que um
  teste consiga reproduzir a mesma distribuicao.

Nada aqui fala com servico externo: os downstreams sao simulados com sono
local. Todos os numeros de exemplo sao sinteticos.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:  # importado como pacote (tests/)
    from . import stats, tagging
except ImportError:  # executado como script (docker compose, smoke-test)
    import stats
    import tagging

PROTOCOL_VERSION = "00"
TRACEPARENT_RE = re.compile(
    r"^(?P<ver>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
HISTOGRAM_BUCKETS_SECONDS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 5.0)
# A fronteira do SLO precisa existir como bucket do histograma, senao a fracao
# de requests compliant nao e calculavel (mesma nota em slo-burn-rate.yml).
SLO_BUCKET = 1.2
SPAN_KIND_SERVER = 2

# route -> nome do span, latencia base, jitter e dependencia simulada.
ROUTES: dict[str, dict[str, object]] = {
    "/pay": {
        "span_name": "processar pagamento",
        "base_ms": 240.0,
        "jitter_ms": 620.0,
        "dependency": "gateway-pagamentos",
    },
    "/auth": {
        "span_name": "autenticar sessao",
        "base_ms": 60.0,
        "jitter_ms": 180.0,
        "dependency": "provedor-identity",
    },
    "/health": {
        "span_name": "health check",
        "base_ms": 1.5,
        "jitter_ms": 3.0,
        "dependency": None,
    },
}


# --------------------------------------------------------------------------
# configuracao
# --------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


class Config:
    """Configuracao vinda de env var. Todo knob de injecao fica explicito aqui."""

    def __init__(
        self,
        service: str = "checkout-api",
        journey: str = "checkout",
        team: str = "payments-core",
        environment: str = "local-sintetico",
        owner: str = "",
        version: str = "1.0.0-sintetico",
        region: str = "local-1",
        error_rate: float = 0.015,
        client_error_rate: float = 0.01,
        dependency_error_rate: float = 0.01,
        latency_factor: float = 1.0,
        seed: int = 2026,
        port: int = 8080,
        host: str = "0.0.0.0",
        otlp_endpoint: str = "",
        log_dir: str = "",
        slo_availability: float = 0.995,
        slo_latency_compliance: float = 0.99,
    ):
        self.service = service
        self.journey = tagging.normalize_journey(journey)
        self.team = team
        self.environment = environment
        self.owner = owner or f"{team}-oncall"
        self.version = version
        self.region = region
        self.error_rate = max(0.0, min(1.0, error_rate))
        self.client_error_rate = max(0.0, min(1.0, client_error_rate))
        self.dependency_error_rate = max(0.0, min(1.0, dependency_error_rate))
        self.latency_factor = max(0.0, latency_factor)
        self.seed = seed
        self.port = port
        self.host = host
        self.otlp_endpoint = otlp_endpoint.rstrip("/")
        self.log_dir = log_dir
        self.slo_availability = slo_availability
        self.slo_latency_compliance = slo_latency_compliance
        self.resource_attributes = tagging.resource_attributes(
            service=service,
            environment=environment,
            journey=self.journey,
            team=team,
            owner=self.owner,
            version=version,
            region=region,
        )

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            service=_env_str("SERVICE", "checkout-api"),
            journey=_env_str("JOURNEY", "checkout"),
            team=_env_str("TEAM", "payments-core"),
            environment=_env_str("ENVIRONMENT", "local-sintetico"),
            owner=_env_str("OWNER", ""),
            version=_env_str("VERSION", "1.0.0-sintetico"),
            region=_env_str("REGION", "local-1"),
            error_rate=_env_float("ERROR_RATE", 0.015),
            client_error_rate=_env_float("CLIENT_ERROR_RATE", 0.01),
            dependency_error_rate=_env_float("DEPENDENCY_ERROR_RATE", 0.01),
            latency_factor=_env_float("LATENCY_FACTOR", 1.0),
            seed=int(_env_float("SEED", 2026)),
            port=int(_env_float("PORT", 8080)),
            host=_env_str("HOST", "0.0.0.0"),
            otlp_endpoint=_env_str("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            log_dir=_env_str("LOG_DIR", ""),
            slo_availability=_env_float("SLO_AVAILABILITY", 0.995),
            slo_latency_compliance=_env_float("SLO_LATENCY_COMPLIANCE", 0.99),
        )

    def __repr__(self) -> str:  # pragma: no cover - conveniencia de debug
        return (
            f"Config(service={self.service!r}, journey={self.journey!r}, "
            f"team={self.team!r}, error_rate={self.error_rate}, "
            f"latency_factor={self.latency_factor}, seed={self.seed})"
        )


# --------------------------------------------------------------------------
# contexto de trace (W3C Trace Context)
# --------------------------------------------------------------------------


def new_trace_id(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(32))


def new_span_id(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(16))


def parse_traceparent(header: str | None) -> dict[str, str] | None:
    """Faz parse de ``traceparent``; None quando o header e ausente ou invalido."""
    if not header:
        return None
    match = TRACEPARENT_RE.match(header.strip().lower())
    if not match:
        return None
    if match.group("trace_id") == "0" * 32 or match.group("span_id") == "0" * 16:
        return None
    return {
        "version": match.group("ver"),
        "trace_id": match.group("trace_id"),
        "parent_span_id": match.group("span_id"),
        "trace_flags": match.group("flags"),
    }


def build_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    flags = "01" if sampled else "00"
    return f"{PROTOCOL_VERSION}-{trace_id}-{span_id}-{flags}"


def b64_id(hex_id: str, size: int) -> str:
    """OTLP/JSON segue o mapeamento protobuf: campos ``bytes`` vao em base64."""
    raw = bytes.fromhex(hex_id.rjust(size * 2, "0"))[:size]
    return base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------------
# metricas
# --------------------------------------------------------------------------


def _labels(**pairs: object) -> str:
    parts = []
    for key, value in pairs.items():
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{key}="{text}"')
    return "{" + ",".join(parts) + "}"


def _bucket_line(prefix: str, base_labels: str, bound: str, value: int) -> str:
    """Linha de bucket cumulativo: ``metrica{labels,le="X"} n``.

    Monta por concatenacao porque ``}`` literal dentro de f-string nao e
    sintaxe valida em Python.
    """
    return prefix + "_seconds_bucket" + base_labels[:-1] + ',le="' + bound + '"} ' + str(value)


class Metrics:
    """Registro minimalista no formato de exposicao do Prometheus.

    Custa ~150 linhas e remove a unica razao tecnica para colocar um pacote de
    terceira parte numa app de exemplo. Em ambiente real usa-se o client
    oficial; a superficie exportada e a mesma.
    """

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._requests: Counter[str] = Counter()
        self._dependency: Counter[str] = Counter()
        self._hist: dict[str, list[int]] = {}
        self._hist_sum: dict[str, float] = {}
        self._hist_count: dict[str, int] = {}
        self._dep_hist: dict[str, list[int]] = {}
        self._dep_hist_sum: dict[str, float] = {}
        self._dep_hist_count: dict[str, int] = {}
        self._in_flight: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()
        self._gauges: dict[str, float] = {}

    # -- escrita ---------------------------------------------------------
    def inc(self, name: str, labels: str = "", value: float = 1.0) -> None:
        with self._lock:
            self._counters[f"{name}{labels}"] += value

    def observe_request(self, route: str, status: int, seconds: float) -> None:
        status_class = f"{status // 100}xx"
        key = _labels(
            service=self.config.service,
            journey=self.config.journey,
            environment=self.config.environment,
            team=self.config.team,
            route=route,
            status=status,
            status_class=status_class,
        )
        hist_key = _labels(
            service=self.config.service,
            journey=self.config.journey,
            environment=self.config.environment,
            team=self.config.team,
            route=route,
        )
        with self._lock:
            self._requests[key] += 1
            buckets = self._hist.setdefault(hist_key, [0] * len(HISTOGRAM_BUCKETS_SECONDS))
            for idx, bound in enumerate(HISTOGRAM_BUCKETS_SECONDS):
                if seconds <= bound:
                    buckets[idx] += 1
            self._hist_sum[hist_key] = self._hist_sum.get(hist_key, 0.0) + seconds
            self._hist_count[hist_key] = self._hist_count.get(hist_key, 0) + 1

    def observe_dependency(self, dependency: str, status: int, seconds: float) -> None:
        status_class = f"{status // 100}xx"
        key = _labels(
            service=self.config.service,
            journey=self.config.journey,
            dependency=dependency,
            status_class=status_class,
        )
        hist_key = _labels(
            service=self.config.service, journey=self.config.journey, dependency=dependency
        )
        with self._lock:
            self._dependency[key] += 1
            buckets = self._dep_hist.setdefault(hist_key, [0] * len(HISTOGRAM_BUCKETS_SECONDS))
            for idx, bound in enumerate(HISTOGRAM_BUCKETS_SECONDS):
                if seconds <= bound:
                    buckets[idx] += 1
            self._dep_hist_sum[hist_key] = self._dep_hist_sum.get(hist_key, 0.0) + seconds
            self._dep_hist_count[hist_key] = self._dep_hist_count.get(hist_key, 0) + 1

    def enter_route(self, route: str) -> None:
        """Gauge de requests em execucao: o label tem que ser valido no /metrics."""
        key = _labels(service=self.config.service, journey=self.config.journey, route=route)
        with self._lock:
            self._in_flight[key] += 1

    def leave_route(self, route: str) -> None:
        key = _labels(service=self.config.service, journey=self.config.journey, route=route)
        with self._lock:
            self._in_flight[key] = max(0, self._in_flight[key] - 1)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def gauge(self, name: str) -> float:
        with self._lock:
            return self._gauges.get(name, 0.0)

    # -- leitura ---------------------------------------------------------
    def snapshot(self) -> dict[str, float]:
        """Leitura agregada usada pelos testes e pelo smoke-test."""
        with self._lock:
            errors = sum(v for k, v in self._requests.items() if 'status_class="5xx"' in k)
            total = sum(self._requests.values())
            return {
                "requests": float(total),
                "errors": float(errors),
                "error_rate": (errors / total) if total else 0.0,
                "in_flight": float(sum(self._in_flight.values())),
            }

    def _render_histogram(self, prefix: str, hist: dict, sums: dict, counts: dict) -> list[str]:
        out = [
            f"# HELP {prefix}_seconds Latencia em segundos, buckets cumulativos.",
            f"# TYPE {prefix}_seconds histogram",
        ]
        for hist_key, buckets in sorted(hist.items()):
            count = counts.get(hist_key, 0)
            total = sums.get(hist_key, 0.0)
            # observe_request ja incrementa todo bucket cujo limite cobre a
            # observacao: os valores armazenados sao cumulativos por construcao,
            # como exige o formato de exposicao. Acumular de novo aqui produziria
            # bucket acima de +Inf e um histogram_quantile sem sentido.
            for idx, bound in enumerate(HISTOGRAM_BUCKETS_SECONDS):
                out.append(_bucket_line(prefix, hist_key, str(bound), buckets[idx]))
            out.append(_bucket_line(prefix, hist_key, "+Inf", count))
            out.append(f"{prefix}_seconds_sum{hist_key} {total:.6f}")
            out.append(f"{prefix}_seconds_count{hist_key} {count}")
        return out

    def render(self) -> str:
        with self._lock:
            out: list[str] = []

            def emit(name: str, kind: str, help_text: str, samples: dict) -> None:
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} {kind}")
                for labels, value in sorted(samples.items()):
                    out.append(f"{name}{labels} {value}")

            emit(
                "http_requests_total",
                "counter",
                "Requests atendidos por rota e classe de status (/health fica fora).",
                dict(self._requests),
            )
            emit(
                "dependency_requests_total",
                "counter",
                "Chamadas de dependencia simulada por destino e classe de status.",
                dict(self._dependency),
            )
            emit(
                "http_requests_in_flight",
                "gauge",
                "Requests em execucao agora, proxy de fila de concurrency.",
                {label: float(v) for label, v in self._in_flight.items()},
            )
            out.extend(
                self._render_histogram("http_request_duration", self._hist, self._hist_sum, self._hist_count)
            )
            out.extend(
                self._render_histogram(
                    "dependency_request_duration", self._dep_hist, self._dep_hist_sum, self._dep_hist_count
                )
            )

            for name, value in sorted(self._counters.items()):
                metric = name.split("{")[0]
                out.append(f"# TYPE {metric} counter")
                out.append(f"{name} {value}")

            base_labels = _labels(
                service=self.config.service,
                journey=self.config.journey,
                environment=self.config.environment,
                team=self.config.team,
                version=self.config.version,
                owner=self.config.owner,
            )
            gauges = [
                (
                    "app_slo_availability_ratio",
                    "Objetivo de disponibilidade declarado pela propria app.",
                    self.config.slo_availability,
                ),
                (
                    "app_slo_latency_seconds",
                    "Objetivo de p95 em segundos; fronteira do bucket de histograma.",
                    SLO_BUCKET,
                ),
                (
                    "app_slo_latency_compliance_ratio",
                    "Fracao de requests que deve ficar abaixo do objetivo de latencia.",
                    self.config.slo_latency_compliance,
                ),
                ("app_info", "Identidade do servico em labels.", 1),
            ]
            for name, help_text, value in gauges:
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} gauge")
                out.append(f"{name}{base_labels} {value}")

            process = [
                ("process_cpu_seconds_total", "counter", "CPU consumida pelo processo."),
                ("process_resident_memory_bytes", "gauge", "RSS do processo."),
                ("process_open_file_descriptors", "gauge", "Descritores de arquivo abertos."),
                ("process_max_file_descriptors", "gauge", "Limite soft de descritores."),
            ]
            for name, kind, help_text in process:
                value = self._gauges.get(name, 0.0)
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} {kind}")
                out.append(f"{name}{base_labels} {value:.6f}")

            return "\n".join(out) + "\n"


def process_metrics(metrics: Metrics) -> None:
    """Atualiza os gauges de saturacao do processo (best effort, stdlib only)."""
    cpu = os.times()
    metrics.set_gauge("process_cpu_seconds_total", cpu.user + cpu.system)
    try:
        import resource

        maxrss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux devolve KiB; o valor e sintetico de qualquer forma.
        metrics.set_gauge("process_resident_memory_bytes", float(maxrss_kb) * 1024.0)
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        metrics.set_gauge("process_max_file_descriptors", float(soft))
    except Exception:  # pragma: no cover - plataforma sem resource
        metrics.set_gauge("process_resident_memory_bytes", 0.0)
        metrics.set_gauge("process_max_file_descriptors", 1024.0)
    try:
        metrics.set_gauge("process_open_file_descriptors", float(len(os.listdir("/proc/self/fd"))))
    except OSError:  # pragma: no cover - fora de Linux
        metrics.set_gauge("process_open_file_descriptors", 12.0)


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------


class Logger:
    """Log JSON no stdout, com espelho opcional em arquivo para o filelog receiver."""

    def __init__(self, config: Config, stream=None):
        self.config = config
        self.stream = stream if stream is not None else sys.stdout
        self.path = os.path.join(config.log_dir, f"{config.service}.log") if config.log_dir else None
        self._lock = threading.Lock()
        if self.path:
            os.makedirs(config.log_dir, exist_ok=True)

    def emit(self, level: str, message: str, **fields: object) -> dict[str, object]:
        now = time.time()
        record: dict[str, object] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            + f".{int((now % 1) * 1000):03d}Z",
            "level": level,
            "service": self.config.service,
            "environment": self.config.environment,
            "journey": self.config.journey,
            "team": self.config.team,
            "owner": self.config.owner,
            "message": message,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        problems = tagging.validate_tags(record, kind="log")
        if problems:  # nunca se derruba um request por causa de log
            record["tag_schema_warnings"] = problems
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            print(line, file=self.stream, flush=True)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:  # pragma: no cover - disco cheio nao derruba a app
                    pass
        return record


# --------------------------------------------------------------------------
# exportador OTLP/JSON
# --------------------------------------------------------------------------


class OtlpJsonExporter:
    """Empacota spans em OTLP/JSON e envia ao Collector por HTTP.

    Mesma intencao do SDK oficial, sem o SDK: mostrar o minimo de contrato que
    um servico precisa cumprir para aparecer no Tempo. Nao implementa retry,
    compressao nem backpressure - isso e papel do SDK em producao, e a
    limitacao esta registrada em app/otel-sidecar.md.
    """

    def __init__(self, config: Config, metrics: Metrics, logger: Logger):
        self.config = config
        self.metrics = metrics
        self.logger = logger
        self.enabled = bool(config.otlp_endpoint)
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            self.logger.emit("info", "exportador OTLP desligado: OTEL_EXPORTER_OTLP_ENDPOINT vazio")
            return
        self.logger.emit("info", "exportador OTLP ativo", endpoint=self.config.otlp_endpoint)
        self._thread = threading.Thread(target=self._run, name="otlp-export", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def add_span(self, span: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            if len(self._buffer) >= 2000:
                self.metrics.inc("spans_dropped_total")
                return
            self._buffer.append(span)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": key, "value": {"stringValue": str(value)}}
                            for key, value in self.config.resource_attributes.items()
                        ]
                    },
                    "scopeSpans": [
                        {"scope": {"name": "app.server", "version": self.config.version}, "spans": batch}
                    ],
                }
            ]
        }
        request = urllib.request.Request(
            f"{self.config.otlp_endpoint}/v1/traces",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                response.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            # Falha de export e cara: trace perdido nao se reconstroi. Por isso
            # e contado como metrica e alertado em infrastructure.yml.
            self.metrics.inc("otlp_export_failures_total")


# --------------------------------------------------------------------------
# spans
# --------------------------------------------------------------------------


def _attr_string(key: str, value: object) -> dict:
    return {"key": key, "value": {"stringValue": str(value)}}


def _attr_int(key: str, value: int) -> dict:
    return {"key": key, "value": {"intValue": str(int(value))}}


def build_server_span(
    context: dict,
    route: str,
    route_spec: dict,
    method: str,
    status: int,
    outcome: str,
    start_unix_ns: int,
    end_unix_ns: int,
    error_type: str | None = None,
    config: Config | None = None,
) -> dict:
    """Span SERVER em OTLP/JSON, com o contexto W3C ja resolvido.

    ``traceId`` e ``spanId`` vao em base64 porque o mapeamento JSON do protobuf
    exige isso em campos ``bytes``; no log o mesmo identificador aparece em hex,
    que e o formato aceito pelo derived field do Loki e pela busca do Tempo.
    """
    attributes = [
        _attr_string("http.request.method", method),
        _attr_string("http.route", route),
        _attr_int("http.response.status_code", status),
        _attr_string("journey", context["journey"]),
        _attr_string("team", context["team"]),
        _attr_string("deployment.environment.name", context["environment"]),
        _attr_string("app.request.id", context["request_id"]),
        _attr_string("app.outcome", outcome),
        _attr_string("app.context.propagated", str(context["propagated"]).lower()),
    ]
    if config is not None:
        attributes.append(_attr_string("owner", config.owner))
    if error_type:
        attributes.append(_attr_string("error.type", error_type))
    span = {
        "traceId": b64_id(context["trace_id"], 16),
        "spanId": b64_id(context["span_id"], 8),
        "name": f"{method} {route_spec['span_name']}",
        "kind": SPAN_KIND_SERVER,
        "startTimeUnixNano": str(start_unix_ns),
        "endTimeUnixNano": str(end_unix_ns),
        "attributes": attributes,
        "status": {"code": 2 if status >= 500 else 1},
    }
    if context.get("parent_span_id"):
        span["parentSpanId"] = b64_id(context["parent_span_id"], 8)
    if error_type:
        span["status"]["message"] = error_type
    return span


def build_dependency_span(
    context: dict,
    dependency: str,
    status: int,
    start_unix_ns: int,
    end_unix_ns: int,
    error_type: str | None = None,
) -> dict:
    """Span CLIENT da dependencia simulada, filho do span do request."""
    attributes = [
        _attr_string("peer.service", dependency),
        _attr_string("rpc.system", "http"),
        _attr_int("response.status_code", status),
        _attr_string("journey", context["journey"]),
    ]
    if error_type:
        attributes.append(_attr_string("error.type", error_type))
    return {
        "traceId": b64_id(context["trace_id"], 16),
        "spanId": b64_id(context["dependency_span_id"], 8),
        "parentSpanId": b64_id(context["span_id"], 8),
        "name": f"call {dependency}",
        "kind": 3,
        "startTimeUnixNano": str(start_unix_ns),
        "endTimeUnixNano": str(end_unix_ns),
        "attributes": attributes,
        "status": {"code": 2 if status >= 500 else 1},
    }


# --------------------------------------------------------------------------
# decisao de request (fora do handler para ser testavel)
# --------------------------------------------------------------------------


def plan_request(config: Config, rng: random.Random, route: str) -> dict:
    """Sorteia latencia com jitter deterministico e os dados do request.

    Um ``random.Random(seed)`` por servidor, protegido por lock: a sequencia e
    reprodutivel, entao um teste consegue afirmar que p95 e taxa de erro saem
    dentro da faixa esperada sem depender de sorte real.
    """
    spec = ROUTES[route]
    base_ms = float(spec["base_ms"]) * config.latency_factor
    jitter_ms = float(spec["jitter_ms"]) * config.latency_factor
    # beta(2, 4) da cauda assimetrica: maioria rapida, cauda acima do bucket 1.2
    # quando o fator de latencia sobe - que e o que se quer ver num painel.
    shape = rng.betavariate(2.0, 4.0)
    return {
        "route": route,
        "planned_ms": base_ms + shape * jitter_ms,
        "roll_dependency": rng.random(),
        "roll_server": rng.random(),
        "roll_client": rng.random(),
    }


def decide_outcome(config: Config, route: str, plan: dict) -> tuple[int, str, str | None, bool]:
    """(status, outcome, error_type, dependencia_falhou) dados os sorteios."""
    if route == "/health":
        return 200, "health_ok", None, False
    dependency = ROUTES[route].get("dependency")
    if dependency and plan["roll_dependency"] < config.dependency_error_rate:
        return 502, "dependency_error", "dependency_timeout", True
    if plan["roll_server"] < config.error_rate:
        return 503, "injected_error", "internal_error_injetado", False
    if plan["roll_client"] < config.client_error_rate:
        return 400, "client_error", "payload_invalido", False
    return 200, "ok", None, False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class RequestHandler(BaseHTTPRequestHandler):
    """Despacho dos tres endpoints do contrato: /pay, /auth e /health."""

    server_version = "obsstack-example/1.0"

    def log_message(self, fmt: str, *args) -> None:
        """Silencia o log textual do ``http.server``: so JSON vai para o stdout."""
        return

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_HEAD(self) -> None:
        self.dispatch("HEAD")

    # -- fluxo -----------------------------------------------------------
    def dispatch(self, method: str) -> None:
        app = self.server  # type: ignore[assignment]
        path = self.path.split("?", 1)[0]
        if path.endswith("/") and path != "/":
            path = path.rstrip("/")
        if path in ("/", "/index.html"):
            self._send(
                200,
                json.dumps(
                    {
                        "service": app.config.service,
                        "journey": app.config.journey,
                        "endpoints": sorted(ROUTES) + ["/metrics"],
                        "sintetico": True,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json",
            )
            return
        if path == "/metrics":
            process_metrics(app.metrics)
            self._send(
                200,
                app.metrics.render().encode("utf-8"),
                "text/plain; version=0.0.4; charset=utf-8",
            )
            return
        if path not in ROUTES:
            self.handle_unknown(method, path)
            return
        self.handle_route(method, path)

    def handle_unknown(self, method: str, path: str) -> None:
        app = self.server  # type: ignore[assignment]
        context = app.accept_context(self.headers)
        app.metrics.inc(
            "http_requests_unmatched_total",
            _labels(service=app.config.service, route="unmatched"),
        )
        app.logger.emit(
            "warn",
            "rota inexistente",
            request_id=context["request_id"],
            trace_id=context["trace_id"],
            route=path,
            status=404,
            duration_ms=0.5,
            outcome="route_not_found",
        )
        body = json.dumps(
            {
                "error": "route_not_found",
                "route": path,
                "known": sorted(ROUTES) + ["/metrics"],
                "request_id": context["request_id"],
                "trace_id": context["trace_id"],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(404, body, "application/json", {"X-Request-Id": context["request_id"]})

    def handle_route(self, method: str, path: str) -> None:
        app = self.server  # type: ignore[assignment]
        cfg = app.config
        route_spec = ROUTES[path]
        is_health = path == "/health"
        context = app.accept_context(self.headers)
        with app.rng_lock:
            plan = plan_request(cfg, app.rng, path)
        status, outcome, error_type, dependency_failed = decide_outcome(cfg, path, plan)

        duration_ms = plan["planned_ms"] + HANDLER_OVERHEAD_MS
        if dependency_failed:
            # Timeout de dependencia e lento: latencia e sintoma junto com erro,
            # e por isso os dois sinais aparecem no mesmo painel de incidente.
            duration_ms += DEPENDENCY_TIMEOUT_MS
        seconds = duration_ms / 1000.0
        start_ns = time.time_ns() - int(duration_ms * 1e6)
        end_ns = start_ns + int(duration_ms * 1e6)

        app.metrics.enter_route(path)
        try:
            dependency = route_spec.get("dependency")
            if dependency and not is_health:
                dep_status = 502 if dependency_failed else 200
                dep_seconds = seconds * 0.65
                app.exporter.add_span(
                    build_dependency_span(
                        context,
                        str(dependency),
                        dep_status,
                        start_ns,
                        start_ns + int(dep_seconds * 1e9),
                        error_type="timeout" if dependency_failed else None,
                    )
                )
                app.metrics.observe_dependency(str(dependency), dep_status, dep_seconds)
            time.sleep(max(0.0, seconds))
        finally:
            app.metrics.leave_route(path)

        # /health fica FORA do SLI: ele mede vivacidade do processo, e cont-lo
        # na taxa de erro dilui o sinal real do comportamento do usuario.
        if not is_health:
            app.metrics.observe_request(path, status, seconds)
            app.record_request_sample(path, duration_ms, status)
        app.exporter.add_span(
            build_server_span(
                context,
                path,
                route_spec,
                method,
                status,
                outcome,
                start_ns,
                end_ns,
                error_type=error_type,
                config=cfg,
            )
        )

        level = "info"
        if status >= 500:
            level = "error"
        elif status >= 400:
            level = "warn"
        elif is_health:
            level = "debug"

        app.logger.emit(
            level,
            f"{method} {path}",
            request_id=context["request_id"],
            trace_id=context["trace_id"],
            span_id=context["span_id"],
            parent_span_id=context.get("parent_span_id") or None,
            route=path,
            status=status,
            status_class=f"{status // 100}xx",
            duration_ms=round(duration_ms, 3),
            outcome=outcome,
            error_type=error_type,
            dependency=route_spec.get("dependency"),
            propagated=context["propagated"],
            latency_factor=cfg.latency_factor,
        )

        payload = {
            "service": cfg.service,
            "journey": cfg.journey,
            "route": path,
            "status": status,
            "outcome": outcome,
            "duration_ms": round(duration_ms, 3),
            "request_id": context["request_id"],
            "trace_id": context["trace_id"],
            "traceparent": context["traceparent"],
        }
        if not is_health:
            payload.update(
                {
                    "window_requests": len(app.recent_durations(path)),
                    "window_p50_ms": round(stats.p50(app.recent_durations(path)), 2),
                    "window_p95_ms": round(stats.p95(app.recent_durations(path)), 2),
                    "window_error_rate": round(stats.error_rate(app.recent_statuses(path)), 4),
                }
            )
        if error_type:
            payload["error_type"] = error_type
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json",
            {
                "X-Request-Id": context["request_id"],
                "X-Trace-Id": context["trace_id"],
                "traceparent": context["traceparent"],
            },
        )


HANDLER_OVERHEAD_MS = 0.4
DEPENDENCY_TIMEOUT_MS = 400.0


# --------------------------------------------------------------------------
# servidor
# --------------------------------------------------------------------------


class AppServer(ThreadingHTTPServer):
    """Servidor com RNG, metricas, logs e exportador de traces compartilhados."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, stream=None):
        self.config = config
        self.metrics = Metrics(config)
        self.logger = Logger(config, stream=stream)
        self.exporter = OtlpJsonExporter(config, self.metrics, self.logger)
        self.rng = random.Random(config.seed)
        self.rng_lock = threading.Lock()
        # Janela deslizante por rota: resumo inline no corpo da resposta e
        # material de conferencia para o smoke-test sem consultar o Prometheus.
        self._recent: dict[str, dict[str, list]] = {
            route: {"durations_ms": [], "statuses": []} for route in ROUTES
        }
        self._recent_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        super().__init__((config.host, config.port), RequestHandler)

    @property
    def url(self) -> str:
        host = self.config.host if self.config.host != "0.0.0.0" else "127.0.0.1"
        return f"http://{host}:{self.server_address[1]}"

    # -- contexto --------------------------------------------------------
    def accept_context(self, headers) -> dict:
        """Resolve trace_id/span_id/request_id a partir do cabecalho recebido.

        Header valido continua o trace existente, e isso que faz dois servicos
        aparecerem no mesmo grafico do Tempo. Header invalido ou ausente comeca
        um trace novo e o log marca ``propagated=false``, porque contexto nao
        propagado e o buraco numero 1 de qualquer esteira de correlacao.
        """
        incoming = parse_traceparent(headers.get("traceparent"))
        request_id = (headers.get("X-Request-Id") or "").strip()[:64]
        with self.rng_lock:
            trace_id = incoming["trace_id"] if incoming else new_trace_id(self.rng)
            span_id = new_span_id(self.rng)
            dependency_span_id = new_span_id(self.rng)
            request_id = request_id or new_span_id(self.rng)
        sampled = bool(incoming) and incoming["trace_flags"].endswith("1")
        return {
            "trace_id": trace_id,
            "span_id": span_id,
            "dependency_span_id": dependency_span_id,
            "parent_span_id": incoming["parent_span_id"] if incoming else "",
            "trace_flags": incoming["trace_flags"] if incoming else "01",
            "propagated": bool(incoming),
            "request_id": request_id,
            "traceparent": build_traceparent(trace_id, span_id, sampled=sampled),
            "service": self.config.service,
            "environment": self.config.environment,
            "journey": self.config.journey,
            "team": self.config.team,
        }

    # -- janela recente --------------------------------------------------
    def record_request_sample(self, route: str, duration_ms: float, status: int) -> None:
        with self._recent_lock:
            bucket = self._recent.setdefault(route, {"durations_ms": [], "statuses": []})
            bucket["durations_ms"].append(duration_ms)
            bucket["statuses"].append(status)
            del bucket["durations_ms"][:-512]
            del bucket["statuses"][:-512]

    def recent_durations(self, route: str) -> list[float]:
        with self._recent_lock:
            return list(self._recent.get(route, {}).get("durations_ms", []))

    def recent_statuses(self, route: str) -> list[int]:
        with self._recent_lock:
            return list(self._recent.get(route, {}).get("statuses", []))

    # -- ciclo de vida ---------------------------------------------------
    def start_thread(self) -> threading.Thread:
        """Sobe ``serve_forever`` em thread daemon (usado por testes e smoke-test)."""
        self._thread = threading.Thread(target=self.serve_forever, name="app-server", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self.exporter.stop()


# --------------------------------------------------------------------------
# entrada
# --------------------------------------------------------------------------

_CLI_OVERRIDES = (
    ("port", "port"),
    ("host", "host"),
    ("service", "service"),
    ("journey", "journey"),
    ("team", "team"),
    ("error_rate", "error_rate"),
    ("latency_factor", "latency_factor"),
    ("seed", "seed"),
    ("log_dir", "log_dir"),
)


def build_config(argv: list[str] | None = None) -> Config:
    """Config a partir de env var, com overrides de linha de comando."""
    parser = argparse.ArgumentParser(description="Aplicacao de exemplo do stack (stdlib only).")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--service", default=None)
    parser.add_argument("--journey", default=None)
    parser.add_argument("--team", default=None)
    parser.add_argument("--error-rate", dest="error_rate", type=float, default=None)
    parser.add_argument("--latency-factor", dest="latency_factor", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--otlp-endpoint", dest="otlp_endpoint", default=None)
    parser.add_argument("--log-dir", dest="log_dir", default=None)
    args = parser.parse_args(argv)

    config = Config.from_env()
    for attr, field in _CLI_OVERRIDES:
        value = getattr(args, attr, None)
        if value is not None:
            setattr(config, field, value)
    if args.journey is not None:
        config.journey = tagging.normalize_journey(args.journey)
    if args.otlp_endpoint is not None:
        config.otlp_endpoint = args.otlp_endpoint.rstrip("/")
    return config


def main(argv: list[str] | None = None) -> int:
    config = build_config(argv)
    server = AppServer(config)
    server.exporter.start()

    def _shutdown(signum, frame) -> None:  # pragma: no cover - depende de sinal externo
        server.logger.emit("info", "sinal recebido, encerrando", signal=signal.Signals(signum).name)
        threading.Thread(target=server.stop, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, RuntimeError):  # pragma: no cover - fora da main thread
            pass

    server.logger.emit(
        "info",
        "aplicacao de exemplo pronta",
        url=server.url,
        routes=sorted(ROUTES) + ["/metrics"],
        error_rate=config.error_rate,
        client_error_rate=config.client_error_rate,
        dependency_error_rate=config.dependency_error_rate,
        latency_factor=config.latency_factor,
        seed=config.seed,
        dados="sinteticos",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
