"""Testes de contrato do repositorio: docs, dados sintetico e conta de custo.

Segura tres coisas que o validador estrutural ja faz e que aqui viram teste para
que a rodada de CI falhe no arquivo certo:

* o repositorio inteiro passa no ``scripts/validate.py`` (inclusive a varredura de
  termos protegidos, que e o teste de anonimato);
* toda referencia a ``ADR-000N`` em runbook ou README aponta para um arquivo que
  existe em ``docs/decisoes/``;
* os numeros colados em ``docs/custos.md`` sao os que
  ``scripts/custo-cardinalidade.py`` calcula agora - documento que diverge do
  script e documento mentiroso.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for _path in (ROOT, SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import miniyaml  # noqa: E402
import validate  # noqa: E402


def carregar_modulo_custos():
    caminho = os.path.join(SCRIPTS, "custo-cardinalidade.py")
    espec = importlib.util.spec_from_file_location("custo_cardinalidade", caminho)
    modulo = importlib.util.module_from_spec(espec)
    espec.loader.exec_module(modulo)  # type: ignore[union-attr]
    return modulo


class ValidadorCompletoTest(unittest.TestCase):
    def test_contrato_inteiro_sem_erros(self) -> None:
        erros, contagens = validate.validate_repo()
        self.assertEqual([], erros, "\n".join(erros))
        self.assertGreaterEqual(contagens["alerts"], 30)
        self.assertGreaterEqual(contagens["recording rules"], 50)
        self.assertEqual(3, contagens["dashboards"])
        self.assertGreater(contagens["painéis"], 20)

    def test_anonimato_zero_ocorrencias(self) -> None:
        """O teste de anonimato do projeto: nenhum termo protegido em texto."""
        erros: list[str] = []
        validate.validate_protected_terms(erros)
        self.assertEqual([], erros, "\n".join(erros))

    def test_arquivos_de_suporte_existem(self) -> None:
        erros: list[str] = []
        validate.validate_support_files(erros)
        self.assertEqual([], erros, "\n".join(erros))

    def test_readme_tem_tudo_que_promete(self) -> None:
        erros: list[str] = []
        validate.validate_readme(erros)
        self.assertEqual([], erros, "\n".join(erros))


class AdrsReferenciadosTest(unittest.TestCase):
    def test_toda_referencia_a_adr_aponta_para_arquivo_existente(self) -> None:
        arquivos = {nome.split("-")[0] + "-" + nome.split("-")[1]
                    for nome in os.listdir(os.path.join(ROOT, "docs", "decisoes"))
                    if nome.endswith(".md")}
        self.assertGreaterEqual(len(arquivos), 4)
        origem = ["README.md", "runbooks/index.md"] + [
            os.path.join("runbooks", n) for n in os.listdir(os.path.join(ROOT, "runbooks"))
            if n.endswith(".md")
        ]
        referencias = set()
        for rel in origem:
            texto = validate.read_text(rel)
            referencias.update(re.findall(r"ADR-(\d{4})", texto))
        self.assertTrue(referencias, "nenhuma referencia a ADR encontrada")
        for numero in sorted(referencias):
            self.assertIn(f"ADR-{numero}", arquivos, f"ADR-{numero} referenciado e inexistente")

    def test_adrs_tem_secoes_e_data_de_aceite(self) -> None:
        erros: list[str] = []
        validate.validate_adrs(erros)
        self.assertEqual([], erros, "\n".join(erros))


class DadosSinteticosTest(unittest.TestCase):
    def test_rotulo_de_dado_sintetico_onde_o_dado_aparece(self) -> None:
        for rel in ("README.md", "docker-compose.yml", "app/server.py",
                    "app/loadgen.py", "runbooks/index.md", "docs/custos.md",
                    "docs/decisoes/ADR-0006-dados-sinteticos-com-semente-declarada.md"):
            texto = validate.normalize(validate.read_text(rel))
            self.assertIn("sintetic", texto, rel)

    def test_semente_declarada(self) -> None:
        compose = validate.normalize(validate.read_text("docker-compose.yml"))
        self.assertIn("seed", compose)
        servidor = validate.read_text("app/server.py")
        self.assertRegex(servidor, r'seed: int = 2026')


class MiniYamlConfigTest(unittest.TestCase):
    # Os arquivos de config que o validador le. Os arquivos de CI (.github/ e
    # .gitlab-ci.yml) ficam fora de proposito: miniyaml nao suporta block scalar
    # (`|`), e o contrato de CI e verificado por presenca de passo, nao por parse.
    DIRS_DE_CONFIG = ("prometheus", "loki", "tempo", "otel", "grafana", "alertmanager")

    def test_todo_yaml_de_config_abre(self) -> None:
        encontrados = 0
        for base in self.DIRS_DE_CONFIG:
            dirpath_base = os.path.join(ROOT, base)
            if not os.path.isdir(dirpath_base):
                continue
            for dirpath, dirnames, filenames in os.walk(dirpath_base):
                dirnames[:] = [d for d in dirnames if d not in validate.SKIP_DIRS]
                for nome in filenames:
                    if not nome.endswith((".yml", ".yaml")):
                        continue
                    caminho = os.path.join(dirpath, nome)
                    doc = miniyaml.parse_file(caminho)
                    self.assertIsInstance(doc, dict, caminho)
                    encontrados += 1
        self.assertGreaterEqual(encontrados, 10, "varredura nao achou os configs")


class CustoCardinalidadeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.modulo = carregar_modulo_custos()
        cls.relatorio = cls.modulo.calcular()

    def test_produto_de_cardinalidade(self) -> None:
        self.assertEqual(4 * 4 * 6 * 3,
                         self.modulo.series_de(("service", "route", "status", "status_class")))

    def test_label_correlato_nao_multiplica(self) -> None:
        um = self.modulo.series_de(("service",))
        cinco = self.modulo.series_de(("service", "journey", "team", "version", "owner"))
        self.assertEqual(um, cinco)
        self.assertEqual(4, um)

    def test_conta_escrita_bate_com_numero(self) -> None:
        for item in self.relatorio["top_familias"]:
            produto = 1
            for parcela in item["conta"].split(" * "):
                produto *= int(parcela.split("=")[1])
            self.assertEqual(produto, item["series"], item["familia"])

    def test_somas_do_relatorio(self) -> None:
        p = self.relatorio["prometheus"]
        prem = self.relatorio["premissas"]
        self.assertEqual(
            p["series_app"] + p["series_recording_rules"] + p["series_metadata_scrape"]
            + p["series_estado_de_alerta"],
            p["series_totais"],
        )
        self.assertEqual(p["series_totais"] * prem["amostras_por_serie_por_dia"],
                         p["amostras_por_dia"])
        self.assertAlmostEqual(
            p["amostras_por_dia"] * prem["bytes_por_amostra"] / 1024**2,
            p["disco_por_dia_mb"], delta=0.1,
        )
        self.assertAlmostEqual(
            p["series_totais"] * prem["bytes_por_serie_no_head"] / 1024**2,
            p["ram_head_mb"], delta=0.1,
        )

    def test_trafego_vem_do_compose(self) -> None:
        self.assertEqual(18.0, float(self.relatorio["trafego"]["requests_por_s"]))
        self.assertEqual(3, len(self.relatorio["trafego"]["geradores"]))

    def test_cenarios_proibidos_sao_piores_que_o_esquema(self) -> None:
        cenarios = self.relatorio["cenarios_proibidos"]
        self.assertEqual(3, len(cenarios))
        trace = cenarios["trace_id como label de metrica"]
        self.assertGreater(trace["series_adicionais_no_fleet"],
                           10 * self.relatorio["prometheus"]["series_totais"])
        self.assertLess(cenarios["route com path completo (query string)"]
                        ["series_por_familia_antes"],
                        cenarios["route com path completo (query string)"]
                        ["series_por_familia_depois"])

    def test_dentro_do_teto_autodeclarado(self) -> None:
        self.assertTrue(self.modulo._dentro(self.relatorio),
                        "a conta passou do teto: rode --check e corrija o schema")

    def test_documento_copia_os_numeros_da_execucao(self) -> None:
        texto = validate.read_text("docs/custos.md")
        p = self.relatorio["prometheus"]
        for trecho in (
            f"TOTAL DE SERIES                = {p['series_totais']}",
            f"RAM: {p['series_totais']} * 3600 bytes = {p['ram_head_mb']} MB no head",
            f"disco: {int(p['amostras_por_dia'])} * 1.5 bytes = "
            f"{p['disco_por_dia_mb']} MB/dia = {p['disco_na_retencao_gb']} GB em 14 dias",
            f"linhas/dia: 18 * 86400 = {self.relatorio['loki']['linhas_por_dia']}",
            f"spans/dia: 18 * 3 * 86400 = {self.relatorio['tempo']['spans_por_dia']}",
        ):
            self.assertIn(trecho, texto, trecho)

    def test_aritmetica_explicita_existe_no_documento(self) -> None:
        """O validador exige a conta; o teste garante que ela continua legivel."""
        texto = validate.read_text("docs/custos.md")
        self.assertTrue(re.search(r"\d+\s*[*x]\s*\d+", texto))
        self.assertIn("288", texto)
        self.assertIn("129600", texto)


if __name__ == "__main__":
    unittest.main()
