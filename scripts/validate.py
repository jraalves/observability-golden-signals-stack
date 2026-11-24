#!/usr/bin/env python3
"""Validador estrutural do repositorio observability-golden-signals-stack.

Uso (na raiz do repositorio):

    python3 scripts/validate.py

Sai com codigo 0 quando tudo passa e 1 quando ha erros. Stdlib only: o
subconjunto YAML e lido por ``scripts/miniyaml.py`` e os dashboards Grafana
sao lidos com ``json``. As verificacoes implementam o contrato que o projeto
assume como definition of done:

* rule files do Prometheus tem ``groups[].rules[]`` com ``alert``/``record``,
  ``expr``, ``for`` e ``labels.severity`` valido, e so referenciam recording
  rules que realmente existem em algum rule file;
* todo alerta aponta para um runbook que existe no repositorio e aparece na
  matriz ``runbooks/index.md``;
* o roteamento do Alertmanager tem ``route`` com ``group_by``, so referencia
  receivers declarados e define ``inhibit_rules``;
* todo dashboard Grafana tem ``panels[]`` com ``gridPos``, ``targets[]`` com
  ``datasource`` conhecido, ``type`` valido e ``title``, sem sobreposicao de
  paineis;
* docker-compose e as configs de Loki, Tempo e OTel Collector tem as chaves
  obrigatorias e os caminhos montados existem em disco;
* nenhum termo protegido (nome de cliente/empregador real ou credencial)
  aparece em arquivos de texto do repositorio.

As funcoes sao importaveis para que ``tests/`` reutilize a mesma logica em
unittest, sem depender de rede nem de servicos externos.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import miniyaml  # noqa: E402  (sys.path ajustado logo acima)
from miniyaml import YamlError  # noqa: E402

# --------------------------------------------------------------------------
# contrato
# --------------------------------------------------------------------------

ALLOWED_SEVERITIES = {"info", "warning", "critical"}
ALLOWED_SIGNALS = {"latency", "traffic", "errors", "saturation", "slo", "infrastructure"}
PANEL_TYPES = {
    "timeseries",
    "stat",
    "table",
    "logs",
    "gauge",
    "bargauge",
    "heatmap",
    "histogram",
    "piechart",
    "news",
    "row",
    "text",
    # Panel de lista de tracos do datasource Tempo: o alvo usa TraceQL em
    # 'query', nao PromQL em 'expr', e isso precisa ser aceito abaixo.
    "traces",
    "flamegraph",
    "nodeGraph",
    "status-history",
}
GRID_COLS = 24
RULE_FILES = (
    "prometheus/rules/golden-signals.yml",
    "prometheus/rules/slo-burn-rate.yml",
    "prometheus/rules/infrastructure.yml",
)
DASHBOARD_FILES = (
    "grafana/dashboards/golden-signals.json",
    "grafana/dashboards/slo-error-budget.json",
    "grafana/dashboards/correlacao-incidente.json",
)
RUNBOOK_REQUIRED_SECTIONS = (
    "## Gatilho",
    "## Hipoteses em ordem de custo de verificacao",
    "## Diagnostico",
    "## Acao",
    "## Criterio de saida",
    "## O que aprender depois",
)
ADR_REQUIRED_SECTIONS = ("## Contexto", "## Decisao", "## Consequencias", "## Como medir que acertou")
DIAGNOSTIC_TOOLS = ("logcli", "promtool", "tempo-cli", "curl", "kubectl")

# Politica de veracidade: o projeto nao cita cliente, empregador real nem
# credencial. Os termos ficam codificados em hex de proposito: uma lista
# literal de nomes colocaria os proprios nomes no repositorio (e no grep de
# qualquer leitor), o que contradiz a regra. A decodificacao e trivial e o
# conteudo desta tupla e auditavel com `python3 -c "..."`.
_PROTECTED_HEX = (
    "6369656c6f",
    "636c61726f",
    "696e6d657472696373",
    "6b616c656e646165",
    "73616e74616e646572",
    "70726f647562616e",
    "74656c65666f6e696361",
    "746f6b696f",
    "696e647261",
    "6869747373",
    "636f6e6e6563746973",
    "63617067656d696e69",
    "627261646573636f",
    "616c6661",
    "766563746f72",
    "6167736e61736f6674",
    "736c74776562",
    "6768705f",
    "6769746875625f7061745f",
    "676c7061742d",
    "786f78622d",
    "414b4941",
    "424547494e2050524956415445204b4559",
    "4265617265722065794a",
    "796132392e",
    "646f636b65722072756e202d6520504f5354475245535f50415353574f5244",
)
PROTECTED_TERMS = tuple(bytes.fromhex(item).decode("utf-8") for item in _PROTECTED_HEX)
TEXT_SUFFIXES = (".md", ".yml", ".yaml", ".json", ".py", ".sh", ".txt", ".cfg", ".ini", ".conf")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def abspath(*parts: str) -> str:
    return os.path.join(ROOT, *parts)


def read_text(rel: str, encoding: str = "utf-8") -> str:
    with open(abspath(rel), encoding=encoding) as fh:
        return fh.read()


def normalize(text: str) -> str:
    """Minusculo sem acentos, para busca de termos protegidos."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def load_yaml(rel: str, errors: list[str]):
    """Carrega um YAML do repositorio; registra erro e devolve None se falhar."""
    try:
        data = miniyaml.parse_file(abspath(rel))
    except YamlError as exc:
        errors.append(f"{rel}: YAML invalido: {exc}")
        return None
    except OSError as exc:
        errors.append(f"{rel}: nao consegui ler: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{rel}: a raiz do documento deveria ser um mapeamento")
        return None
    return data


def get_path(doc, dotted: str, default=None):
    """Le ``a.b.0.c`` de um dict/list aninhado, sem try/except espalhado.

    Segmentos numericos sao indices de lista, o que e o jeito como o YAML
    representa ``static_configs[0].targets`` sem inventar sintaxe nova.
    """
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return default
            cur = cur[int(part)]
            continue
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def require_keys(doc, dotted_keys: tuple[str, ...], where: str, errors: list[str]):
    for key in dotted_keys:
        if get_path(doc, key, "__faltando__") == "__faltando__":
            errors.append(f"{where}: chave obrigatoria ausente: {key}")


def is_duration(value) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[0-9]+(ms|s|m|h|d|w|y)", value.strip())
    )


