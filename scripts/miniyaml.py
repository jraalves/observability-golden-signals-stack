"""Parser YAML de subconjunto, stdlib only.

Este modulo nao pretende substituir PyYAML. O objetivo e permitir que
``scripts/validate.py`` rode em qualquer runner de CI sem instalar
dependencias e ainda assim entregue estrutura (dict/list) para as
verificacoes semanticas dos arquivos de configuracao do repositorio.

Subconjunto suportado (e tudo que os arquivos YAML deste repositorio usam):

* mapeamentos aninhados com indentacao em multiplos de 2 espacos;
* sequencias em bloco (``- item``), inclusive mapeamentos iniciados na
  mesma linha do traco;
* sequencias e mapeamentos em fluxo (``[a, b]``, ``{a: b}``);
* escalares plain, single-quoted e double-quoted, int, float, bool e null;
* comentarios de linha (``#`` fora de aspas) e linhas em branco.

Construcoes explicitamente rejeitadas, para manter os arquivos de
configuracao triviais de ler e de auditar: block scalars (``|``, ``>``),
anchors/aliases (``&``, ``*``), tags (``!!``), documentos multiplos
(``---``), indentacao com tabulacao e chaves duplicadas.
"""

from __future__ import annotations

__all__ = ["YamlError", "parse", "parse_file", "parse_value"]

DIGITS = set("0123456789")
BLOCK_MARKERS = ("|", "|-", "|+", ">", ">-", ">+")


class YamlError(ValueError):
    """Erro de sintaxe/estrutura com numero de linha quando disponivel."""

    def __init__(self, message: str, line_no: int | None = None):
        super().__init__(f"linha {line_no}: {message}" if line_no else message)
        self.line_no = line_no


# --------------------------------------------------------------------------
# utilitarios de lex
# --------------------------------------------------------------------------


def _skip_string(text: str, i: int = 0) -> int:
    """Indice logo apos o termino da string que comeca em ``text[i]``."""
    quote = text[i]
    i += 1
    n = len(text)
    while i < n:
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    raise YamlError("string nao fechada")


def _strip_comment(text: str) -> str:
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _find_colon(text: str) -> int:
    """Indice do ``:`` separador de chave/valor, ou -1 quando nao e um par."""
    i = 0
    n = len(text)
    depth = 0
    while i < n:
        ch = text[i]
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            if i + 1 >= n or text[i + 1] in " \t":
                return i
        i += 1
    return -1


def _split_key(text: str, line_no: int) -> tuple[str, str]:
    idx = _find_colon(text)
    if idx < 0:
        raise YamlError(f"esperava 'chave: valor', encontrei: {text!r}", line_no)
    raw_key = text[:idx].strip()
    value = text[idx + 1 :].strip()
    key = _scalar(raw_key, line_no)
    return (key if isinstance(key, str) else raw_key), value


# --------------------------------------------------------------------------
# escalares e fluxo
# --------------------------------------------------------------------------


def _scalar(text: str, line_no: int | None):
    t = text.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        q = t[0]
        body = t[1:-1]
        if q == "'":
            return body.replace("''", "'")
        return body.replace("\\\\", "\x00").replace('\\"', '"').replace(
            "\\n", "\n"
        ).replace("\x00", "\\")
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if t in BLOCK_MARKERS:
        raise YamlError(
            "block scalar ('|' ou '>') nao e suportado pelo validador; "
            "use uma string entre aspas",
            line_no,
        )
    if t[0] in "[{":
        return _flow(t, line_no)
    if t[0] in "&*!%>":
        raise YamlError(f"construto YAML nao suportado: {t!r}", line_no)
    if t[0] in "+-." or (t and t[0] in DIGITS):
        try:
            return int(t, 10)
        except ValueError:
            pass
        try:
            return float(t)
        except ValueError:
            pass
    return t


def _flow_items(body: str) -> list[str]:
    items: list[str] = []
    cur = ""
    depth = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in "\"'":
            j = _skip_string(body, i)
            cur += body[i:j]
            i = j
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur.strip())
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    if cur.strip():
        items.append(cur.strip())
    return items


