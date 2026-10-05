"""Execution crews, distinct from the authenticated audit actor.

Only evidenced spelling aliases are merged. Unknown/new operators remain free
text; whitespace is not a delimiter unless every word is a known operator.
"""
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

OPERATOR_REFERENCES = (
    "ALMOXARIFADO", "AMARILDO", "CARLOS", "VINICIUS", "CLEITON", "CM",
    "EBER FAVACHO", "EVERTON", "JEAN", "PAULO", "IAGO", "VITOR", "FELIPE",
    "FRED", "GEOVANY", "GRUPO EURO", "IGOR", "JOSE", "JUAREZ", "LUCAS",
    "LUIZ", "RENATO", "ROBERT", "RODRIGO", "SAMUEL", "SIDNEY", "THIAGO", "VICTOR",
)
OPERATOR_ALIASES = {
    "CLEILTON": "CLEITON", "CLEILRON": "CLEITON",
    "GEOVANE": "GEOVANY", "GEOVANNY": "GEOVANY",
    "EVRTON": "EVERTON", "JEN": "JEAN",
    "WILIAN": "WILLIAN",
}
_KNOWN = sorted(set(OPERATOR_REFERENCES) | set(OPERATOR_ALIASES), key=lambda name: (-len(name), name))
_KNOWN_PATTERN = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, _KNOWN)) + r")(?!\w)")


def operator_key(value):
    normalized = " ".join("".join(char for char in unicodedata.normalize("NFKD", str(value or ""))
                            if not unicodedata.combining(char)).upper().split())
    return "NÃO INFORMADO" if normalized == "NAO INFORMADO" else normalized


def operator_names(*values):
    names = []
    for value in values:
        if isinstance(value, (list, tuple)):
            pieces = operator_names(*value)
        else:
            pieces = []
            for chunk in re.split(r"\s*[/,;+&|]\s*|\s+E\s+", operator_key(value)):
                if not chunk:
                    continue
                matches = _KNOWN_PATTERN.findall(chunk)
                # CARLOS EVERTON is two people; JOAO PEDRO is one full name.
                parts = matches if matches and not _KNOWN_PATTERN.sub("", chunk).strip() else [chunk]
                pieces.extend(OPERATOR_ALIASES.get(part, part) for part in parts)
        for name in pieces:
            if name and name not in names:
                names.append(name)
    return names


def normalize_operators(value):
    return " / ".join(operator_names(value))


def execution_crew(payload, default):
    if "operadores" in payload:
        values = payload["operadores"]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("Informe os operadores em uma lista de nomes.")
        names = operator_names(values)
    else:
        names = operator_names(payload.get("responsavel") or default)
    if not names:
        raise ValueError("Informe ao menos um operador responsável.")
    if len(names) > 20 or any(len(name) > 160 for name in names):
        raise ValueError("Informe até 20 operadores, com no máximo 160 caracteres por nome.")
    return " / ".join(names)


def distribute_hours(hours, count):
    """Equal rounded shares whose sum is exactly the original stage total."""
    total = Decimal(str(hours or 0))
    if count < 1:
        raise ValueError("Informe ao menos um operador.")
    rounded = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return [rounded(total * (index+1)/count) - rounded(total * index/count) for index in range(count)]
