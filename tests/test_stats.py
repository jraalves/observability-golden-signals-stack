"""Testes de ``app/stats.py``: percentile nearest-rank e taxa de erro.

E o espelho local do que o Prometheus calcula com ``histogram_quantile`` e
``rate``. As assercoes usam listas fixas - dado sintetico deterministico - porque
teste com lista aleatoria nao documenta criterio nenhum.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import stats  # noqa: E402

CEM = list(range(1, 101))          # 1..100, em milissegundos sinteticos


class PercentileTest(unittest.TestCase):
    def test_nearest_rank_em_1_a_100(self) -> None:
        self.assertEqual(50.0, stats.p50(CEM))
        self.assertEqual(95.0, stats.p95(CEM))
        self.assertEqual(99.0, stats.p99(CEM))
        self.assertEqual(100.0, stats.percentile(CEM, 100))
        self.assertEqual(1.0, stats.percentile(CEM, 0))

    def test_entrada_desordenada_nao_muda_resultado(self) -> None:
        embaralhado = list(reversed(CEM)) + [7, 3]
        self.assertEqual(stats.p95(CEM + [7, 3]), stats.p95(embaralhado))

    def test_lista_vira_zero_e_nao_excecao(self) -> None:
        self.assertEqual(0.0, stats.percentile([], 95))
        self.assertEqual(0.0, stats.p95([]))
        self.assertEqual(0.0, stats.mean([]))

    def test_elemento_unico(self) -> None:
        self.assertEqual(42.0, stats.p95([42]))
        self.assertEqual(42.0, stats.p99([42]))

    def test_rank_arredonda_para_cima(self) -> None:
        """Com 10 valores, p95 e o decimo: ceil(0.95 * 10) = 10."""
        self.assertEqual(10.0, stats.p95([float(x) for x in range(1, 11)]))
        self.assertEqual(9.0, stats.percentile([float(x) for x in range(1, 11)], 85))

    def test_percentil_fora_de_0_100(self) -> None:
        with self.assertRaises(ValueError):
            stats.percentile(CEM, 101)
        with self.assertRaises(ValueError):
            stats.percentile(CEM, -1)

    def test_monotonia(self) -> None:
        valores = [1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0, 89.0, 144.0, 233.0]
        self.assertLessEqual(stats.p50(valores), stats.p95(valores))
        self.assertLessEqual(stats.p95(valores), stats.p99(valores))
        self.assertLessEqual(stats.mean(valores), stats.p99(valores))

    def test_cauda_larga_aparece_no_p99_antes_do_p95(self) -> None:
        # nearest-rank so mostra a cauda no p99 quando ela passa de 1% da amostra
        cauda = [10.0] * 95 + [5000.0] * 5
        self.assertEqual(10.0, stats.p95(cauda))
        self.assertEqual(5000.0, stats.p99(cauda))
        self.assertEqual(259.5, stats.mean(cauda))
        # a media subiu 25x e continua parecendo "tudo bem"; o p99 nomeia o problema
        self.assertGreater(stats.p99(cauda), stats.mean(cauda) * 19)


class ErrorRateTest(unittest.TestCase):
    def test_tudo_ok(self) -> None:
        self.assertEqual(0.0, stats.error_rate([200, 201, 200]))

    def test_lista_vazia(self) -> None:
        self.assertEqual(0.0, stats.error_rate([]))

    def test_4xx_nao_queima_budget_de_erro(self) -> None:
        """O contrato do SLI: validar entrada do usuario nao e indisponibilidade."""
        self.assertEqual(0.0, stats.error_rate([400, 404, 200, 200]))

    def test_proporcao_exata(self) -> None:
        self.assertEqual(0.5, stats.error_rate([200, 200, 500, 503]))
        self.assertEqual(0.25, stats.error_rate([200, 200, 200, 502]))

    def test_limiar_do_alerta(self) -> None:
        """Reproduz o corte de ErrorRatioHigh (>0,02) e do critico (>0,05)."""
        statuses = [200] * 97 + [500, 500, 500]
        taxa = stats.error_rate(statuses)
        self.assertGreater(taxa, 0.02)
        self.assertLess(taxa, 0.05)

    def test_status_como_string_de_header(self) -> None:
        self.assertEqual(0.5, stats.error_rate(["200", "500"]))  # type: ignore[list-item]


class SummarizeTest(unittest.TestCase):
    def test_chaves_e_consistencia(self) -> None:
        dur = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 500.0]
        st = [200, 200, 200, 200, 200, 200, 200, 200, 503, 502]
        resumo = stats.summarize(dur, st)
        self.assertEqual({"requests", "p50_ms", "p95_ms", "p99_ms", "avg_ms",
                          "error_rate"}, set(resumo))
        self.assertEqual(10.0, resumo["requests"])
        self.assertEqual(0.2, round(resumo["error_rate"], 4))
        self.assertEqual(500.0, resumo["p95_ms"])
        self.assertLessEqual(resumo["p50_ms"], resumo["p95_ms"])

    def test_vazios(self) -> None:
        resumo = stats.summarize([], [])
        self.assertEqual(0.0, resumo["requests"])
        self.assertEqual(0.0, resumo["error_rate"])
        self.assertEqual(0.0, resumo["p95_ms"])


if __name__ == "__main__":
    unittest.main()