def _flow(text: str, line_no: int | None):
    t = text.strip()
    if t.startswith("["):
        if not t.endswith("]"):
            raise YamlError(f"sequencia em fluxo nao fechada: {t!r}", line_no)
        return [_node_or_scalar(item, line_no) for item in _flow_items(t[1:-1])]
    if t.startswith("{"):
        if not t.endswith("}"):
            raise YamlError(f"mapeamento em fluxo nao fechado: {t!r}", line_no)
        out: dict = {}
        for item in _flow_items(t[1:-1]):
            if item == "":
                continue
            idx = _find_colon(item)
            if idx < 0:
                out[_scalar(item, line_no)] = None
            else:
                out[_scalar(item[:idx], line_no)] = _scalar(item[idx + 1 :], line_no)
        return out
    raise YamlError(f"fluxo invalido: {t!r}", line_no)


def _node_or_scalar(text: str, line_no: int | None):
    t = text.strip()
    if not t:
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return _scalar(t, line_no)
    if t[0] in "[{":
        return _flow(t, line_no)
    return _scalar(t, line_no)


def parse_value(text: str, line_no: int | None = None):
    """Interpreta o lado direito de um ``chave:`` ja isolado."""
    return _node_or_scalar(text, line_no)


# --------------------------------------------------------------------------
# parser em bloco
# --------------------------------------------------------------------------


class _Line:
    __slots__ = ("indent", "text", "no")

    def __init__(self, indent: int, text: str, no: int):
        self.indent = indent
        self.text = text
        self.no = no


def _is_seq_line(text: str) -> bool:
    return text == "-" or text.startswith("- ") or text.startswith("-\t")


def _parse(lines: list[_Line], i: int, indent: int):
    if _is_seq_line(lines[i].text):
        return _parse_seq(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_seq(lines: list[_Line], i: int, indent: int):
    out: list = []
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.indent < indent or (ln.indent == indent and not _is_seq_line(ln.text)):
            break
        if ln.indent > indent:
            raise YamlError("indentacao de lista inconsistente", ln.no)
        rest = ln.text[2:].strip() if len(ln.text) > 2 else ""
        item_indent = ln.indent + 2
        if rest == "":
            child, i = _parse_child(lines, i + 1, ln.indent)
            out.append(child)
            continue
        if _is_seq_line(rest):
            lines[i] = _Line(item_indent, rest, ln.no)
            child, i = _parse_seq(lines, i, item_indent)
            out.append(child)
            continue
        if _find_colon(rest) >= 0:
            lines[i] = _Line(item_indent, rest, ln.no)
            child, i = _parse_map(lines, i, item_indent)
            out.append(child)
            continue
        out.append(_node_or_scalar(rest, ln.no))
        i += 1
    return out, i


def _parse_child(lines: list[_Line], i: int, parent_indent: int):
    """Valor aninhado que vem depois de ``chave:`` ou de um ``-`` isolado."""
    if i >= len(lines):
        return None, i
    nxt = lines[i]
    if nxt.indent > parent_indent:
        return _parse(lines, i, nxt.indent)
    if nxt.indent == parent_indent and _is_seq_line(nxt.text):
        return _parse_seq(lines, i, nxt.indent)
    return None, i


def _parse_map(lines: list[_Line], i: int, indent: int):
    out: dict = {}
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.indent < indent:
            break
        if ln.indent > indent:
            raise YamlError(f"indentacao inesperada em {ln.text!r}", ln.no)
        if _is_seq_line(ln.text):
            break
        key, raw_value = _split_key(ln.text, ln.no)
        if key in out:
            raise YamlError(f"chave duplicada {key!r}", ln.no)
        if raw_value == "":
            child, i = _parse_child(lines, i + 1, ln.indent)
            out[key] = child
            continue
        out[key] = _node_or_scalar(raw_value, ln.no)
        i += 1
    return out, i


def parse(text: str):
    """Faz parse do subconjunto YAML descrito no docstring do modulo."""
    raw_lines = text.splitlines()
    for no, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if stripped == "---":
            raise YamlError("documentos multiplos nao sao suportados", no)
        if stripped.startswith("%"):
            raise YamlError("diretivas de documento nao sao suportadas", no)
        prefix = line[: len(line) - len(line.lstrip())]
        if "\t" in prefix:
            raise YamlError("tabulacao usada para indentar YAML (use espacos)", no)

    lines: list[_Line] = []
    for no, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = _strip_comment(line[indent:])
        if content.strip() == "":
            continue
        lines.append(_Line(indent, content, no))

    if not lines:
        return {}
    value, consumed = _parse(lines, 0, lines[0].indent)
    if consumed < len(lines):
        raise YamlError(
            f"conteudo sobrando apos o fim do documento: {lines[consumed].text!r}",
            lines[consumed].no,
        )
    return value


def parse_file(path: str):
    with open(path, encoding="utf-8") as fh:
        return parse(fh.read())
