"""Esquema de tags padronizado do stack.

Este modulo e a versao executavel do que esta escrito em ``otel/tagging.md``:
as chaves que uma aplicacao DEVE ter em log, metrica e span, e as chaves que
NAO podem virar label de metrica nem label de stream de log.

Por que isso mora em codigo e nao so em documento: um padrao de tags que só
existe em wiki e violado na primeira sprint. Aqui o schema e importado pela
aplicacao, pelos testes e pelo validador do repositorio, entao um campo
obrigatorio ausente quebra o CI antes de virar painel sem dado.
"""

from __future__ import annotations

import re

__all__ = [
    "REQUIRED_TAGS",
    "OPTIONAL_TAGS",
    "FORBIDDEN_METRIC_LABELS",
    "FORBIDDEN_LOG_STREAM_LABELS",
    "ALLOWED_SEVERITIES",
    "JOURNEYS",
    "normalize_journey",
    "resource_attributes",
    "validate_tags",
    "cardinality_risk",
    "labelize",
]

# Identidade do servico. `service` e `environment` sao o par minimo para
# qualquer correlacao entre os tres sinais; sem eles nao ha join.
REQUIRED_TAGS = ("service", "environment", "journey", "team")

# Contexto que acelera triagem mas que nunca deve virar label de metrica.
OPTIONAL_TAGS = ("owner", "version", "region", "instance", "cost_center", "request_id", "trace_id")

# Alto cardinalidade por construcao: cada valor novo e uma serie temporal na
# memoria do Prometheus e um stream novo no Loki. Ver docs/custos.md.
FORBIDDEN_METRIC_LABELS = (
    "trace_id",
    "request_id",
    "user_id",
    "customer_id",
    "pod",
    "pod_ip",
    "http.url",
    "http.target",
    "sql.statement",
    "error.message",
    "session",
)

# No Loki, label de stream vira indice. Conteudo de linha NAO e indexado de
# proposito: o trace_id fica no corpo do log e e extraido pelo parser.
FORBIDDEN_LOG_STREAM_LABELS = FORBIDDEN_METRIC_LABELS + ("message", "body", "stacktrace")

ALLOWED_SEVERITIES = ("debug", "info", "warn", "error", "fatal")

# Jornadas sinteticas do ambiente de exemplo.
JOURNEYS = ("checkout", "login", "descoberta", "notificacao")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def labelize(text: str) -> str:
    """Converte rotulo livre em slug estavel, para cardinalidade controlada."""
    return _SLUG_RE.sub("-", str(text).strip().lower()).strip("-") or "desconhecido"


def normalize_journey(value: str | None) -> str:
    """Mapeia nome de jornada para o vocabulario controlado do esquema.

    Jornada e dimensao de negocio, entao o conjunto de valores precisa ser
    pequeno e fechado: texto livre aqui e a receita para um painel com 400
    variaveis e para custo de indice inesperado.
    """
    texto = str(value or "").strip()
    if not texto:
        # Sem valor nao existe jornada: "outra" e o unico resultado honesto.
        # labelize("") devolve o placeholder "desconhecido", que casa com o
        # prefixo de "descoberta" e inventaria uma jornada que ninguem pediu.
        return "outra"
    slug = labelize(texto)
    if slug in JOURNEYS:
        return slug
    for known in JOURNEYS:
        if known.startswith(slug[:4]) and len(slug) >= 4:
            return known
    return "outra"


def resource_attributes(
    service: str,
    environment: str,
    journey: str,
    team: str,
    owner: str | None = None,
    version: str = "0.1.0",
    region: str = "local-1",
    cost_center: str = "sintetico-plataforma",
) -> dict[str, str]:
    """Atributos de resource OTel no esquema do projeto.

    Os nomes seguem a semantica do OpenTelemetry (``service.name``,
    ``deployment.environment.name``); os do esquema do projeto vao junto como
    atributos explicitos para que Loki e Tempo indexem o mesmo vocabulario que
    o Prometheus ve no scrape.
    """
    return {
        "service.name": service,
        "service.version": version,
        "deployment.environment.name": environment,
        "service.instance.id": f"{service}-{region}",
        "journey": normalize_journey(journey),
        "team": team,
        "owner": owner or f"{team}-oncall",
        "region": region,
        "cost_center": cost_center,
    }


def validate_tags(tags: dict[str, object], kind: str = "log") -> list[str]:
    """Retorna a lista de problemas do schema (vazia quando o registro e valido)."""
    problems: list[str] = []
    for key in REQUIRED_TAGS:
        value = tags.get(key)
        if value in (None, ""):
            problems.append(f"tag obrigatoria ausente ou vazia: {key}")
        elif not isinstance(value, str):
            problems.append(f"tag {key} deveria ser string, veio {type(value).__name__}")
    if tags.get("journey") and tags["journey"] != normalize_journey(str(tags["journey"])):
        problems.append(
            f"journey fora do vocabulario controlado: {tags['journey']!r} "
            f"(normalizaria para {normalize_journey(str(tags['journey']))!r})"
        )
    if kind == "log":
        level = tags.get("level") or tags.get("severity")
        if level is not None and str(level) not in ALLOWED_SEVERITIES:
            problems.append(f"level invalido: {level!r}, usar um de {ALLOWED_SEVERITIES}")
        for field in ("timestamp", "message"):
            if field in tags and tags[field] in (None, ""):
                problems.append(f"campo de log vazio: {field}")
    forbidden = FORBIDDEN_METRIC_LABELS if kind == "metric" else FORBIDDEN_LOG_STREAM_LABELS
    leaked = sorted(key for key in tags if key in forbidden and key not in REQUIRED_TAGS)
    if leaked and kind == "metric":
        problems.append(f"label de alto cardinalidade em metrica: {', '.join(leaked)}")
    return problems


def cardinality_risk(keys: list[str] | tuple[str, ...] | set[str]) -> dict[str, str]:
    """Classifica cada chave em baixa, media ou alta (inaceitavel) risco.

    Ferramenta de revisao de PR: ao adicionar um label, a resposta e se o custo
    de series que aquilo multiplica vale a pergunta que o label responde.
    """
    risk: dict[str, str] = {}
    for key in keys:
        if key in FORBIDDEN_METRIC_LABELS:
            risk[key] = "alta"
        elif key in REQUIRED_TAGS or key in ("route", "status_class", "dependency", "outcome"):
            risk[key] = "baixa"
        elif key in ("status", "region", "version", "team", "le"):
            risk[key] = "media"
        else:
            risk[key] = "media"
    return risk
