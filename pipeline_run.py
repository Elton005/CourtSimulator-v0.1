"""
scraper/sources/tennisexplorer/match_detail.py

Extrae de una página match-detail: ronda, superficie, cuotas de cierre
(Home/Away) y datos de perfil de ambos jugadores.

Estrategia: trabaja sobre el TEXTO PLANO de la página (vía
BeautifulSoup(html).get_text()) para la cabecera y las cuotas, y con
pandas.read_html() para las tablas de bio/H2H -- ambos métodos son
más robustos que depender de clases CSS específicas, que todavía no
hemos confirmado con el HTML crudo real.

Falta confirmar (marcado con TODO):
- La etiqueta exacta que usa TennisExplorer para rondas de clasificación
  (qualy). Necesitamos ver un ejemplo real de un partido de qualy.
- Si "Average odds" bajo Home/Away es siempre la PRIMERA ocurrencia de
  ese texto en la página (asumido aquí, a confirmar con más ejemplos).
"""

import re
from dataclasses import dataclass, field
from io import StringIO

import requests
import pandas as pd
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

HEADER_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s*,\s*(\d{2}:\d{2}|--:--)\s*,\s*(.+?)\s*,\s*"
    r"([\w. -]*?(?:round|Final|Semifinal|Quarterfinal|Qualification|Q\d))\s*,\s*"
    r"([\w/]+)"
)

SURFACE_MAP = {
    "hard": "hard",
    "clay": "clay",
    "grass": "grass",
    "indoors": "indoor",
    "indoor": "indoor",
}

BIO_FIELDS = ["Singles ranking", "Birthdate", "Height", "Weight", "Plays", "Turned pro"]


@dataclass
class MatchDetail:
    match_id: int
    date_str: str | None = None
    time_str: str | None = None
    tournament: str | None = None
    round_name: str | None = None
    is_qualifying: bool = False
    surface: str | None = None
    closing_odds_a: float | None = None
    closing_odds_b: float | None = None
    player_a_bio: dict = field(default_factory=dict)
    player_b_bio: dict = field(default_factory=dict)


def fetch_match_detail_html(match_id: int) -> str:
    url = f"https://www.tennisexplorer.com/match-detail/?id={match_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_header(page_text: str) -> dict:
    match = HEADER_RE.search(page_text)
    if not match:
        # DEBUG temporal: busca "round" en el texto real para ver el
        # contexto exacto que produce BeautifulSoup sobre el HTML real.
        idx = page_text.lower().find("round")
        contexto = page_text[max(0, idx - 100):idx + 100] if idx != -1 else "(no se encontró la palabra 'round' en absoluto)"
        print("---- DEBUG: contexto real alrededor de 'round' ----")
        print(repr(contexto))
        print("----------------------------------------------------")
        raise ValueError("No se encontró la línea de cabecera (fecha/ronda/superficie)")
    date_str, time_str, tournament, round_name, surface = match.groups()
    return {
        "date_str": date_str,
        "time_str": time_str,
        "tournament": tournament.strip(),
        "round_name": round_name.strip(),
        "is_qualifying": "qual" in round_name.lower(),
        "surface": SURFACE_MAP.get(surface.lower().strip()),
    }


def parse_closing_odds(page_text: str) -> tuple[float | None, float | None]:
    """
    Busca la primera línea 'Average odds X Y', que en el orden habitual
    de la página corresponde al mercado Home/Away (cuota de cierre).
    """
    match = re.search(r"Average odds\s+([\d.]+)\s+([\d.]+)", page_text)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def parse_player_bio_tables(html: str) -> tuple[dict, dict]:
    """
    Usa pandas.read_html para encontrar la tabla de bio (ranking,
    nacimiento, altura, peso, mano dominante, año pro) sin depender
    de clases CSS -- solo necesita que exista un <table> real.
    """
    tables = pd.read_html(StringIO(html))
    bio_table = None
    for t in tables:
        text_blob = t.to_string()
        if any(field in text_blob for field in BIO_FIELDS):
            bio_table = t
            break

    if bio_table is None:
        return {}, {}

    player_a_bio, player_b_bio = {}, {}
    for _, row in bio_table.iterrows():
        label = str(row.iloc[2]).strip() if len(row) > 2 else ""
        if label in BIO_FIELDS:
            player_a_bio[label] = row.iloc[1]
            player_b_bio[label] = row.iloc[3] if len(row) > 3 else None

    return clean_bio(player_a_bio), clean_bio(player_b_bio)


def _clean_or_none(value) -> str | None:
    value = str(value).strip()
    return None if value in ("-", "", "nan") else value


def parse_ranking(raw) -> int | None:
    value = _clean_or_none(raw)
    if value is None:
        return None
    return int(value.rstrip("."))


def parse_birthdate(raw) -> str | None:
    """
    TennisExplorer usa el formato 'D. M. AAAA', ej. '21. 5. 1996'.
    Devuelve 'AAAA-MM-DD' (ISO), listo para la columna birth_date.
    """
    value = _clean_or_none(raw)
    if value is None:
        return None
    day, month, year = [p.strip().rstrip(".") for p in value.split(".") if p.strip()]
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def parse_cm(raw) -> int | None:
    value = _clean_or_none(raw)
    if value is None:
        return None
    return int(value.replace("cm", "").strip())


def parse_kg(raw) -> int | None:
    value = _clean_or_none(raw)
    if value is None:
        return None
    return int(value.replace("kg", "").strip())


def clean_bio(bio: dict) -> dict:
    return {
        "current_ranking": parse_ranking(bio.get("Singles ranking")),
        "birth_date": parse_birthdate(bio.get("Birthdate")),
        "height_cm": parse_cm(bio.get("Height")),
        "weight_kg": parse_kg(bio.get("Weight")),
        "plays": _clean_or_none(bio.get("Plays")),
        "turned_pro": _clean_or_none(bio.get("Turned pro")),
    }


def parse_match_detail(match_id: int, html: str) -> MatchDetail:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)

    detail = MatchDetail(match_id=match_id)
    detail.__dict__.update(parse_header(page_text))
    detail.closing_odds_a, detail.closing_odds_b = parse_closing_odds(page_text)
    detail.player_a_bio, detail.player_b_bio = parse_player_bio_tables(html)

    return detail


if __name__ == "__main__":
    for test_id in (3307389, 3303261):  # partido normal + partido de qualy
        html = fetch_match_detail_html(test_id)
        result = parse_match_detail(test_id, html)
        print(result)
        print()