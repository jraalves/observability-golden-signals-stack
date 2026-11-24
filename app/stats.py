"""Estatisticas de golden signals em stdlib, para a app e para os testes.

As funcoes abaixo sao o espelho local do que o Prometheus calcula com
``histogram_quantile`` e ``rate``. A razao de existirem em codigo e pragmatica:
o runbook precisa de um numero rapido a partir do log de uma instancia, o teste
precisa conferir a conta, e o resultado precisa ser o mesmo criterio que a
recording rule usa (nearest-rank, e nao interpolacao linear ingenua).
"""

from __future__ import annotations

import math

__all__ = ["percentile", "p50", "p95", "p99", "error_rate", "summarize", "mean"]


def percentile(values: list[float] | tuple[float, ...], q: float) -> float:
    """Percentil pelo metodo nearest-rank.

    nearest-rank: o menor valor cujo percentil de posicao e >= q. O Prometheus
    faz interpolacao linear dentro do bucket, entao este resultado e uma
    aproximacao de mesma ordem, nao identica - e isso que o teste documenta.
    """
    if not values:
        return 0.0
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentil fora de 0..100: {q!r}")
    ordered = sorted(values)
    rank = max(1, math.ceil((q / 100.0) * len(ordered)))
    return float(ordered[min(rank - 1, len(ordered) - 1)])


def p50(values: list[float]) -> float:
    return percentile(values, 50)


def p95(values: list[float]) -> float:
    return percentile(values, 95)


def p99(values: list[float]) -> float:
    return percentile(values, 99)


def error_rate(statuses: list[int] | tuple[int, ...]) -> float:
    """Proporcao de respostas 5xx. 4xx NAO queima budget de erro.

    Essa distincao e a fonte numero 1 de alerta falso: validar entrada do
    usuario com 400 nao e indisponibilidade do servico.
    """
    total = len(statuses)
    if total == 0:
        return 0.0
    errors = sum(1 for code in statuses if int(code) >= 500)
    return errors / total


def mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def summarize(durations_ms: list[float], statuses: list[int]) -> dict[str, float]:
    """Resumo de golden signals de uma janela de requests."""
    return {
        "requests": float(len(durations_ms)),
        "p50_ms": p50(durations_ms),
        "p95_ms": p95(durations_ms),
        "p99_ms": p99(durations_ms),
        "avg_ms": mean(durations_ms),
        "error_rate": error_rate(statuses),
    }
