#!/usr/bin/env python3
"""Calculadora de custo de cardinalidade e armazenamento do stack local.

Uso (na raiz do repositorio)::

    python3 scripts/custo-cardinalidade.py           # relatorio em texto
    python3 scripts/custo-cardinalidade.py --json    # mesma conta, maquina-legivel
    python3 scripts/custo-cardinalidade.py --check   # falha se a conta estourar o teto

Todo numero aqui e **sintetico**: o ambiente de exemplo nao tem trafego real. O
que a conta faz e transformar as escolhas de schema (``app/tagging.py``), os
labels que ``app/server.py`` realmente escreve, o ``scrape_interval`` do
Prometheus e as taxas declaradas no ``docker-compose.yml`` em memoria e disco
previsiveis. A narrativa com a interpretacao esta em ``docs/custos.md``.

A conta obedece a uma regra que a literatura de Prometheus costuma pular:
**label correlato nao multiplica**. ``service``, ``journey``, ``team``,
``version`` e ``owner`` andam juntos - um servico tem uma jornada, um time, uma
versao. Multiplicar as cardinalidades desses cinco labels daria 1024
combinacoes que nunca existem. Entao o grupo de identidade entra na conta uma
unica vez, com o numero de servicos. Quem multiplica de verdade sao os labels
que variam dentro de uma instancia: ``route``, ``status``, ``status_class``,
``dependency`` e ``le``.

Stdlib apenas: ``miniyaml`` para ler os arquivos do repositorio, ``json`` para a
saida maquina-legivel, nada de rede.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import miniyaml  # noqa: E402

# --------------------------------------------------------------------------
# constantes de custo
# --------------------------------------------------------------------------
# Ordem de grandeza do Prometheus com compactacao ligada e head de 2h. O objetivo
# e comparar cenarios entre si, nao cobrar fatura de nuvem nenhuma.

BYTES_POR_AMOSTRA = 1.5          # amostra ja comprimida no bloco do TSDB
BYTES_POR_SERIE_HEAD = 3600      # serie ativa na memoria do head
SEGUNDOS_DIA = 86400
JANELA_HEAD_S = 7200             # 2h: periodo em que a serie fica na memoria
DIAS_RETENCAO_PROMETHEUS = 14
DIAS_RETENCAO_LOKI = 14
BYTES_POR_LINHA_LOG = 380        # linha JSON media do formato de app/server.py
BYTES_POR_LINHA_INDICE_LOKI = 0.6
SPANS_POR_REQUEST = 3            # servidor + dependencia + fila do exportador
BYTES_POR_SPAN = 520
DIAS_RETENCAO_TEMPO = 2          # compaction.block_retention: 48h

# Cardinalidade sintetica por label. Cada linha e uma decisao de schema, nao um
# dado medido: e o numero que se discute na revisao do ADR-0004.
CARDINALIDADE = {
    "route": 4,             # /pay, /auth, /health, unmatched
    "status": 6,            # 200, 201, 400, 404, 502, 503
    "status_class": 3,      # 2xx, 4xx, 5xx
    "dependency": 2,        # gateway-pagamentos, provedor-identity
    "le": 11,               # HISTOGRAM_BUCKETS_SECONDS (10) + bucket +Inf
    "job": 8,               # scrape_configs do prometheus.yml
    "instance": 4,          # alvo por job
    "severity": 3,          # info, warning, critical
    "signal": 6,            # os seis valores aceitos pelo validador
    "environment": 1,       # local-sintetico: um so valor neste ambiente
    "user_id": 5000,        # hipotese dos cenarios proibidos
}

# Labels de identidade: andam juntos, entao entram na conta uma unica vez com o
# numero de servicos do ambiente. ``version`` e o caso traiçoeiro: durante um
# rollout ha duas versoes vivas, e o FATOR_IDENTIDADE dobra junto - ver o
# paragrafo sobre rollout em docs/custos.md.
LABELS_DE_IDENTIDADE = ("service", "journey", "team", "version", "owner", "cost_center")
FATOR_IDENTIDADE = 4           # sample-app, auth-api, search-api, notification-worker
INSTANCIAS_POR_SERVICO = 1

# Quantos valores cada label de identidade teria se fosse independente: a conta
# ingenua multiplica tudo isso, a conta correta usa FATOR_IDENTIDADE uma vez.
IDENTIDADE_POR_LABEL = {
    "service": 4,
    "journey": 4,
    "team": 4,
    "version": 4,
    "owner": 4,
    "cost_center": 2,
}

# Familias expostas por app/server.py com os labels que o renderer escreve de
# fato (ver Metrics.observe_request, base_labels e _render_histogram). Se a app
# ganhar label, esta tabela ganha a label na mesma linha - de proposito.
FAMILIAS_APP: dict[str, tuple[str, ...]] = {
    "http_requests_total": LABELS_DE_IDENTIDADE[:1] + ("journey", "environment", "team",
                                                       "route", "status", "status_class"),
    "http_request_duration_seconds_bucket": ("service", "journey", "environment", "team",
                                             "route", "le"),
    "http_request_duration_seconds_sum": ("service", "journey", "environment", "team", "route"),
    "http_request_duration_seconds_count": ("service", "journey", "environment", "team", "route"),
    "dependency_requests_total": ("service", "journey", "dependency", "status_class"),
    "dependency_request_duration_seconds_bucket": ("service", "journey", "dependency", "le"),
    "dependency_request_duration_seconds_sum": ("service", "journey", "dependency"),
    "dependency_request_duration_seconds_count": ("service", "journey", "dependency"),
    "http_requests_in_flight": ("service", "journey", "route"),
    "app_slo_availability_ratio": ("service", "journey", "environment", "team", "version", "owner"),
    "app_slo_latency_seconds": ("service", "journey", "environment", "team", "version", "owner"),
    "app_slo_latency_compliance_ratio": ("service", "journey", "environment", "team",
                                         "version", "owner"),
    "app_info": ("service", "journey", "environment", "team", "version", "owner"),
    "process_cpu_seconds_total": ("service", "journey", "environment", "team", "version", "owner"),
    "process_resident_memory_bytes": ("service", "journey", "environment", "team", "version", "owner"),
    "process_open_file_descriptors": ("service", "journey", "environment", "team", "version", "owner"),
    "process_max_file_descriptors": ("service", "journey", "environment", "team", "version", "owner"),
    "otlp_export_failures_total": ("service",),
    "http_requests_unmatched_total": ("service", "route"),
}

# Prefixo convencional do nome da recording rule define as labels de saida
# neste repositorio; sem o `by (...)`, usamos o prefixo como declaracao.
PREFIXO_PARA_LABELS = {
    "service": ("service", "journey"),
    "route": ("service", "journey", "route"),
    "slo": ("service", "journey"),
    "job": ("job", "service"),
    "instance": ("service", "instance"),
    "infra": ("service", "job", "instance"),
    "host": ("instance",),
    "cluster": ("service", "job"),
}

RULE_FILES = (
    "prometheus/rules/golden-signals.yml",
    "prometheus/rules/slo-burn-rate.yml",
    "prometheus/rules/infrastructure.yml",
)
BY_RE = re.compile(r"\bby\s*\(([^)]*)\)")


def abspath(*parts: str) -> str:
    return os.path.join(ROOT, *parts)


def _valores(labels, valores: dict[str, int] | None) -> list[tuple[str, int]]:
    base = dict(CARDINALIDADE)
    if valores:
        base.update(valores)
    return [(label, base.get(label, 1)) for label in labels]


def series_de(labels, valores: dict[str, int] | None = None) -> int:
    """Produto das cardinalidades, com grupo de identidade contando uma vez.

    ``("service", "journey", "team", "route")`` -> ``4 * 4`` e nao ``4 * 4 * 4 * 4``:
    jornada e time sao correlatos ao servico neste ambiente.
    """
    total = 1
    identidade_contada = False
    for label, cardinalidade in _valores(labels, valores):
        if label in LABELS_DE_IDENTIDADE:
            if identidade_contada:
                continue
            identidade_contada = True
            total *= FATOR_IDENTIDADE if label == "service" else 1
            continue
        total *= cardinalidade
    return total


def conta_de(labels, valores: dict[str, int] | None = None) -> str:
    """A expressao aritmetica correspondente a ``series_de``.

    Mostra a conta escrita: ``identidade=4 * route=4 * status=6 * status_class=3``.
    """
    partes = []
    identidade_contada = False
    for label, cardinalidade in _valores(labels, valores):
        if label in LABELS_DE_IDENTIDADE:
            if identidade_contada:
                continue
            identidade_contada = True
            partes.append(f"identidade={FATOR_IDENTIDADE}")
            continue
        partes.append(f"{label}={cardinalidade}")
    return " * ".join(partes) if partes else "1"


def scrape_interval_segundos() -> int:
    doc = miniyaml.parse_file(abspath("prometheus", "prometheus.yml"))
    raw = str((doc.get("global") or {}).get("scrape_interval", "15s"))
    match = re.fullmatch(r"([0-9]+)(s|m|h)", raw.strip())
    if not match:
        return 15
    valor, unidade = int(match.group(1)), match.group(2)
    return valor * {"s": 1, "m": 60, "h": 3600}[unidade]


def jobs_e_alvos() -> tuple[int, int]:
    doc = miniyaml.parse_file(abspath("prometheus", "prometheus.yml"))
    jobs = doc.get("scrape_configs") or []
    alvos = 0
    for job in jobs:
        targets = (job.get("static_configs") or [{}])[0].get("targets") or []
        alvos += max(1, len(targets))
    return len(jobs), alvos


def trafego_sintetico() -> dict[str, object]:
    """Requests/s somados dos geradores declarados no compose."""
    doc = miniyaml.parse_file(abspath("docker-compose.yml"))
    total = 0.0
    detalhes: dict[str, object] = {}
    for nome, svc in (doc.get("services") or {}).items():
        if "load" not in nome:
            continue
        env = svc.get("environment") or {}
        rate = float(env.get("RATE", 0) or 0)
        steps = [c for c in (svc.get("command") or []) if str(c).startswith("http://")]
        if not steps or rate <= 0:
            continue
        detalhes[nome] = {"viagens_por_s": rate, "passos": len(steps),
                          "requests_por_s": rate * len(steps)}
        total += rate * len(steps)
    return {"requests_por_s": total, "geradores": detalhes}


def servicos_app() -> list[str]:
    doc = miniyaml.parse_file(abspath("docker-compose.yml"))
    return [nome for nome, svc in (doc.get("services") or {}).items()
            if str(svc.get("image", "")).startswith("obs-stack/")]


def series_app() -> tuple[int, list[dict[str, object]]]:
    linhas = [{"familia": fam, "conta": conta_de(labels), "series": series_de(labels),
               "labels": list(labels)}
              for fam, labels in FAMILIAS_APP.items()]
    total = sum(int(item["series"]) for item in linhas)
    return total, sorted(linhas, key=lambda item: -int(item["series"]))


def series_recording_rules() -> tuple[int, dict[str, int], int]:
    """Series produzidas pelas recording rules, agrupadas por arquivo."""
    total = 0
    por_arquivo: dict[str, int] = {}
    quantidade = 0
    for rel in RULE_FILES:
        caminho = abspath(rel)
        if not os.path.exists(caminho):
            continue
        doc = miniyaml.parse_file(caminho)
        arquivo_total = 0
        for grupo in doc.get("groups") or []:
            for regra in grupo.get("rules") or []:
                if "record" not in regra:
                    continue
                quantidade += 1
                nome = str(regra["record"])
                match = BY_RE.search(str(regra.get("expr", "")))
                if match:
                    labels = tuple(x.strip() for x in match.group(1).split(",") if x.strip())
                else:
                    labels = PREFIXO_PARA_LABELS.get(nome.split(":", 1)[0], ("service",))
                arquivo_total += series_de(labels)
        por_arquivo[rel] = arquivo_total
        total += arquivo_total
    return total, por_arquivo, quantidade


def series_estado_de_alerta(quantidade_alertas: int) -> int:
    """ALERTS{alertname, alertstate, service, journey, severity, ...}."""
    return quantidade_alertas * FATOR_IDENTIDADE * 3


def cenarios_proibidos(requests_por_s: float) -> dict[str, dict[str, object]]:
    """O preco de adicionar um label proibido a familia de maior custo."""
    familia = "http_requests_total"
    labels_atuais = FAMILIAS_APP[familia]
    serie_atual = series_de(labels_atuais)
    cenarios: dict[str, dict[str, object]] = {}
    for rotulo, novos, unico_por_request in (
        # rota com URL instanciada: 250 paths diferentes no lugar de 4 templates
        ("route com path completo (query string)", {"route": 250}, False),
        # user_id: 5000 usuarios sinteticos, cada um em várias rotas e com varios status
        ("user_id como label de metrica", {"user_id": CARDINALIDADE["user_id"]}, False),
        # trace_id: um valor novo a cada request, guardado no head por 2h
        ("trace_id como label de metrica", {"trace_id": int(requests_por_s * JANELA_HEAD_S)}, True),
    ):
        valores = dict(CARDINALIDADE)
        valores.update(novos)
        substitui = all(novo in labels_atuais for novo in novos)
        labels_novos = tuple(
            labels_atuais if substitui
            else tuple(labels_atuais) + tuple(n for n in novos if n not in labels_atuais)
        )
        serie_nova = series_de(labels_novos, valores)
        # serie_nova e serie_atual ja sao contas do fleet inteiro: a familia
        # http_requests_total inclui `service`, e o fator de identidade cuida de
        # contar os 4 servicos uma unica vez.
        adicionais = serie_nova - serie_atual
        novo_label = list(novos)[0]
        piso = int(novos[novo_label]) if unico_por_request else None
        cenarios[rotulo] = {
            "familia": familia,
            "labels_novos": list(novos.keys()),
            "conta_antes": conta_de(labels_atuais),
            "conta_depois": conta_de(labels_novos, valores),
            "series_por_familia_antes": serie_atual,
            "series_por_familia_depois": serie_nova,
            "series_adicionais_no_fleet": adicionais,
            "head_adicional_gb": round(adicionais * BYTES_POR_SERIE_HEAD / 1024**3, 1),
            "piso_series": piso,
            "piso_head_mb": round(piso * BYTES_POR_SERIE_HEAD / 1024**2, 1) if piso else None,
        }
    return cenarios


def calcular() -> dict[str, object]:
    intervalo = scrape_interval_segundos()
    amostras_por_serie_dia = SEGUNDOS_DIA // intervalo
    trafego = trafego_sintetico()
    requests_por_s = float(trafego["requests_por_s"])
    apps = servicos_app()
    jobs, alvos = jobs_e_alvos()

    series_app_total, top_familias = series_app()
    series_rules, rules_por_arquivo, quantidade_rules = series_recording_rules()
    series_metadata = alvos * 4          # up, scrape_duration, scrape_samples, pods
    quantidade_alertas = 35
    series_alertas = series_estado_de_alerta(quantidade_alertas)
    series_totais = series_app_total + series_rules + series_metadata + series_alertas

    ram_bytes = series_totais * BYTES_POR_SERIE_HEAD
    amostras_dia = series_totais * amostras_por_serie_dia
    disco_dia = amostras_dia * BYTES_POR_AMOSTRA
    disco_retencao = disco_dia * DIAS_RETENCAO_PROMETHEUS

    # Stream labels do Loki no esquema do ADR-0004: service, environment, journey,
    # team. Correlatos outra vez: 4 streams reais, nao 4 * 1 * 4 * 4 = 64.
    streams = FATOR_IDENTIDADE * CARDINALIDADE["environment"]
    for label in ("journey", "team"):
        streams *= IDENTIDADE_POR_LABEL[label]
    streams_reais = FATOR_IDENTIDADE * CARDINALIDADE["environment"]
    linhas_dia = requests_por_s * SEGUNDOS_DIA
    dados_dia = linhas_dia * BYTES_POR_LINHA_LOG
    indice_dia = linhas_dia * BYTES_POR_LINHA_INDICE_LOKI

    spans_dia = requests_por_s * SPANS_POR_REQUEST * SEGUNDOS_DIA
    bytes_dia_tempo = spans_dia * BYTES_POR_SPAN

    return {
        "rotulo": "todos os numeros sao sinteticos",
        "premissas": {
            "scrape_interval_s": intervalo,
            "amostras_por_serie_por_dia": amostras_por_serie_dia,
            "bytes_por_amostra": BYTES_POR_AMOSTRA,
            "bytes_por_serie_no_head": BYTES_POR_SERIE_HEAD,
            "janela_head_s": JANELA_HEAD_S,
            "fator_identidade": FATOR_IDENTIDADE,
            "servicos_app": apps,
            "instancias_por_servico": INSTANCIAS_POR_SERVICO,
            "jobs_de_scrape": jobs,
            "alvos_de_scrape": alvos,
            "recording_rules": quantidade_rules,
            "alerts": quantidade_alertas,
            "labels_de_identidade": list(LABELS_DE_IDENTIDADE),
        },
        "trafego": trafego,
        "prometheus": {
            "series_app": series_app_total,
            "series_recording_rules": series_rules,
            "series_recording_rules_por_arquivo": rules_por_arquivo,
            "series_metadata_scrape": series_metadata,
            "series_estado_de_alerta": series_alertas,
            "series_totais": series_totais,
            "amostras_por_dia": amostras_dia,
            "ram_head_mb": round(ram_bytes / 1024**2, 1),
            "disco_por_dia_mb": round(disco_dia / 1024**2, 1),
            "disco_na_retencao_gb": round(disco_retencao / 1024**3, 2),
        },
        "loki": {
            "streams_teoria": streams,
            "streams_reais": streams_reais,
            "stream_labels": ("service", "environment", "journey", "team"),
            "linhas_por_dia": int(linhas_dia),
            "dados_por_dia_mb": round(dados_dia / 1024**2, 1),
            "indice_por_dia_mb": round(indice_dia / 1024**2, 2),
            "disco_na_retencao_mb": round((dados_dia + indice_dia) * DIAS_RETENCAO_LOKI / 1024**2, 1),
            "health_nao_logado_por_dia": len(apps) * (SEGUNDOS_DIA // 20),
        },
        "tempo": {
            "spans_por_request": SPANS_POR_REQUEST,
            "spans_por_dia": int(spans_dia),
            "bytes_por_span": BYTES_POR_SPAN,
            "dados_por_dia_mb": round(bytes_dia_tempo / 1024**2, 1),
            "disco_na_retencao_gb": round(bytes_dia_tempo * DIAS_RETENCAO_TEMPO / 1024**3, 2),
        },
        "top_familias": top_familias[:6],
        "cenarios_proibidos": cenarios_proibidos(requests_por_s),
        "tetos": {"series_totais": 8000, "ram_head_mb": 32.0, "disco_na_retencao_gb": 1.5},
    }


def _dentro(relatorio: dict[str, object]) -> bool:
    p = relatorio["prometheus"]
    t = relatorio["tetos"]
    return (
        int(p["series_totais"]) <= int(t["series_totais"])
        and float(p["ram_head_mb"]) <= float(t["ram_head_mb"])
        and float(p["disco_na_retencao_gb"]) <= float(t["disco_na_retencao_gb"])
    )


def formatar(relatorio: dict[str, object]) -> str:
    p = relatorio["prometheus"]
    l = relatorio["loki"]
    tp = relatorio["tempo"]
    t = relatorio["trafego"]
    prem = relatorio["premissas"]
    linhas = [
        "custo de cardinalidade - ambiente local, dados sinteticos",
        "=" * 64,
        f"trafego sintetico: {t['requests_por_s']:.0f} requests/s  "
        f"(somando os {len(t['geradores'])} geradores do docker-compose)",
        f"identidade: {prem['fator_identidade']} servicos x {prem['instancias_por_servico']} "
        f"instancia(s); labels correlatos contam uma vez",
        f"scrape_interval: {prem['scrape_interval_s']}s -> "
        f"{prem['amostras_por_serie_por_dia']} amostras por serie por dia",
        "",
        "[Prometheus] maiores familias de serie, no fleet inteiro:",
    ]
    for item in relatorio["top_familias"]:
        linhas.append(
            f"  {item['familia']:<42} {str(item['conta']):<58} = {item['series']}"
        )
    linhas += [
        "",
        f"  app                            = {p['series_app']}",
        f"  recording rules ({prem['recording_rules']} regras) = {p['series_recording_rules']}",
        f"  metadata de scrape ({prem['alvos_de_scrape']} alvos) = {p['series_metadata_scrape']}",
        f"  estado de alerta ({prem['alerts']} alertas) = {p['series_estado_de_alerta']}",
        f"  TOTAL DE SERIES                = {p['series_totais']}",
        f"  RAM: {p['series_totais']} * {prem['bytes_por_serie_no_head']} bytes = "
        f"{p['ram_head_mb']} MB no head",
        f"  amostras/dia: {p['series_totais']} * {prem['amostras_por_serie_por_dia']} = "
        f"{int(p['amostras_por_dia'])}",
        f"  disco: {int(p['amostras_por_dia'])} * {prem['bytes_por_amostra']} bytes = "
        f"{p['disco_por_dia_mb']} MB/dia = {p['disco_na_retencao_gb']} GB em "
        f"{DIAS_RETENCAO_PROMETHEUS} dias",
        "",
        "[Loki]",
        f"  streams: {l['streams_reais']} reais - 4 servicos, identidade conta uma vez; "
        f"{l['streams_teoria']} se a conta ingenua multiplicasse "
        f"{' * '.join(str(x) for x in (4, 1, 4, 4))}",
        f"  linhas/dia: {t['requests_por_s']:.0f} * 86400 = {l['linhas_por_dia']}",
        f"  dados: {l['linhas_por_dia']} * {BYTES_POR_LINHA_LOG} bytes = "
        f"{l['dados_por_dia_mb']} MB/dia",
        f"  indice: {l['linhas_por_dia']} * {BYTES_POR_LINHA_INDICE_LOKI} bytes = "
        f"{l['indice_por_dia_mb']} MB/dia",
        f"  retencao {DIAS_RETENCAO_LOKI} dias = {l['disco_na_retencao_mb']} MB",
        f"  linhas de /health por dia que o log nao deveria guardar: "
        f"{l['health_nao_logado_por_dia']}",
        "",
        "[Tempo]",
        f"  spans/dia: {t['requests_por_s']:.0f} * {tp['spans_por_request']} * 86400 = "
        f"{tp['spans_por_dia']}",
        f"  dados: {tp['spans_por_dia']} * {tp['bytes_por_span']} bytes = "
        f"{tp['dados_por_dia_mb']} MB/dia",
        f"  retencao {DIAS_RETENCAO_TEMPO} dias (48h) = {tp['disco_na_retencao_gb']} GB",
        "",
        "[cenarios com label proibido, familia http_requests_total]",
    ]
    for rotulo, info in relatorio["cenarios_proibidos"].items():
        texto = (
            f"  {rotulo}: {info['conta_depois']} = "
            f"{info['series_por_familia_depois']} series "
            f"(antes {info['series_por_familia_antes']}), "
            f"+{info['series_adicionais_no_fleet']} no fleet, "
            f"+{info['head_adicional_gb']} GB de head"
        )
        if info.get("piso_series"):
            texto += f"; piso real {info['piso_series']} (um valor por request)"
        linhas.append(texto)
    tetos = relatorio["tetos"]
    linhas += [
        "",
        f"[teto auto-imposto] series <= {tetos['series_totais']}, "
        f"head <= {tetos['ram_head_mb']} MB, disco <= {tetos['disco_na_retencao_gb']} GB",
        f"resultado: {'dentro do teto' if _dentro(relatorio) else 'FORA do teto'}",
    ]
    return "\n".join(linhas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", action="store_true", help="saida em JSON")
    parser.add_argument("--check", action="store_true", help="sai com 1 se passar do teto")
    args = parser.parse_args(argv)
    relatorio = calcular()
    print(json.dumps(relatorio, indent=2, ensure_ascii=False, sort_keys=True)
          if args.json else formatar(relatorio))
    if args.check and not _dentro(relatorio):
        print("ERRO: a conta passou do teto auto-imposto", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
