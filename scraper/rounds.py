"""
rounds.py

Normalización de rondas de tenis.
Mapea cualquier texto de ronda (en inglés, con puntos, abreviaciones, etc.)
a un vocabulario canónico cerrado, apto para análisis de datos.
"""

import re
from typing import Optional

# ============================================================================
# VOCABULARIO CANÓNICO
# Solo estos valores pueden aparecer en la columna `matches.round`.
# ============================================================================
CANONICAL_ROUNDS = {
    # Cuadro principal
    "F", "SF", "QF", "R16", "R32", "R64", "R128",
    # Qualifying
    "Q1", "Q2", "Q3", "QF",  # QF se reutiliza; se distingue por is_qualifying
    # Casos especiales
    "RR",        # Round Robin (Davis Cup, etc.)
    "PO",        # Play-off
    "3RD",       # Tercer puesto
    "UNKNOWN",   # Fallback cuando no se puede identificar
}

# Orden numérico para análisis (menor = etapa más temprana)
ROUND_ORDER = {
    "Q1": 1, "Q2": 2, "Q3": 3,
    "R128": 10, "R64": 20, "R32": 30, "R16": 40,
    "QF": 50, "SF": 60, "F": 70,
    "RR": 5, "PO": 75, "3RD": 72,
    "UNKNOWN": 0,
}

# ============================================================================
# MAPPER: texto crudo → valor canónico
# Las claves son patrones regex (case-insensitive).
# El orden importa: se evalúan de arriba hacia abajo, gana el primer match.
# ============================================================================
_ROUND_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Qualifying: "Qualification - 1. round", "Q-1R", "Q-R16", "Q-QF", etc.
    (re.compile(r"qual(?:ification)?[\s\-]*final", re.I),           "QF"),
    (re.compile(r"qual(?:ification)?[\s\-]*semifinal", re.I),       "SF"),
    (re.compile(r"qual(?:ification)?[\s\-]*quarterfinal", re.I),    "QF"),
    (re.compile(r"qual(?:ification)?[\s\-]*round\s*of\s*16", re.I), "R16"),
    (re.compile(r"qual(?:ification)?[\s\-]*3\.\s*round", re.I),     "Q3"),
    (re.compile(r"qual(?:ification)?[\s\-]*2\.\s*round", re.I),     "Q2"),
    (re.compile(r"qual(?:ification)?[\s\-]*1\.\s*round", re.I),     "Q1"),
    (re.compile(r"q[\-\.]?\s*qf", re.I),                            "QF"),
    (re.compile(r"q[\-\.]?\s*sf", re.I),                            "SF"),
    (re.compile(r"q[\-\.]?\s*r16", re.I),                           "R16"),
    (re.compile(r"q[\-\.]?\s*3r", re.I),                            "Q3"),
    (re.compile(r"q[\-\.]?\s*2r", re.I),                            "Q2"),
    (re.compile(r"q[\-\.]?\s*1r", re.I),                            "Q1"),

    # Cuadro principal: formas completas
    (re.compile(r"\bfinal\b", re.I),                                "F"),
    (re.compile(r"\bsemifinal(?:s)?\b", re.I),                      "SF"),
    (re.compile(r"\bquarterfinal(?:s)?\b", re.I),                   "QF"),
    (re.compile(r"\bround\s*of\s*16\b", re.I),                      "R16"),
    (re.compile(r"\bround\s*of\s*32\b", re.I),                      "R32"),
    (re.compile(r"\bround\s*of\s*64\b", re.I),                      "R64"),
    (re.compile(r"\bround\s*of\s*128\b", re.I),                     "R128"),
    (re.compile(r"\b3rd\s*(?:place|position)?\b", re.I),            "3RD"),
    (re.compile(r"\bplay[\s\-]*off\b", re.I),                       "PO"),
    (re.compile(r"\bround[\s\-]*robin\b", re.I),                    "RR"),

    # Cuadro principal: abreviaciones
    (re.compile(r"\bF\b"),                                          "F"),
    (re.compile(r"\bSF\b"),                                         "SF"),
    (re.compile(r"\bQF\b"),                                         "QF"),
    (re.compile(r"\bR16\b"),                                        "R16"),
    (re.compile(r"\bR32\b"),                                        "R32"),
    (re.compile(r"\bR64\b"),                                        "R64"),
    (re.compile(r"\bR128\b"),                                       "R128"),

    # Cuadro principal: "1. round", "2nd round", "1R", "2R", etc.
    (re.compile(r"\b128[\.\s]*round\b|\b128r\b", re.I),             "R128"),
    (re.compile(r"\b64[\.\s]*round\b|\b64r\b", re.I),               "R64"),
    (re.compile(r"\b32[\.\s]*round\b|\b32r\b", re.I),               "R32"),
    (re.compile(r"\b16[\.\s]*round\b|\b16r\b", re.I),               "R16"),
    (re.compile(r"\b4[\.\s]*round\b|\b4th\s*round\b|\b4r\b", re.I), "QF"),
    (re.compile(r"\b3[\.\s]*round\b|\b3rd\s*round\b|\b3r\b", re.I), "R16"),
    (re.compile(r"\b2[\.\s]*round\b|\b2nd\s*round\b|\b2r\b", re.I), "R32"),
    (re.compile(r"\b1[\.\s]*round\b|\b1st\s*round\b|\b1r\b", re.I), "R64"),
]


def normalize_round(raw: Optional[str]) -> tuple[str, bool]:
    """
    Normaliza un texto de ronda al vocabulario canónico.
    
    Devuelve:
        (round_canonical: str, is_qualifying: bool)
    
    Ejemplos:
        "Qualification - 2. round"  → ("Q2", True)
        "1R"                        → ("R64", False)
        "semifinal"                 → ("SF", False)
        "Q-QF"                      → ("QF", True)
        "Final"                     → ("F", False)
        None o ""                   → ("UNKNOWN", False)
    """
    if not raw:
        return "UNKNOWN", False
    
    text = raw.strip()
    
    # Detectar si es qualifying ANTES de normalizar
    is_qualifying = bool(re.search(r"qual|q[\-\.]", text, re.I))
    
    # Probar cada patrón en orden
    for pattern, canonical in _ROUND_PATTERNS:
        if pattern.search(text):
            return canonical, is_qualifying
    
    # Fallback: loguear para revisión manual
    return "UNKNOWN", is_qualifying


def round_sort_key(round_code: str) -> int:
    """Devuelve un número para ordenar rondas cronológicamente."""
    return ROUND_ORDER.get(round_code, 0)


def is_main_draw_round(round_code: str) -> bool:
    """True si la ronda pertenece al cuadro principal (no qualifying)."""
    return round_code in {"F", "SF", "QF", "R16", "R32", "R64", "R128", "RR", "3RD", "PO"}