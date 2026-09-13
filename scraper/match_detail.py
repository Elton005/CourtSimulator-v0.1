"""
scraper/sources/tennisexplorer/match_detail.py

Extrae de una página match-detail: ronda, superficie, cuotas de cierre
(Home/Away), datos de perfil de ambos jugadores y el ID canónico del torneo (slug).
"""

import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup
# Reemplaza la línea "from .rounds import ..." por esto:
try:
    from .rounds import normalize_round, round_sort_key
except ImportError:
    # Fallback por si se ejecuta el script directamente o se pierde el contexto del paquete
    from scraper.rounds import normalize_round, round_sort_key

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

SURFACE_MAP = {
    "hard": "hard",
    "clay": "clay",
    "grass": "grass",
    "indoors": "indoor",
    "indoor": "indoor",
    "carpet": "carpet",
}

BIO_FIELDS = ["Singles ranking", "Birthdate", "Height", "Weight", "Plays", "Turned pro"]

_NOISE_WORDS = {
    "hard", "clay", "grass", "indoors", "indoor", "carpet",
    "surface", "round", "final", "semifinal", "quarterfinal",
    "qualification", "challenger", "futures",
}


@dataclass
class MatchDetail:
    match_id: int
    date_str: str | None = None
    time_str: str | None = None
    tournament: str | None = None
    tournament_slug: str | None = None      # ← NUEVO: ID canónico del torneo
    tournament_year: int | None = None      # ← NUEVO: Año extraído de la URL
    round_name: str | None = None
    is_qualifying: bool = False
    surface: str | None = None
    closing_odds_a: float | None = None
    closing_odds_b: float | None = None
    player_a_bio: dict = field(default_factory=dict)
    player_b_bio: dict = field(default_factory=dict)
    player_a_full_name: str | None = None
    player_b_full_name: str | None = None
    player_a_photo: str | None = None
    player_b_photo: str | None = None


def fetch_match_detail_html(match_id: int) -> str:
    url = f"https://www.tennisexplorer.com/match-detail/?id={match_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text

def parse_header(soup: BeautifulSoup) -> dict:
    """
    Extrae la cabecera del partido. 
    AHORA COMPATIBLE con 'Today', 'Tomorrow' y fechas numéricas (DD.MM.YYYY).
    """
    h1 = soup.find('h1', class_='bg')
    if not h1:
        raise ValueError("No se encontró la etiqueta <h1 class='bg'>")
    
    header_div = h1.find_next_sibling('div')
    if not header_div:
        raise ValueError("No se encontró el div hermano del <h1>")
    
    header_text = header_div.get_text(separator=" ", strip=True)
    
    # 1. Extraer Fecha (Today, Tomorrow o DD.MM.YYYY) y Hora
    date_time_match = re.search(
        r"(Today|Tomorrow|\d{2}\.\d{2}\.\d{4})\s*,\s*(\d{2}:\d{2}|--:--)", 
        header_text, 
        re.IGNORECASE
    )
    if not date_time_match:
        raise ValueError(f"No se encontró fecha/hora en el texto: '{header_text}'")
    
    date_str_raw = date_time_match.group(1)
    time_str = date_time_match.group(2)
    
    # Normalizar la fecha a formato YYYY-MM-DD para la base de datos
    if date_str_raw.lower() == 'today':
        date_str = datetime.date.today().isoformat()
    elif date_str_raw.lower() == 'tomorrow':
        date_str = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    else:
        # Convertir DD.MM.YYYY a YYYY-MM-DD
        d, m, y = date_str_raw.split('.')
        date_str = f"{y}-{m}-{d}"
    
    # 2. Extraer el enlace del torneo para obtener el slug y el año
    tournament_link = header_div.find('a')
    tournament_slug = None
    tournament_year = None
    raw_tournament = "Unknown Tournament"
    
    if tournament_link:
        raw_tournament = tournament_link.get_text(strip=True)
        href = tournament_link.get('href', '')
        parts = href.strip("/").split("/")
        if len(parts) >= 2:
            tournament_slug = parts[0]
            try:
                tournament_year = int(parts[1])
            except ValueError:
                pass
    
    # 3. Extraer Superficie y Ronda del texto restante
    rest_of_text = header_text[date_time_match.end():].strip()
    surface = "unknown"
    surface_match = re.search(r"\b(hard|clay|grass|indoors|indoor|carpet)\b", rest_of_text, re.IGNORECASE)
    if surface_match:
        surface = surface_match.group(1).lower()
        rest_of_text = rest_of_text[:surface_match.start()].strip().rstrip(',')
    
    round_name = rest_of_text.strip() if rest_of_text else "Unknown"
    is_qualifying = bool(re.search(r"qual|q[\-\._]", round_name, re.IGNORECASE))
    
    return {
        "date_str": date_str,
        "time_str": time_str,
        "tournament": raw_tournament,
        "tournament_slug": tournament_slug,
        "tournament_year": tournament_year,
        "round_name": round_name,
        "is_qualifying": is_qualifying,
        "surface": SURFACE_MAP.get(surface, surface.title()),
    }


