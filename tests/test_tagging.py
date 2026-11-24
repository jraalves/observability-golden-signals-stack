"""Testes do esquema de tags executavel em ``app/tagging.py``.

O schema e o que impede painel vazio depois de deploy e TSDB cheio por
criatividade (``ADR-0004``). Se ele deixar de valer em codigo, nao adianta o
documento em ``otel/tagging.md`` continuar bonito.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import tagging  # noqa: E402


class VocabularioTest(unittest.TestCase):
    def test_tags_obrigatorias(self) -> None:
        self.assertEqual(("service", "environment", "journey", "team"),
                         tagging.REQUIRED_TAGS)

    def test_obrigatorias_nao_sao_proibidas(self) -> None:
        """Um label obrigatorio jamais pode estar na lista de proibidos."""
        self.assertFalse(set(tagging.REQUIRED_TAGS) & set(tagging.FORBIDDEN_METRIC_LABELS))

    def test_trace_id_proibido_como_label(self) -> None:
        self.assertIn("trace_id", tagging.FORBIDDEN_METRIC_LABELS)
        self.assertIn("request_id", tagging.FORBIDDEN_METRIC_LABELS)
        self.assertIn("user_id", tagging.FORBIDDEN_METRIC_LABELS)

    def test_stream_labels_proibidos_sao_superconjunto(self) -> None:
        self.assertTrue(set(tagging.FORBIDDEN_METRIC_LABELS).issubset(
            set(tagging.FORBIDDEN_LOG_STREAM_LABELS)))
        for conteudo in ("message", "body", "stacktrace"):
            self.assertIn(conteudo, tagging.FORBIDDEN_LOG_STREAM_LABELS)

    def test_jornadas_sao_finitas(self) -> None:
        self.assertLessEqual(len(tagging.JOURNEYS), 8)
        self.assertEqual(4, len(tagging.JOURNEYS))


class LabelizeTest(unittest.TestCase):
    def test_slug_estavel(self) -> None:
        self.assertEqual("pagina-inicial", tagging.labelize("Pagina Inicial"))
        self.assertEqual("checkout-api", tagging.labelize("  checkout__API  "))
        self.assertEqual("desconhecido", tagging.labelize("   "))
        self.assertEqual("desconhecido", tagging.labelize(""))

    def test_idempotente(self) -> None:
        for valor in ("Checkout Flow", "login", "Notifica??es", "a  B__c"):
            uma = tagging.labelize(valor)
            self.assertEqual(uma, tagging.labelize(uma), valor)


class NormalizeJourneyTest(unittest.TestCase):
    def test_valores_conhecidos(self) -> None:
        self.assertEqual("checkout", tagging.normalize_journey("Checkout"))
        self.assertEqual("checkout", tagging.normalize_journey("CHECKOUT"))
        self.assertEqual("login", tagging.normalize_journey("login"))
        self.assertEqual("descoberta", tagging.normalize_journey("descoberta"))
        self.assertEqual("notificacao", tagging.normalize_journey("notificacao"))

    def test_prefixo_case_ingoroso(self) -> None:
        self.assertEqual("checkout", tagging.normalize_journey("checkout flow v3"))
        self.assertEqual("descoberta", tagging.normalize_journey("Descoberta"))

    def test_fora_do_vocabulario_vira_outra(self) -> None:
        self.assertEqual("outra", tagging.normalize_journey("campanha-de-natal"))
        self.assertEqual("outra", tagging.normalize_journey(None))
        self.assertEqual("outra", tagging.normalize_journey(""))

    def test_journey_normalizada_nunca_muda_duas_vezes(self) -> None:
        for valor in ("checkout", "Checkout Flow", "nada-a-ver"):
            uma = tagging.normalize_journey(valor)
            self.assertEqual(uma, tagging.normalize_journey(uma))


class ResourceAttributesTest(unittest.TestCase):
    def test_semantica_otel_presente(self) -> None:
        attrs = tagging.resource_attributes("checkout-api", "local-sintetico",
                                           "checkout", "payments-core")
        self.assertEqual("checkout-api", attrs["service.name"])
        self.assertEqual("local-sintetico", attrs["deployment.environment.name"])
        self.assertEqual("checkout", attrs["journey"])
        self.assertEqual("payments-core", attrs["team"])
        self.assertIn("cost_center", attrs)
        self.assertTrue(attrs["service.instance.id"].startswith("checkout-api"))

    def test_owner_default_levado_pelo_time(self) -> None:
        attrs = tagging.resource_attributes("auth-api", "local-sintetico",
                                            "login", "identity")
        self.assertEqual("identity-oncall", attrs["owner"])

    def test_obrigatorias_viajam_para_metric(self) -> None:
        """O que o painel usa para agregar precisa existir no resource."""
        attrs = tagging.resource_attributes("search-api", "local-sintetico",
                                           "descoberta", "discovery")
        mapa = {"service": "service.name",
                "environment": "deployment.environment.name",
                "journey": "journey", "team": "team"}
        for tag in tagging.REQUIRED_TAGS:
            self.assertIn(mapa[tag], attrs, f"resource attributes sem {tag}")


class ValidateTagsTest(unittest.TestCase):
    def registro(self, **extra):
        base = {
            "timestamp": "2026-11-24T10:00:00.000Z",
            "level": "info",
            "service": "checkout-api",
            "environment": "local-sintetico",
            "journey": "checkout",
            "team": "payments-core",
            "owner": "payments-core-oncall",
            "message": "POST /pay",
        }
        base.update(extra)
        return base

    def test_registro_bom_nao_gera_problema(self) -> None:
        self.assertEqual([], tagging.validate_tags(self.registro(), kind="log"))

    def test_tag_obrigatoria_ausente(self) -> None:
        registro = self.registro()
        del registro["team"]
        problemas = tagging.validate_tags(registro, kind="log")
        self.assertTrue(any("team" in p for p in problemas), problemas)

    def test_tag_vazia_tambemconta_como_ausente(self) -> None:
        problemas = tagging.validate_tags(self.registro(service=""), kind="log")
        self.assertTrue(any("service" in p for p in problemas), problemas)

    def test_journey_fora_do_vocabulario(self) -> None:
        problemas = tagging.validate_tags(self.registro(journey="black-friday"), kind="log")
        self.assertTrue(any("journey" in p for p in problemas), problemas)

    def test_level_invalido(self) -> None:
        problemas = tagging.validate_tags(self.registro(level="trace"), kind="log")
        self.assertTrue(any("level" in p for p in problemas), problemas)

    def test_message_vazia(self) -> None:
        problemas = tagging.validate_tags(self.registro(message=""), kind="log")
        self.assertTrue(any("campo de log vazio" in p for p in problemas), problemas)

    def test_metrica_com_trace_id_e_rejeitada(self) -> None:
        metrica = {"service": "checkout-api", "environment": "local-sintetico",
                   "journey": "checkout", "team": "payments-core",
                   "trace_id": "0" * 31 + "1", "request_id": "abc"}
        problemas = tagging.validate_tags(metrica, kind="metric")
        texto = " ".join(problemas)
        self.assertIn("alto cardinalidade", texto)
        self.assertIn("trace_id", texto)
        self.assertIn("request_id", texto)

    def test_log_com_trace_id_no_corpo_e_aceito(self) -> None:
        """Correlacao por trace_id no log e o contrato do ADR-0002."""
        registro = self.registro(trace_id="a" * 32, request_id="b" * 16)
        self.assertEqual([], tagging.validate_tags(registro, kind="log"))


class CardinalityRiskTest(unittest.TestCase):
    def test_classificacao(self) -> None:
        risco = tagging.cardinality_risk(
            ["trace_id", "user_id", "service", "route", "status", "inventado"]
        )
        self.assertEqual("alta", risco["trace_id"])
        self.assertEqual("alta", risco["user_id"])
        self.assertEqual("baixa", risco["service"])
        self.assertEqual("baixa", risco["route"])
        self.assertEqual("media", risco["status"])
        self.assertEqual("media", risco["inventado"])

    def test_nada_e_baixo_por_acaso(self) -> None:
        risco = tagging.cardinality_risk(tagging.FORBIDDEN_METRIC_LABELS)
        self.assertEqual({"alta"}, set(risco.values()))


if __name__ == "__main__":
    unittest.main()
