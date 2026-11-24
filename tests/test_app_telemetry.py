"""Testes da telemetria produzida pela app de exemplo.

Cobre o contrato que o resto do stack consome: toda linha de log de request
carrega ``trace_id``, o ``traceparent`` continua o trace quando vem de fora, e o
``/metrics`` expoe histograma cumulativo sem label de alto cardinalidade.

Nada aqui abre conexao de rede: o ``AppServer`` e instanciado em porta 0 apenas
para exercitar ``accept_context`` e fechado em seguida - nenhum request e
enviado. Os testes do ciclo completo (health, carga, contagem de trace_id) ficam
em ``scripts/smoke-test.sh``.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import server as srv  # noqa: E402
from app import stats, tagging  # noqa: E402

HEX32 = re.compile(r"^[0-9a-f]{32}$")
LINE_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? [-+.eE0-9]+$")


def config(**extra) -> srv.Config:
    base = dict(service="checkout-api", journey="checkout", team="payments-core",
                environment="local-sintetico", version="1.0.0-sintetico",
                error_rate=0.0, client_error_rate=0.0, dependency_error_rate=0.0,
                latency_factor=1.0, seed=2026, host="127.0.0.1", port=0,
                otlp_endpoint="", log_dir="")
    base.update(extra)
    return srv.Config(**base)


class LoggerComTraceIdTest(unittest.TestCase):
    def emitir(self, **campos):
        buf = io.StringIO()
        logger = srv.Logger(config(), stream=buf)
        registro = logger.emit("info", "POST /pay", **campos)
        return registro, buf.getvalue()

    def test_registro_de_request_carrega_trace_id(self) -> None:
        contexto = srv.AppServer(config())
        try:
            ctx = contexto.accept_context({})
        finally:
            contexto.server_close()
        registro, saida = self.emitir(
            request_id=ctx["request_id"], trace_id=ctx["trace_id"],
            span_id=ctx["span_id"], route="/pay", status=200, status_class="2xx",
            duration_ms=241.5, outcome="ok", propagated=ctx["propagated"],
        )
        self.assertRegex(str(registro["trace_id"]), HEX32)
        self.assertEqual(registro["trace_id"], ctx["trace_id"])

        # a linha escrita no stdout e a mesma coisa, em JSON de uma linha
        self.assertEqual(1, len(saida.strip().splitlines()))
        gravado = json.loads(saida.strip().splitlines()[-1])
        self.assertEqual(registro["trace_id"], gravado["trace_id"])
        self.assertIn("trace_id", gravado)

    def test_log_obedece_o_esquema_de_tags(self) -> None:
        _, saida = self.emitir(route="/pay", status=200)
        gravado = json.loads(saida.strip().splitlines()[-1])
        self.assertNotIn("tag_schema_warnings", gravado)
        self.assertEqual([], tagging.validate_tags(gravado, kind="log"))
        for obrigatorio in tagging.REQUIRED_TAGS:
            self.assertIn(obrigatorio, gravado)

    def test_toda_linha_do_logger_e_json_valido(self) -> None:
        buf = io.StringIO()
        logger = srv.Logger(config(), stream=buf)
        for nivel in ("debug", "info", "warn", "error"):
            logger.emit(nivel, "mensagem", trace_id="a" * 32)
        for linha in buf.getvalue().strip().splitlines():
            doc = json.loads(linha)
            self.assertIn(doc["level"], tagging.ALLOWED_SEVERITIES)
            self.assertIn("timestamp", doc)

    def test_esquema_de_traceparent(self) -> None:
        tp = srv.build_traceparent("0" * 31 + "1", "f" * 16)
        self.assertTrue(tp.startswith("00-"))
        self.assertTrue(tp.endswith("-01"))
        parseado = srv.parse_traceparent(tp)
        self.assertIsNotNone(parseado)
        self.assertEqual("0" * 31 + "1", parseado["trace_id"])


class AcceptContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = srv.AppServer(config())
        self.addCleanup(self.app.server_close)

    def test_sem_header_comeca_trace_novo_marca_nao_propagado(self) -> None:
        ctx = self.app.accept_context({})
        self.assertRegex(ctx["trace_id"], HEX32)
        self.assertFalse(ctx["propagated"])
        self.assertEqual("", ctx["parent_span_id"])
        # raiz criada pela app nao se auto-amostra: o header devolvido carrega
        # flags 00, e a decisao de amostragem fica com a jornada que chama
        self.assertEqual(f"00-{ctx['trace_id']}-{ctx['span_id']}-00", ctx["traceparent"])

    def test_header_valido_continua_o_mesmo_trace(self) -> None:
        trace_id = "ab" * 16
        ctx = self.app.accept_context({"traceparent": f"00-{trace_id}-{'cd' * 8}-01"})
        self.assertEqual(trace_id, ctx["trace_id"])
        self.assertTrue(ctx["propagated"])
        self.assertEqual("cd" * 8, ctx["parent_span_id"])

    def test_header_invalido_vira_trace_novo(self) -> None:
        invalidos = (
            "00-xyz-1-01",                              # nao hex
            "",                                         # vazio
            "00-" + "0" * 32 + "-" + "cd" * 8 + "-01",  # trace_id todo zero
            "00-" + "ab" * 16 + "-" + "0" * 16 + "-01",  # span_id todo zero
            "00-" + "ab" * 16 + "-" + "cd" * 8,         # faltando flags
            "00-" + "ab" * 8 + "-" + "cd" * 8 + "-01",  # trace_id curto
        )
        for invalido in invalidos:
            ctx = self.app.accept_context({"traceparent": invalido})
            self.assertFalse(ctx["propagated"], invalido)
            self.assertRegex(ctx["trace_id"], HEX32)

    def test_dois_requests_sem_contexto_tem_traces_diferentes(self) -> None:
        primeiro = self.app.accept_context({})["trace_id"]
        segundo = self.app.accept_context({})["trace_id"]
        self.assertNotEqual(primeiro, segundo)


class MetricsRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.metrics = srv.Metrics(config())
        for _ in range(8):
            self.metrics.observe_request("/pay", 200, 0.24)
        for _ in range(2):
            self.metrics.observe_request("/pay", 503, 1.9)
        self.texto = self.metrics.render()

    def test_familias_esperadas_pelas_recording_rules(self) -> None:
        for familia in ("http_requests_total", "http_request_duration_seconds_bucket",
                        "http_request_duration_seconds_count", "app_slo_availability_ratio",
                        "app_slo_latency_seconds"):
            self.assertIn(familia, self.texto, familia)

    def test_histograma_cumulativo_com_infinito(self) -> None:
        linhas = [l for l in self.texto.splitlines()
                  if l.startswith("http_request_duration_seconds_bucket")]
        self.assertTrue(linhas)
        valores = []
        for linha in linhas:
            match = re.search(r'le="([^"]+)"\} ([0-9.eE+-]+)$', linha)
            self.assertIsNotNone(match, linha)
            valores.append((match.group(1), float(match.group(2))))
        # monotonia: bucket cumulativo nunca diminue
        vistos = [v for _, v in valores]
        self.assertEqual(sorted(vistos), vistos)
        valores_le = dict(valores)
        self.assertEqual(11, len(valores))           # 10 cortes + +Inf
        self.assertEqual(10.0, valores_le["+Inf"])   # +Inf == count da serie
        self.assertEqual(0.0, valores_le["0.05"])
        self.assertEqual(10.0, valores_le["5.0"])
        self.assertLessEqual(max(v for le, v in valores if le != "+Inf"),
                             valores_le["+Inf"])

    def test_nenhum_label_proibido_na_exposicao(self) -> None:
        for proibido in tagging.FORBIDDEN_METRIC_LABELS:
            self.assertNotIn(f'{proibido}="', self.texto, proibido)

    def test_formato_de_exposicao(self) -> None:
        for linha in self.texto.splitlines():
            if not linha or linha.startswith("#"):
                continue
            self.assertRegex(linha, LINE_RE, linha)

    def test_snapshot_bate_com_stats(self) -> None:
        """O numero que a app mostra local e o mesmo criterio do runbook."""
        snapshot = self.metrics.snapshot()
        self.assertEqual(10.0, snapshot["requests"])
        self.assertEqual(2.0, snapshot["errors"])
        self.assertEqual(stats.error_rate([200] * 8 + [503] * 2), snapshot["error_rate"])

    def test_labels_de_identidade_estao_presentes(self) -> None:
        for label in tagging.REQUIRED_TAGS:
            self.assertIn(f'{label}="', self.texto, label)

    def test_slo_declarado_pela_app(self) -> None:
        valores = {}
        for linha in self.texto.splitlines():
            if not linha or linha.startswith("#"):
                continue
            nome, _, valor = linha.partition(" ")
            valores.setdefault(nome.split("{")[0], []).append(valor.strip())
        self.assertEqual(["0.995"], valores["app_slo_availability_ratio"])
        self.assertEqual(["1.2"], valores["app_slo_latency_seconds"])
        self.assertEqual(["0.99"], valores["app_slo_latency_compliance_ratio"])


class PlanosSinteticosTest(unittest.TestCase):
    def test_plano_e_deterministico_com_semente(self) -> None:
        import random

        cfg = config()
        um = srv.plan_request(cfg, random.Random(2026), "/pay")
        dois = srv.plan_request(cfg, random.Random(2026), "/pay")
        self.assertEqual(um, dois)

    def test_outcome_dos_desfechos_possiveis(self) -> None:
        cfg = config(error_rate=1.0)
        status, outcome, erro, _ = srv.decide_outcome(
            cfg, "/pay", {"roll_dependency": 0.9, "roll_server": 0.1, "roll_client": 0.9}
        )
        self.assertEqual(503, status)
        self.assertEqual("injected_error", outcome)
        self.assertEqual("internal_error_injetado", erro)

    def test_health_nao_e_contado_como_erro(self) -> None:
        cfg = config(error_rate=1.0)
        status, outcome, _, _ = srv.decide_outcome(
            cfg, "/health", {"roll_dependency": 0.0, "roll_server": 0.0, "roll_client": 0.0}
        )
        self.assertEqual((200, "health_ok"), (status, outcome))

    def test_rotas_do_contrato(self) -> None:
        self.assertEqual({"/pay", "/auth", "/health"}, set(srv.ROUTES))


if __name__ == "__main__":
    unittest.main()
