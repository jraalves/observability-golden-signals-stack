"""Testes estruturais dos arquivos de rules do Prometheus.

Reusa as funcoes do ``scripts/validate.py`` para que o CI e a revisao local
confiram exatamente o mesmo contrato - sem duplicar regra em dois lugares. Os
casos negativos existem para provar que o validador continua detectando erro: um
teste que so afirma que o estado atual e valido nao protege ninguem de um
validador enfraquecido.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for _path in (ROOT, SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import validate  # noqa: E402


class RuleFilesReaisTest(unittest.TestCase):
    """Os tres arquivos versionados atendem ao contrato do projeto."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.errors: list[str] = []
        cls.alerts = validate.validate_rule_files(cls.errors)
        cls.recorded = validate.collect_recording_rules()

    def test_arquivos_existem(self) -> None:
        for rel in validate.RULE_FILES:
            self.assertTrue(os.path.exists(validate.abspath(rel)), rel)

    def test_nenhum_erro_estrutural(self) -> None:
        self.assertEqual([], self.errors, "\n".join(self.errors))

    def test_volume_minimo_de_alertas_e_gravacoes(self) -> None:
        self.assertGreaterEqual(len(self.alerts), 30)
        self.assertGreaterEqual(len(self.recorded), 50)
        self.assertEqual(len(self.alerts), len(set(self.alerts)))

    def test_todos_os_alertas_tem_runbook_no_repositorio(self) -> None:
        for rel in validate.RULE_FILES:
            doc = validate.load_yaml(rel, [])
            self.assertIsNotNone(doc)
            for _, rule in validate.iter_rules(doc or {}):
                if "alert" not in rule:
                    continue
                url = validate.get_path(rule, "annotations.runbook_url")
                alvo = validate.runbook_target_to_path(url)
                self.assertIsNotNone(alvo, f"{rule['alert']}: {url}")
                self.assertTrue(os.path.exists(validate.abspath(alvo)),
                                f"{rule['alert']}: runbook ausente {alvo}")

    def test_vocabulario_de_labels(self) -> None:
        for rel in validate.RULE_FILES:
            doc = validate.load_yaml(rel, [])
            for _, rule in validate.iter_rules(doc or {}):
                if "alert" not in rule:
                    continue
                self.assertIn(validate.get_path(rule, "labels.severity"),
                              validate.ALLOWED_SEVERITIES)
                self.assertIn(validate.get_path(rule, "labels.signal"),
                              validate.ALLOWED_SIGNALS)
                self.assertTrue(validate.get_path(rule, "labels.team"))

    def test_metricas_de_recording_rule_referenciadas_existem(self) -> None:
        """Nada de alerta consumindo serie que nenhuma regra produz."""
        for rel in validate.RULE_FILES:
            doc = validate.load_yaml(rel, [])
            for _, rule in validate.iter_rules(doc or {}):
                expr = str(rule.get("expr", ""))
                for token in validate.colon_identifiers(expr):
                    if token not in self.recorded:
                        self.fail(
                            f"{rel} ({rule.get('alert') or rule.get('record')}) "
                            f"referencia {token}, que nao e uma recording rule"
                        )

    def test_recording_rules_nao_usam_for_nem_severity(self) -> None:
        for rel in validate.RULE_FILES:
            doc = validate.load_yaml(rel, [])
            for _, rule in validate.iter_rules(doc or {}):
                if "record" not in rule:
                    continue
                self.assertNotIn("for", rule)
                self.assertIsNone(validate.get_path(rule, "labels.severity"))

    def test_matriz_de_runbooks_cobre_os_alertas(self) -> None:
        erros: list[str] = []
        validate.validate_runbooks(erros, self.alerts)
        self.assertEqual([], erros, "\n".join(erros))


class HelpersAritmeticosTest(unittest.TestCase):
    def test_is_duration(self) -> None:
        for valido in ("15s", "5m", "1h", "14d", "250ms", "1w"):
            self.assertTrue(validate.is_duration(valido), valido)
        for invalido in ("15", "5 minutos", "s", "1.5h", "", "60"):
            self.assertFalse(validate.is_duration(invalido), invalido)

    def test_janelas_de_tempo_dentro_do_expr(self) -> None:
        expr = 'sum by (service) (rate(http_requests_total{status_class="5xx"}[5m]))'
        import re

        janelas = re.findall(r"\[([^\]]*)\]", validate.strip_quoted(expr))
        self.assertEqual(["5m"], janelas)
        self.assertTrue(all(validate.is_duration(j) for j in janelas))

    def test_balanced_detecta(self) -> None:
        self.assertIsNone(validate.check_balanced('a{b="x"} / c'))
        self.assertIsNotNone(validate.check_balanced("a / (b"))
        self.assertIsNotNone(validate.check_balanced('a{b="x}'))
        self.assertIsNotNone(validate.check_balanced("a] / b"))

    def test_strip_quoted_protege_strings(self) -> None:
        limpo = validate.strip_quoted('up{job="prometheus:9090"}')
        self.assertNotIn("9090", limpo)

    def test_colon_identifiers(self) -> None:
        tokens = validate.colon_identifiers(
            'slo:availability:burn_rate:1h > 3 and on (service) service:http_error_ratio:rate5m'
        )
        self.assertIn("slo:availability:burn_rate:1h", tokens)
        self.assertIn("service:http_error_ratio:rate5m", tokens)
        self.assertNotIn("service", tokens)