def _clean_player_name(raw: str) -> Optional[str]:
    if not raw:
        return None
    cleaned = raw
    for word in _NOISE_WORDS:
        cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\d\.\-\_]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) < 3:
        return None
    return cleaned


def parse_full_names(page_text: str) -> tuple[Optional[str], Optional[str]]:
    match = re.search(
        r"([A-Za-zÀ-ÿ'\s\.\-]+?)"
        r"\s+\d+\s*:\s*\d+\s*\([^)]*\)\s*"
        r"([A-Za-zÀ-ÿ'\s\.\-]+?)"
        r"(?=\s{2,}|Singles ranking|Birthdate|$)",
        page_text,
    )
    if not match:
        return None, None
    return _clean_player_name(match.group(1)), _clean_player_name(match.group(2))


def parse_player_photos(html: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    result_table = soup.select_one("table.gDetail")
    if not result_table:
        return None, None
    
    thumbs = result_table.select("td.thumb img")
    photo_a = photo_b = None
    
    if len(thumbs) >= 1 and thumbs[0].get("src"):
        photo_a = f"https://www.tennisexplorer.com{thumbs[0]['src']}"
    if len(thumbs) >= 2 and thumbs[1].get("src"):
        photo_b = f"https://www.tennisexplorer.com{thumbs[1]['src']}"
    
    return photo_a, photo_b


def parse_closing_odds(page_text: str) -> tuple[float | None, float | None]:
    match = re.search(r"Average odds\s+([\d.]+)\s+([\d.]+)", page_text)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def parse_player_bio_tables(html: str) -> tuple[dict, dict]:
    tables = pd.read_html(StringIO(html))
    bio_table = next((t for t in tables if any(field in t.to_string() for field in BIO_FIELDS)), None)
    
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
    if value is None:
        return None
    value = str(value).strip().rstrip(".")
    return None if value in ("-", "", "nan") else value


def parse_ranking(raw) -> int | None:
    value = _clean_or_none(raw)
    return int(value) if value else None


def parse_birthdate(raw) -> str | None:
    value = _clean_or_none(raw)
    if not value:
        return None
    parts = [p.strip().rstrip(".") for p in value.split(".") if p.strip()]
    if len(parts) == 3:
        return f"{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}"
    return None


def parse_cm(raw) -> int | None:
    value = _clean_or_none(raw)
    return int(value.replace("cm", "").strip()) if value else None


def parse_kg(raw) -> int | None:
    value = _clean_or_none(raw)
    return int(value.replace("kg", "").strip()) if value else None


def parse_year(raw) -> int | None:
    value = _clean_or_none(raw)
    if not value:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def clean_bio(bio: dict) -> dict:
    return {
        "current_ranking": parse_ranking(bio.get("Singles ranking")),
        "birth_date": parse_birthdate(bio.get("Birthdate")),
        "height_cm": parse_cm(bio.get("Height")),
        "weight_kg": parse_kg(bio.get("Weight")),
        "plays": _clean_or_none(bio.get("Plays")),
        "turned_pro": parse_year(bio.get("Turned pro")),
    }


def parse_match_detail(match_id: int, html: str) -> MatchDetail:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)

    detail = MatchDetail(match_id=match_id)
    # Esto actualiza automáticamente tournament_slug y tournament_year
    detail.__dict__.update(parse_header(soup))
    detail.closing_odds_a, detail.closing_odds_b = parse_closing_odds(page_text)
    detail.player_a_bio, detail.player_b_bio = parse_player_bio_tables(html)
    detail.player_a_full_name, detail.player_b_full_name = parse_full_names(page_text)
    detail.player_a_photo, detail.player_b_photo = parse_player_photos(html)

    return detail


if __name__ == "__main__":
    for test_id in (1974721, 1916239):  
        html = fetch_match_detail_html(test_id)
        result = parse_match_detail(test_id, html)
        print(f"Match {test_id}:")
        print(f"  Tournament: {result.tournament}")
        print(f"  Slug: {result.tournament_slug} | Year: {result.tournament_year}")
        print()