def strip_quoted(expr: str) -> str:
    """Remove trechos entre aspas de uma expressao, para procurar identificadores."""
    out = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch in "\"'":
            j = i + 1
            while j < n and expr[j] != ch:
                j += 1 if expr[j] != "\\" else 2
            out.append(" " * (min(j, n) - i + 1))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def colon_identifiers(expr: str) -> set[str]:
    """Identificadores com ``:`` em uma PromQL (candidatos a recording rule)."""
    return set(
        re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*(?::[a-zA-Z0-9_]+)+", strip_quoted(expr))
    )


def check_balanced(expr: str) -> str | None:
    """Retorna descricao do desbalanceamento de parenteses/colchetes/aspas."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = {v: k for k, v in pairs.items()}
    stack: list[str] = []
    quote = ""
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch in pairs:
            stack.append(ch)
        elif ch in closers:
            if not stack or stack[-1] != closers[ch]:
                return f"fechamento inesperado de '{ch}'"
            stack.pop()
        i += 1
    if quote:
        return "aspas nao fechadas"
    if stack:
        return f"abertura sem fechamento de '{stack[-1]}'"
    return None


def iter_rules(doc):
    """Percorre ``(grupo, rule)`` de um rule file ja parseado."""
    groups = doc.get("groups") or []
    for gidx, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        gname = group.get("name", f"<grupo {gidx}>")
        for rule in group.get("rules") or []:
            if isinstance(rule, dict):
                yield gname, rule


def collect_recording_rules(rel_files: tuple[str, ...] = RULE_FILES) -> set[str]:
    names: set[str] = set()
    for rel in rel_files:
        if not os.path.exists(abspath(rel)):
            continue
        try:
            doc = miniyaml.parse_file(abspath(rel))
        except YamlError:
            continue
        for _, rule in iter_rules(doc or {}):
            if "record" in rule:
                names.add(str(rule["record"]))
    return names


def repo_text_files() -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.endswith(TEXT_SUFFIXES):
                found.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    return sorted(found)


# --------------------------------------------------------------------------
# Prometheus: rule files
# --------------------------------------------------------------------------


def validate_rule_doc(doc: dict, rel: str, recorded: set[str] | None = None) -> list[str]:
    """Valida a estrutura de um rule file do Prometheus."""
    errors: list[str] = []
    recorded = recorded if recorded is not None else collect_recording_rules()
    groups = doc.get("groups")
    if not isinstance(groups, list) or not groups:
        errors.append(f"{rel}: esperado 'groups' como lista nao vazia")
        return errors

    for gidx, group in enumerate(groups):
        where = f"{rel}: groups[{gidx}]"
        if not isinstance(group, dict):
            errors.append(f"{where}: deveria ser um mapeamento")
            continue
        gname = group.get("name")
        if not isinstance(gname, str) or not gname.strip():
            errors.append(f"{where}: 'name' obrigatorio")
        gname = gname or f"<grupo {gidx}>"
        interval = group.get("interval")
        if interval is not None and not is_duration(str(interval)):
            errors.append(f"{where}.interval: duracao invalida {interval!r}")
        rules = group.get("rules")
        if not isinstance(rules, list) or not rules:
            errors.append(f"{where} ({gname}): 'rules' deve ser uma lista nao vazia")
            continue
        for ridx, rule in enumerate(rules):
            spot = f"{where}.rules[{ridx}]"
            if not isinstance(rule, dict):
                errors.append(f"{spot}: deveria ser um mapeamento")
                continue
            kind = "alert" if "alert" in rule else "record" if "record" in rule else None
            if kind is None:
                errors.append(f"{spot}: regra precisa de 'alert' ou 'record'")
                continue
            if "alert" in rule and "record" in rule:
                errors.append(f"{spot}: regra nao pode ter 'alert' e 'record' juntos")
                continue
            name = rule[kind]
            if not isinstance(name, str) or not METRIC_NAME_RE.fullmatch(name or ""):
                errors.append(f"{spot}: nome de regra invalido: {name!r}")
            expr = rule.get("expr")
            if not isinstance(expr, str) or not expr.strip():
                errors.append(f"{spot} ({name}): 'expr' obrigatorio e deve ser texto")
            else:
                problem = check_balanced(expr)
                if problem:
                    errors.append(f"{spot} ({name}): expr com sintaxe duvidosa: {problem}")
                for window in re.findall(r"\[([^\]]*)\]", strip_quoted(expr)):
                    if not is_duration(window.strip()):
                        errors.append(f"{spot} ({name}): janela de tempo invalida [{window}]")
                used = colon_identifiers(expr) if kind == "alert" or "rate(" in expr else set()
                unknown = sorted(tok for tok in used if tok not in recorded)
                if unknown:
                    errors.append(
                        f"{spot} ({name}): referenciando metricas de recording rule "
                        f"que nao existem: {', '.join(unknown)}"
                    )
            if kind == "record":
                if "for" in rule:
                    errors.append(f"{spot} ({name}): recording rules nao usam 'for'")
                if get_path(rule, "labels.severity") is not None:
                    errors.append(
                        f"{spot} ({name}): recording rules nao devem carregar labels.severity"
                    )
                continue
            # --- alert ---
            sev = get_path(rule, "labels.severity")
            if sev not in ALLOWED_SEVERITIES:
                errors.append(
                    f"{spot} ({name}): labels.severity deve ser um de "
                    f"{sorted(ALLOWED_SEVERITIES)}, veio {sev!r}"
                )
            if "for" not in rule:
                errors.append(f"{spot} ({name}): alertas precisam de 'for' (sem flash de pagina)")
            elif not is_duration(str(rule["for"])):
                errors.append(f"{spot} ({name}): 'for' invalido: {rule['for']!r}")
            if get_path(rule, "labels.team") in (None, ""):
                errors.append(f"{spot} ({name}): labels.team obrigatorio (roteamento por time)")
            signal = get_path(rule, "labels.signal")
            if signal not in ALLOWED_SIGNALS:
                errors.append(
                    f"{spot} ({name}): labels.signal deve ser um de "
                    f"{sorted(ALLOWED_SIGNALS)}, veio {signal!r}"
                )
            summary = get_path(rule, "annotations.summary")
            if not isinstance(summary, str) or not summary.strip():
                errors.append(f"{spot} ({name}): annotations.summary obrigatorio")
            runbook = get_path(rule, "annotations.runbook_url")
            if not isinstance(runbook, str) or not runbook.strip():
                errors.append(f"{spot} ({name}): annotations.runbook_url obrigatorio")
            else:
                rel_target = runbook_target_to_path(runbook)
                if rel_target is None:
                    errors.append(f"{spot} ({name}): runbook_url fora do repositorio: {runbook!r}")
                elif not os.path.exists(abspath(rel_target)):
                    errors.append(f"{spot} ({name}): runbook_url aponta para arquivo ausente: {rel_target}")
    return errors


def runbook_target_to_path(url: str) -> str | None:
    """Converte .../blob/main/runbooks/x.md ou runbooks/x.md em caminho local."""
    marker = "/blob/main/"
    if marker in url:
        return url.split(marker, 1)[1]
    if url.startswith("runbooks/") or url.startswith("docs/"):
        return url
    return None


METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


def validate_rule_files(errors: list[str]) -> list[str]:
    alert_names: list[str] = []
    recorded = collect_recording_rules()
    for rel in RULE_FILES:
        if not os.path.exists(abspath(rel)):
            errors.append(f"{rel}: arquivo de regras ausente")
            continue
        doc = load_yaml(rel, errors)
        if doc is None:
            continue
        errors.extend(validate_rule_doc(doc, rel, recorded))
        for _, rule in iter_rules(doc):
            if "alert" in rule:
                alert_names.append(str(rule["alert"]))
    dupes = sorted({n for n in alert_names if alert_names.count(n) > 1})
    if dupes:
        errors.append(f"nomes de alerta duplicados entre rule files: {', '.join(dupes)}")
    return alert_names


# --------------------------------------------------------------------------
# Prometheus: scrape config
# --------------------------------------------------------------------------


def validate_prometheus_yml(errors: list[str], services: set[str]):
    rel = "prometheus/prometheus.yml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return
    require_keys(doc, ("global.scrape_interval", "scrape_configs"), rel, errors)
    rule_files = doc.get("rule_files") or []
    if not rule_files:
        errors.append(f"{rel}: rule_files vazio")
    for entry in rule_files:
        base = "/etc/prometheus/rules/"
        if isinstance(entry, str) and entry.startswith(base):
            local = "prometheus/rules/" + os.path.basename(entry)
            if not os.path.exists(abspath(local)):
                errors.append(f"{rel}: rule_files aponta para {entry}, mas {local} nao existe")
        else:
            errors.append(f"{rel}: rule_files deveria usar caminho do container ({base}*)")
    am_targets = get_path(doc, "alerting.alertmanagers.0.static_configs.0.targets") or []
    if not am_targets:
        errors.append(f"{rel}: sem alertas sem alertmanager alvo em alerting.alertmanagers")
    seen_jobs = set()
    for jidx, job in enumerate(doc.get("scrape_configs") or []):
        spot = f"{rel}: scrape_configs[{jidx}]"
        if not isinstance(job, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        job_name = job.get("job_name")
        if not job_name:
            errors.append(f"{spot}: 'job_name' obrigatorio")
            continue
        if job_name in seen_jobs:
            errors.append(f"{spot}: job_name duplicado {job_name!r}")
        seen_jobs.add(job_name)
        targets = get_path(job, "static_configs.0.targets") or []
        if not targets and "file_sd_configs" not in job:
            errors.append(f"{spot} ({job_name}): nenhum alvo (static_configs ou file_sd_configs)")
        for target in targets:
            host = str(target).split(":")[0]
            if services and host not in services and host not in {"localhost", "127.0.0.1"}:
                errors.append(
                    f"{spot} ({job_name}): alvo {target!r} aponta para {host!r}, "
                    "que nao e um servico do docker-compose"
                )
        metrics_path = job.get("metrics_path", "/metrics")
        if not str(metrics_path).startswith("/"):
            errors.append(f"{spot} ({job_name}): metrics_path invalido")


# --------------------------------------------------------------------------
# Alertmanager
# --------------------------------------------------------------------------


def _walk_routes(route, out: list[tuple[str, dict]]):
    if not isinstance(route, dict):
        return
    out.append((str(route.get("receiver", "")), route))
    for child in route.get("routes") or []:
        _walk_routes(child, out)


def validate_alertmanager(errors: list[str]):
    rel = "prometheus/alertmanager.yml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return
    require_keys(doc, ("route", "receivers"), rel, errors)
    if get_path(doc, "global.resolve_timeout") is None:
        errors.append(f"{rel}: global.resolve_timeout deveria estar explicito")
    receivers = doc.get("receivers") or []
    names = []
    for ridx, recv in enumerate(receivers):
        spot = f"{rel}: receivers[{ridx}]"
        if not isinstance(recv, dict) or not recv.get("name"):
            errors.append(f"{spot}: receiver precisa de 'name'")
            continue
        names.append(str(recv["name"]))
        if not any(k for k in recv if k != "name"):
            errors.append(f"{spot} ({recv['name']}): receiver sem integracao de envio")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"{rel}: receivers duplicados: {', '.join(dupes)}")
    if not names:
        errors.append(f"{rel}: nenhum receiver definido")
    known = set(names)

    route = doc.get("route") or {}
    routes: list[tuple[str, dict]] = []
    _walk_routes(route, routes)
    if not routes:
        errors.append(f"{rel}: 'route' precisa de pelo menos a rota raiz")
    for spot_key, item in routes:
        where = f"{rel}: rota receiver={item.get('receiver')!r}"
        if item is route:
            where = f"{rel}: route"
        group_by = item.get("group_by")
        if "group_by" in item and (not isinstance(group_by, list) or not group_by):
            # Sub-rota pode omitir group_by (herda da raiz); o que nao pode e
            # declarar a chave vazia, porque ai o agrupamento vira por-acaso.
            errors.append(f"{where}: 'group_by' declarado deve ser lista nao vazia")
        if item is route:
            for dim in ("team", "service"):
                if dim not in (group_by or []):
                    errors.append(f"{where}: a rota raiz deveria agrupar por {dim!r}")
        if spot_key and spot_key not in known:
            errors.append(f"{where}: receiver {spot_key!r} nao esta em receivers")
        for field in ("group_wait", "group_interval", "repeat_interval"):
            value = item.get(field)
            if value is not None and not is_duration(str(value)):
                errors.append(f"{where}: {field} invalido: {value!r}")
        if item is route:
            for field in ("group_wait", "group_interval", "repeat_interval"):
                if field not in item:
                    errors.append(f"{where}: rota raiz deveria definir {field}")
        for mkey in ("match", "match_re"):
            if mkey in item:
                errors.append(f"{where}: use {mkey.replace('match', 'target_matcher')}s, {mkey} esta deprecado")
    severities_seen = []
    for _, item in routes:
        for matcher in item.get("matchers") or []:
            found = re.findall(r"severity\s*(=~|=)\s*\{?\"?([a-z]+)", str(matcher))
            severities_seen.extend(sev for _, sev in found)
    bad_sev = sorted(set(severities_seen) - ALLOWED_SEVERITIES)
    if bad_sev:
        errors.append(f"{rel}: rota filtra severity inexistente: {', '.join(bad_sev)}")

    inhibits = doc.get("inhibit_rules") or []
    if not inhibits:
        errors.append(f"{rel}: inibicao e obrigatoria (inhibit_rules) para evitar ruido")
    for iidx, rule in enumerate(inhibits):
        spot = f"{rel}: inhibit_rules[{iidx}]"
        if not isinstance(rule, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        if not rule.get("target_matchers") and not rule.get("source_matchers"):
            errors.append(f"{spot}: precisa de source_matchers e target_matchers")
        for field in ("equal", "equal_to"):
            if field in rule and not rule[field]:
                errors.append(f"{spot}: {field} vazio")
    if "time_intervals" in doc:
        defined = {
            str(t.get("name"))
            for t in (doc.get("time_intervals") or [])
            if isinstance(t, dict)
        }
        for _, item in routes:
            for key in ("mute_time_intervals", "active_time_intervals"):
                for value in item.get(key) or []:
                    if str(value) not in defined:
                        errors.append(f"{rel}: rota usa time_interval {value!r} nao definido")


# --------------------------------------------------------------------------
# OpenTelemetry Collector
# --------------------------------------------------------------------------


def validate_otel_collector(errors: list[str], services: set[str]):
    rel = "otel/collector-config.yaml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return
    require_keys(
        doc,
        (
            "receivers.otlp.protocols.grpc",
            "receivers.otlp.protocols.http",
            "receivers.hostmetrics",
            "receivers.filelog",
            "processors.batch",
            "processors.memory_limiter",
            "processors.attributes",
            "exporters.prometheus",
            "exporters.loki",
            "exporters.otlp/tempo",
            "service.pipelines",
        ),
        rel,
        errors,
    )
    declared = {
        "receivers": set((doc.get("receivers") or {}).keys()),
        "processors": set((doc.get("processors") or {}).keys()),
        "exporters": set((doc.get("exporters") or {}).keys()),
        "extensions": set((doc.get("extensions") or {}).keys()),
    }
    service = doc.get("service") or {}
    pipelines = service.get("pipelines") or {}
    if not pipelines:
        errors.append(f"{rel}: service.pipelines vazio")
    for kind in ("metrics", "logs", "traces"):
        if kind not in pipelines:
            errors.append(f"{rel}: falta o pipeline {kind!r}")
            continue
    for signal, pipe in pipelines.items():
        spot = f"{rel}: service.pipelines.{signal}"
        if not isinstance(pipe, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        for key in ("receivers", "processors", "exporters"):
            for component in pipe.get(key) or []:
                base = str(component).split("/")[0]
                # Componente parametrizado (otlp/tempo, file/traces-debug): o
                # nome completo e o que precisa existir na secao de configuracao.
                if str(component) in declared[key]:
                    continue
                if base not in declared[key]:
                    errors.append(f"{spot}: {key} usa {component!r}, que nao esta declarado")
        if not pipe.get("exporters"):
            errors.append(f"{spot}: pipeline sem exporters")
        procs = pipe.get("processors") or []
        if procs and procs[0] != "memory_limiter":
            errors.append(
                f"{spot}: memory_limiter deve ser o primeiro processor (recomendacao oficial)"
            )
        if "batch" not in procs:
            errors.append(f"{spot}: batch deveria estar no fim da cadeia de processors")
    otlp_grpc = get_path(doc, "receivers.otlp.protocols.grpc.endpoint") or ""
    if otlp_grpc and ":4317" not in str(otlp_grpc):
        errors.append(f"{rel}: otlp grpc deveria escutar em :4317, veio {otlp_grpc!r}")
    for exp_name, exporter in (doc.get("exporters") or {}).items():
        if not isinstance(exporter, dict):
            continue
        endpoint = str(exporter.get("endpoint", ""))
        host = re.sub(r"^https?://", "", endpoint).split(":")[0]
        if host and services and host not in services and host not in {"localhost", "0.0.0.0"}:
            errors.append(
                f"{rel}: exporters.{exp_name} aponta para {host!r}, ausente do docker-compose"
            )
    if not os.path.exists(abspath("otel/tagging.md")):
        errors.append("otel/tagging.md: documento do esquema de tags ausente")


# --------------------------------------------------------------------------
# Loki e Tempo
# --------------------------------------------------------------------------


def validate_loki(errors: list[str]):
    rel = "loki/config.yaml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return
    require_keys(
        doc,
        (
            "auth_enabled",
            "server.http_listen_port",
            "schema_config.configs",
            "storage_config.filesystem.directory",
            "compactor.working_directory",
            "table_manager.retention_period",
            "limits_config.max_line_size",
        ),
        rel,
        errors,
    )
    if doc.get("auth_enabled") is not False:
        errors.append(f"{rel}: auth_enabled deve ser false (ambiente local, multi-tenant desligado)")
    for cidx, cfg in enumerate(get_path(doc, "schema_config.configs") or []):
        spot = f"{rel}: schema_config.configs[{cidx}]"
        if not isinstance(cfg, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        require_keys(
            cfg,
            ("from", "store", "object_store", "schema", "index.prefix"),
            spot,
            errors,
        )
        if cfg.get("store") != "tsdb":
            errors.append(f"{spot}: use store tsdb; bolt-shipper esta deprecado")
    if not is_duration(str(get_path(doc, "table_manager.retention_period", ""))):
        errors.append(f"{rel}: table_manager.retention_period deveria ser uma duracao")
    if get_path(doc, "table_manager.retention_deletes_enabled") is not True:
        errors.append(f"{rel}: retention_deletes_enabled deve ser true para retencao valer")
    if get_path(doc, "memberlist.join_address") is None:
        errors.append(f"{rel}: memberlist.join_address (ou equivalent) deveria estar configurado")


def validate_tempo(errors: list[str]):
    rel = "tempo/tempo.yaml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return
    require_keys(
        doc,
        (
            "server.http_listen_port",
            "server.grpc_listen_port",
            "distributor.receivers.otlp.protocols.grpc.endpoint",
            "distributor.receivers.otlp.protocols.http.endpoint",
            "ingester.max_traces_per_user",
            "compactor.compaction.block_retention",
            "storage.trace.backend",
            "storage.trace.local.path",
            "metrics_generator",
        ),
        rel,
        errors,
    )
    if get_path(doc, "storage.trace.backend") not in ("local",):
        errors.append(f"{rel}: neste ambiente local o backend deve ser 'local'")
    for field in ("compactor.compaction.block_retention", "metrics_generator.metrics_ttl"):
        value = str(get_path(doc, field, ""))
        if value and not is_duration(value):
            errors.append(f"{rel}: {field} deveria ser uma duracao, veio {value!r}")
    rel_fe = "tempo/trace-frontend.yaml"
    fe = load_yaml(rel_fe, errors)
    if fe is None:
        return
    require_keys(
        fe,
        (
            "query_frontend.search.duration_slo",
            "query_frontend.traceql_2.engine",
            "query_frontend.compaction.enabled",
        ),
        rel_fe,
        errors,
    )
    for key in ("query_frontend", "server"):
        if key not in fe:
            errors.append(f"{rel_fe}: bloco {key} ausente no overlay")
    if not os.path.exists(abspath("jaeger/README.md")):
        errors.append("jaeger/README.md: justificativa de armazenamento Jaeger ausente")


# --------------------------------------------------------------------------
# docker-compose
# --------------------------------------------------------------------------

COMPOSE_SERVICES = {
    "prometheus",
    "alertmanager",
    "grafana",
    "loki",
    "tempo",
    "jaeger",
    "otel-collector",
    "sample-app",
    "load-generator",
}


def validate_compose(errors: list[str]) -> set[str]:
    rel = "docker-compose.yml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return set()
    if "version" in doc:
        errors.append(f"{rel}: chave 'version' esta obsoleta no Compose v2, remova")
    services = doc.get("services") or {}
    if not services:
        errors.append(f"{rel}: 'services' vazio")
    missing = sorted(COMPOSE_SERVICES - set(services))
    if missing:
        errors.append(f"{rel}: servicos obrigatorios ausentes: {', '.join(missing)}")
    networks = set(doc.get("networks") or {})
    volumes = set(doc.get("volumes") or {})
    host_ports: dict[str, str] = {}
    for name, svc in services.items():
        spot = f"{rel}: services.{name}"
        if not isinstance(svc, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        if not svc.get("image") and not svc.get("build"):
            errors.append(f"{spot}: precisa de 'image' ou 'build'")
        image = str(svc.get("image", ""))
        # Servico com `build:` legitima uma tag local (obs-stack/...); imagem
        # puramente externa precisa de registry explicito.
        if image and not svc.get("build") and not image.startswith("docker.io/"):
            errors.append(
                f"{spot}: prefira imagem com registry explicito (docker.io/...), veio {image!r}"
            )
        for pidx, mapping in enumerate(svc.get("ports") or []):
            text = str(mapping)
            m = re.match(r"^(\d{2,5}):(\d{2,5})", text)
            if not m:
                errors.append(f"{spot}.ports[{pidx}]: formato esperado 'HOST:CONTAINER', veio {text!r}")
                continue
            host, container = m.group(1), m.group(2)
            if host in host_ports:
                errors.append(
                    f"{spot}.ports[{pidx}]: porta do host {host} ja usada por {host_ports[host]}"
                )
            host_ports[host] = spot
            if "expose" in svc and container not in [str(e) for e in svc["expose"]]:
                errors.append(
                    f"{spot}: publica {host}:{container} mas nao declara {container} em 'expose'"
                )
        for vidx, mount in enumerate(svc.get("volumes") or []):
            source = str(mount).split(":")[0]
            if source.startswith("./"):
                target = abspath(os.path.relpath(os.path.join(ROOT, source)))
                if not os.path.exists(target):
                    errors.append(f"{spot}.volumes[{vidx}] : caminho montado nao existe: {source}")
            elif source not in volumes and not source.startswith("/"):
                errors.append(f"{spot}.volumes[{vidx}]: volume nomeado {source!r} nao esta em 'volumes:'")
        for dname in (svc.get("depends_on") or {}):
            if dname not in services:
                errors.append(f"{spot}.depends_on: servico inexistente {dname!r}")
        for net in svc.get("networks") or []:
            if networks and net not in networks:
                errors.append(f"{spot}.networks: rede inexistente {net!r}")
        hc = svc.get("healthcheck")
        if not hc:
            if name != "otel-collector":
                errors.append(f"{spot}: sem healthcheck")
            continue
        test = hc.get("test")
        if not test:
            errors.append(f"{spot}: healthcheck sem 'test'")
        for token in (test if isinstance(test, list) else [str(test)]):
            if "localhost" in str(token) and name in {"loki", "tempo", "prometheus", "alertmanager", "grafana"}:
                errors.append(
                    f"{spot}: use o nome do servico no healthcheck em vez de localhost"
                )
    return set(services)


# --------------------------------------------------------------------------
# Grafana: provisioning e dashboards
# --------------------------------------------------------------------------


def validate_datasources(errors: list[str], services: set[str]) -> set[str]:
    rel = "grafana/provisioning/datasources/datasources.yml"
    doc = load_yaml(rel, errors)
    uids: set[str] = set()
    if doc is None:
        return uids
    items = doc.get("datasources")
    if not isinstance(items, list) or not items:
        errors.append(f"{rel}: esperado 'datasources' como lista nao vazia")
        return uids
    for didx, ds in enumerate(items):
        spot = f"{rel}: datasources[{didx}]"
        if not isinstance(ds, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        require_keys(ds, ("name", "uid", "type", "access", "url"), spot, errors)
        uid = str(ds.get("uid", ""))
        if uid in uids:
            errors.append(f"{spot}: uid duplicado {uid!r}")
        uids.add(uid)
        if ds.get("access") not in ("proxy", "direct"):
            errors.append(f"{spot}: access deve ser proxy ou direct")
        url = str(ds.get("url", ""))
        host = re.sub(r"^https?://", "", url).split(":")[0]
        if services and host not in services:
            errors.append(
                f"{spot} ({ds.get('name')}): url aponta para {host!r}, ausente do docker-compose"
            )
        for key in ("password", "secureJsonData"):
            if key in ds and ds[key]:
                errors.append(f"{spot}: segredos nao pertencem ao repositorio ({key})")
        editable = ds.get("editable")
        if editable is True:
            errors.append(f"{spot}: prefira editable: false para o provisioning ser a fonte da verdade")
    return uids


def validate_dashboard_provisioning(errors: list[str]):
    rel = "grafana/provisioning/dashboards/dashboards.yml"
    doc = load_yaml(rel, errors)
    if doc is None:
        return
    providers = doc.get("providers") or []
    if not providers:
        errors.append(f"{rel}: 'providers' vazio")
        return
    for pidx, prov in enumerate(providers):
        spot = f"{rel}: providers[{pidx}]"
        if not isinstance(prov, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        require_keys(prov, ("name", "type", "options.path"), spot, errors)
        if prov.get("type") != "file":
            errors.append(f"{spot}: type deve ser 'file'")
        opts = prov.get("options") or {}
        path_in_container = str(opts.get("path", ""))
        if not path_in_container.startswith("/"):
            errors.append(f"{spot}: options.path deveria ser um caminho absoluto do container")
        disable_delete = opts.get("foldersFromFilesStructure")
        if disable_delete is None and prov.get("allowUiUpdates") is True:
            errors.append(f"{spot}: allowUiUpdates true perde o rastreio por git")
        if prov.get("allowUiUpdates") not in (None, False):
            if "disableDelete" in opts and opts["disableDelete"] is True:
                errors.append(f"{spot}: disableDelete:true + allowUiUpdates:true e contraditorio")
    if not os.path.isdir(abspath("grafana/dashboards")):
        errors.append("grafana/dashboards/: diretorio de dashboards montado ausente")


def _panel_rect(panel, spot, errors):
    gp = panel.get("gridPos")
    if not isinstance(gp, dict):
        errors.append(f"{spot}: 'gridPos' obrigatorio e deve ser objeto")
        return None
    for field in ("x", "y", "w", "h"):
        if not isinstance(gp.get(field), int):
            errors.append(f"{spot}: gridPos.{field} deve ser inteiro, veio {gp.get(field)!r}")
            return None
    if gp["w"] < 1 or gp["w"] > GRID_COLS:
        errors.append(f"{spot}: gridPos.w fora de 1..{GRID_COLS}")
    if gp["h"] < 1:
        errors.append(f"{spot}: gridPos.h deve ser >= 1")
    if gp["x"] < 0 or gp["x"] + gp["w"] > GRID_COLS:
        errors.append(f"{spot}: gridPos excede a largura de {GRID_COLS} colunas")
    return gp


def _overlaps(a, b) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def validate_dashboard(
    doc: dict, rel: str, known_uids: set[str], promql_rules: set[str]
) -> list[str]:
    """Valida um dashboard Grafana ja carregado."""
    errors: list[str] = []
    for field in ("uid", "title", "schemaVersion", "version", "panels", "time", "templating"):
        if field not in doc:
            errors.append(f"{rel}: campo obrigatorio ausente: {field}")
    uid = str(doc.get("uid", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,39}", uid):
        errors.append(f"{rel}: uid deveria ser estavel e slug ([a-z0-9-]), veio {uid!r}")
    if not isinstance(doc.get("schemaVersion"), int) or doc.get("schemaVersion", 0) < 36:
        errors.append(f"{rel}: schemaVersion plausivel (>=36) esperada")
    time_block = doc.get("time") or {}
    if not isinstance(time_block, dict) or "from" not in time_block or "to" not in time_block:
        errors.append(f"{rel}: bloco 'time' com from/to obrigatorio")
    templating = doc.get("templating") or {}
    if not isinstance(templating, dict) or "list" not in templating:
        errors.append(f"{rel}: 'templating.list' obrigatorio (varaveis de servico/jornada)")
    else:
        var_names = set()
        for vidx, var in enumerate(templating.get("list") or []):
            spot = f"{rel}: templating.list[{vidx}]"
            if not isinstance(var, dict):
                errors.append(f"{spot}: deveria ser um mapeamento")
                continue
            require_keys(var, ("name", "type"), spot, errors)
            if var.get("name") in var_names:
                errors.append(f"{spot}: variavel duplicada {var.get('name')!r}")
            var_names.add(var.get("name"))
            if var.get("type") == "query":
                ds_uid = str(get_path(var, "datasource.uid", ""))
                if known_uids and ds_uid and ds_uid not in known_uids:
                    errors.append(f"{spot}: datasource {ds_uid!r} desconhecido")
    panels = doc.get("panels") or []
    if not panels:
        errors.append(f"{rel}: nenhum painel")
        return errors
    ids = set()
    rects = []
    loki_panels = 0
    for pidx, panel in enumerate(panels):
        spot = f"{rel}: panels[{pidx}]"
        if not isinstance(panel, dict):
            errors.append(f"{spot}: deveria ser um mapeamento")
            continue
        title = panel.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{spot}: 'title' obrigatorio")
        ptype = panel.get("type")
        if ptype not in PANEL_TYPES:
            errors.append(f"{spot}: type {ptype!r} fora do conjunto esperado")
        pid = panel.get("id")
        if not isinstance(pid, int):
            errors.append(f"{spot}: 'id' deve ser inteiro")
        elif pid in ids:
            errors.append(f"{spot}: id de painel duplicado: {pid}")
        else:
            ids.add(pid)
        rect = _panel_rect(panel, spot, errors)
        if rect:
            for other_spot, other in rects:
                if _overlaps(rect, other):
                    errors.append(f"{spot}: gridPos sobrepoe o painel {other_spot}")
            rects.append((spot, rect))
        if panel.get("datasource") and panel.get("type") != "datasource":
            pass
        targets = panel.get("targets")
        if not isinstance(targets, list) or not targets:
            errors.append(f"{spot} ({title}): 'targets' deve ser uma lista nao vazia")
            continue
        ref_ids = set()
        for tidx, target in enumerate(targets):
            tsp = f"{spot}.targets[{tidx}] ({title})"
            if not isinstance(target, dict):
                errors.append(f"{tsp}: deveria ser um mapeamento")
                continue
            ds = target.get("datasource")
            if not isinstance(ds, dict) or not ds.get("uid"):
                errors.append(f"{tsp}: target precisa de datasource.uid")
            else:
                if known_uids and ds["uid"] not in known_uids:
                    errors.append(f"{tsp}: datasource.uid {ds['uid']!r} nao existe em datasources.yml")
                if ds.get("type") == "loki":
                    loki_panels += 1
            if not (target.get("expr") or target.get("query")):
                # TraceQL (datasource Tempo) usa 'query'; PromQL e LogQL usam
                # 'expr'. Os dois sao validos, o que nao pode e painel vazio.
                errors.append(f"{tsp}: target sem 'expr' nem 'query'")
            else:
                query_text = str(target.get("expr") or target.get("query"))
                problem = check_balanced(query_text)
                if problem:
                    errors.append(f"{tsp}: expr com sintaxe duvidosa: {problem}")
                ds_type = get_path(ds, "type", "")
                if ds_type == "prometheus":
                    unknown = sorted(t for t in colon_identifiers(query_text) if t not in promql_rules)
                    if unknown:
                        errors.append(
                            f"{tsp}: expr usa metricas de recording rule ausentes: {', '.join(unknown)}"
                        )
            ref = target.get("refId")
            if not ref:
                errors.append(f"{tsp}: refId obrigatorio")
            elif ref in ref_ids:
                errors.append(f"{tsp}: refId duplicado no painel: {ref}")
            else:
                ref_ids.add(str(ref))
        if ptype == "logs":
            derived = panel.get("derivedConfig") or panel.get("fieldConfig", {}).get(
                "defaults", {}
            ).get("links")
            fields = panel.get("derivedFields") or get_path(panel, "options.fields")
            has_tempo_link = False
            for cand in (derived or [], fields or []):
                if isinstance(cand, list):
                    for item in cand:
                        text = json.dumps(item)
                        if "trace_id" in text and "tempo" in text.lower():
                            has_tempo_link = True
            if not has_tempo_link and "derivedFields" not in json.dumps(panel):
                errors.append(
                    f"{spot} ({title}): painel de logs precisa de derivedFields com link de "
                    "trace_id para o Tempo"
                )
    if not loki_panels and "loki" in json.dumps(doc):
        errors.append(f"{rel}: menciona loki mas nenhum target usa o datasource do Loki")
    return errors


def validate_dashboards(errors: list[str], known_uids: set[str]):
    rules = collect_recording_rules()
    seen: dict[str, str] = {}
    for rel in DASHBOARD_FILES:
        full = abspath(rel)
        if not os.path.exists(full):
            errors.append(f"{rel}: dashboard ausente")
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                doc = json.load(fh)
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: JSON invalido: {exc}")
            continue
        if not isinstance(doc, dict):
            errors.append(f"{rel}: raiz do dashboard deveria ser um objeto")
            continue
        uid = str(doc.get("uid", ""))
        if uid in seen:
            errors.append(f"{rel}: uid {uid!r} ja usado em {seen[uid]}")
        seen[uid] = rel
        errors.extend(validate_dashboard(doc, rel, known_uids, rules))


# --------------------------------------------------------------------------
# Docs: runbooks, ADRs, custos, README, scripts
# --------------------------------------------------------------------------


def validate_runbooks(errors: list[str], alert_names: list[str]):
    index_rel = "runbooks/index.md"
    if not os.path.exists(abspath(index_rel)):
        errors.append(f"{index_rel}: matriz alerta-runbook-painel ausente")
        index_text = ""
    else:
        index_text = read_text(index_rel)
    if "Painel" not in index_text or "Alerta" not in index_text:
        if index_text:
            errors.append(f"{index_rel}: matriz precisa das colunas Alerta/Runbook/Painel")
    indexed = {name for name in alert_names if re.search(rf"\b{re.escape(name)}\b", index_text)}
    missing = sorted(set(alert_names) - indexed)
    if missing:
        errors.append(f"{index_rel}: alertas sem linha na matriz: {', '.join(missing)}")
    files = sorted(
        f for f in os.listdir(abspath("runbooks")) if f.endswith(".md") and f != "index.md"
    )
    expected = {"rb-latencia.md", "rb-taxa-erro.md", "rb-saturacao.md", "rb-throughput.md"}
    if not expected.issubset(set(files)):
        errors.append(f"runbooks/: faltam {', '.join(sorted(expected - set(files)))}")
    for name in files:
        rel = f"runbooks/{name}"
        text = read_text(rel)
        for section in RUNBOOK_REQUIRED_SECTIONS:
            if section not in text:
                errors.append(f"{rel}: secao obrigatoria ausente: {section}")
        found = [tool for tool in DIAGNOSTIC_TOOLS if re.search(rf"\b{tool}\b", text)]
        if len(found) < 3:
            errors.append(
                f"{rel}: use ao menos 3 ferramentas de diagnostico reais "
                f"(encontrei: {', '.join(found) or 'nenhuma'})"
            )
        if "aprovacao" not in normalize(text):
            errors.append(f"{rel}: acoes destrutivas precisam exigir aprovacao humana explicita")
        if "adr-" not in text.lower():
            errors.append(f"{rel}: deve apontar para o ADR correspondente ('o que aprender depois')")


def validate_adrs(errors: list[str]):
    docs = abspath("docs", "decisoes")
    if not os.path.isdir(docs):
        errors.append("docs/decisoes/: diretorio de ADRs ausente")
        return
    files = sorted(f for f in os.listdir(docs) if f.endswith(".md"))
    if len(files) < 4:
        errors.append(f"docs/decisoes/: esperado ao menos 4 ADRs, encontrei {len(files)}")
    for name in files:
        rel = f"docs/decisoes/{name}"
        text = read_text(rel)
        for section in ADR_REQUIRED_SECTIONS:
            if section not in text:
                errors.append(f"{rel}: secao obrigatoria ausente: {section}")
        if "Status:" not in text:
            errors.append(f"{rel}: ADR precisa de linha 'Status:'")
        if not re.search(r"aceita", normalize(text)):
            errors.append(f"{rel}: ADR deveria registrar a data de aceite")


def validate_readme(errors: list[str]):
    rel = "README.md"
    if not os.path.exists(abspath(rel)):
        errors.append("README.md: ausente")
        return
    text = read_text(rel)
    # normalize(): minusculo sem acentos, para "latencia" casar com "latencia"
    # e "sintetico" casar com "sintetico" sem obrigar o texto a ser sem acento.
    low = normalize(text)
    for needle in (
        "docker compose up",
        "golden signals",
        "latencia",
        "traffic",
        "errors",
        "saturation",
        "english summary",
        "sintetic",
        "runbooks/index.md",
    ):
        if needle not in low:
            errors.append(f"README.md: falta mencionar {needle!r}")
    if "->" not in text or "OTel Collector" not in text:
        errors.append("README.md: falta o diagrama ASCII do pipeline (app -> OTel Collector -> ...)")
    if not re.search(r"```text|```", text):
        errors.append("README.md: o diagrama deveria estar em bloco de codigo")
    for bad in ("emoji", "\U0001F680"):
        if bad in text:
            errors.append(f"README.md: conteudo de marketing visual nao pertence aqui ({bad})")


def validate_support_files(errors: list[str]):
    required = (
        "app/server.py",
        "app/loadgen.py",
        "app/stats.py",
        "app/tagging.py",
        "app/requirements.txt",
        "app/Dockerfile",
        "app/otel-sidecar.md",
        "docs/custos.md",
        "scripts/validate.py",
        "scripts/smoke-test.sh",
        "scripts/port-forward-notes.md",
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        ".gitignore",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
    )
    for rel in required:
        if not os.path.exists(abspath(rel)):
            errors.append(f"{rel}: arquivo obrigatorio do repositorio ausente")
    if os.path.exists(abspath("app/requirements.txt")):
        lines = [
            ln.strip()
            for ln in read_text("app/requirements.txt").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if lines:
            errors.append(
                f"app/requirements.txt: a app de exemplo deve ser stdlib only, achei: {', '.join(lines)}"
            )
    if os.path.exists(abspath("docs/custos.md")):
        text = read_text("docs/custos.md")
        if "=" not in text or not re.search(r"\d+\s*[*x]\s*\d+", text):
            errors.append("docs/custos.md: falta mostrar a conta aritmetica do custo de cardinalidade")
    if os.path.exists(abspath("scripts/smoke-test.sh")):
        text = read_text("scripts/smoke-test.sh")
        if not os.access(abspath("scripts/smoke-test.sh"), os.X_OK):
            errors.append("scripts/smoke-test.sh: deveria ser executavel (chmod +x)")
        if "50" not in text:
            errors.append("scripts/smoke-test.sh: deve disparar 50 requests sinteticos")
    for ci in (".github/workflows/ci.yml", ".gitlab-ci.yml"):
        if os.path.exists(abspath(ci)):
            text = read_text(ci).lower()
            for step in ("validate.py", "py_compile", "unittest", "smoke-test"):
                if step not in text:
                    errors.append(f"{ci}: falta a etapa {step!r}")
            if "secret" in text and "secrets." in text:
                errors.append(f"{ci}: pipeline nao deve usar segredos")


# --------------------------------------------------------------------------
# varredura de termos protegidos
# --------------------------------------------------------------------------


def validate_protected_terms(errors: list[str]):
    for rel in repo_text_files():
        try:
            text = normalize(read_text(rel))
        except OSError as exc:  # pragma: no cover
            errors.append(f"{rel}: falha ao ler: {exc}")
            continue
        for term in PROTECTED_TERMS:
            needle = normalize(term)
            if needle in text:
                line_no = next(
                    (
                        i
                        for i, ln in enumerate(normalize(read_text(rel)).splitlines(), 1)
                        if needle in ln
                    ),
                    0,
                )
                errors.append(
                    f"{rel}:{line_no}: termo protegido presente ({term!r}); "
                    "o projeto nao cita clientes, empregadores reais nem credenciais"
                )


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def validate_repo(root: str | None = None) -> tuple[list[str], dict[str, int]]:
    """Rodada completa; devolve (erros, contadores de itens validados)."""
    global ROOT
    if root:
        ROOT = os.path.abspath(root)
    errors: list[str] = []
    counts: dict[str, int] = {}
    alert_names = validate_rule_files(errors)
    counts["rule files"] = len(RULE_FILES)
    counts["alerts"] = len(alert_names)
    counts["recording rules"] = len(collect_recording_rules())
    services = validate_compose(errors)
    validate_prometheus_yml(errors, services)
    validate_alertmanager(errors)
    validate_otel_collector(errors, services)
    validate_loki(errors)
    validate_tempo(errors)
    ds_uids = validate_datasources(errors, services)
    counts["datasources"] = len(ds_uids)
    validate_dashboard_provisioning(errors)
    validate_dashboards(errors, ds_uids)
    counts["dashboards"] = len(DASHBOARD_FILES)
    validate_runbooks(errors, alert_names)
    validate_adrs(errors)
    validate_readme(errors)
    validate_support_files(errors)
    validate_protected_terms(errors)
    for rel in DASHBOARD_FILES:
        if os.path.exists(abspath(rel)):
            with open(abspath(rel), encoding="utf-8") as fh:
                counts["painéis"] = counts.get("painéis", 0) + len(json.load(fh).get("panels") or [])
    return errors, counts


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = argv[0] if argv else None
    errors, counts = validate_repo(root)
    print("observability-golden-signals-stack :: validador estrutural")
    print(f"repo: {ROOT}")
    for label in ("rule files", "recording rules", "alerts", "datasources", "dashboards", "painéis"):
        if label in counts:
            print(f"  {label:<16} {counts[label]}")
    print()
    checked = (
        "prometheus/rules/*.yml",
        "prometheus/prometheus.yml",
        "prometheus/alertmanager.yml",
        "otel/collector-config.yaml",
        "loki/config.yaml",
        "tempo/tempo.yaml",
        "tempo/trace-frontend.yaml",
        "docker-compose.yml",
        "grafana/provisioning/*/*.yml",
        "grafana/dashboards/*.json",
        "runbooks/*.md",
        "docs/decisoes/*.md",
        "README.md",
        "politica de termos protegidos",
    )
    print(f"escopos verificados: {len(checked)}")
    if errors:
        print(f"\nFALHAS ({len(errors)}):")
        for err in errors:
            print(f"  - {err}")
        print("\nresultado: FAIL")
        return 1
    print("\nresultado: OK - YAML/JSON estruturalmente validos e contrato atendido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