class RegrasQuebradasTest(unittest.TestCase):
    """O validador continua pegando o que nao pode passar."""

    def _doc(self, rule: dict) -> dict:
        return {"groups": [{"name": "grupo-teste", "rules": [rule]}]}

    def test_alerta_sem_labels_obrigatorios(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({"alert": "QualquerCoisa", "expr": "up == 0"}),
            "teste.yml",
            recorded=set(),
        )
        texto = "\n".join(erros)
        for obrigatorio in ("severity", "for", "labels.team", "labels.signal",
                            "annotations.summary", "annotations.runbook_url"):
            self.assertIn(obrigatorio, texto)

    def test_severity_fora_do_vocabulario(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "X",
                "expr": "up == 0",
                "for": "5m",
                "labels": {"severity": "fatal", "team": "sre", "signal": "errors"},
                "annotations": {"summary": "s", "runbook_url": "runbooks/rb-taxa-erro.md"},
            }),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("severity" in e for e in erros), erros)

    def test_signal_inventado(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "Y",
                "expr": "up == 0",
                "for": "5m",
                "labels": {"severity": "warning", "team": "sre", "signal": "vibe"},
                "annotations": {"summary": "s", "runbook_url": "runbooks/rb-taxa-erro.md"},
            }),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("signal" in e for e in erros), erros)

    def test_recording_rule_fantasma(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "Z",
                "expr": "service:http_nada_por_acaso:rate5m > 1",
                "for": "5m",
                "labels": {"severity": "warning", "team": "sre", "signal": "errors"},
                "annotations": {"summary": "s", "runbook_url": "runbooks/rb-taxa-erro.md"},
            }),
            "teste.yml",
            recorded={"service:http_error_ratio:rate5m"},
        )
        self.assertTrue(any("nao existem" in e for e in erros), erros)

    def test_runbook_apontando_para_arquivo_ausente(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "W",
                "expr": "up == 0",
                "for": "5m",
                "labels": {"severity": "warning", "team": "sre", "signal": "errors"},
                "annotations": {"summary": "s",
                                "runbook_url": "runbooks/rb-que-ninguem-escreveu.md"},
            }),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("arquivo ausente" in e for e in erros), erros)

    def test_expr_desequilibrado(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "V",
                "expr": "sum(rate(http_requests_total[5m])) / (sum(rate(",
                "for": "5m",
                "labels": {"severity": "warning", "team": "sre", "signal": "traffic"},
                "annotations": {"summary": "s", "runbook_url": "runbooks/rb-throughput.md"},
            }),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("sintaxe duvidosa" in e for e in erros), erros)

    def test_janela_invalida(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "U",
                "expr": "rate(http_requests_total[5 minutos]) > 1",
                "for": "5m",
                "labels": {"severity": "warning", "team": "sre", "signal": "traffic"},
                "annotations": {"summary": "s", "runbook_url": "runbooks/rb-throughput.md"},
            }),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("janela de tempo invalida" in e for e in erros), erros)

    def test_alert_e_record_juntos(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({"alert": "T", "record": "t:cosita", "expr": "up"}),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("juntos" in e for e in erros), erros)

    def test_alerta_sem_for_para_na_primeira_avaliacao(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({
                "alert": "S",
                "expr": "up == 0",
                "labels": {"severity": "critical", "team": "sre", "signal": "errors"},
                "annotations": {"summary": "s", "runbook_url": "runbooks/rb-taxa-erro.md"},
            }),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("precisam de 'for'" in e for e in erros), erros)

    def test_recording_rule_com_for(self) -> None:
        erros = validate.validate_rule_doc(
            self._doc({"record": "a:b", "expr": "up", "for": "5m"}),
            "teste.yml",
            recorded=set(),
        )
        self.assertTrue(any("recording rules nao usam" in e for e in erros), erros)


if __name__ == "__main__":
    unittest.main()
