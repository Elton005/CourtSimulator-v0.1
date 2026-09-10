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
from rounds import normalize_round

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

HEADER_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s*,\s*"          # Fecha
    r"(\d{2}:\d{2}|--:--)\s*,\s*"            # Hora
    r"(.+?)\s*,\s*"                           # Torneo (no-greedy, pero...)
    r"([\w. -]*?(?:round|Final|Semifinal|Quarterfinal|Qualification|Q\d))\s*,\s*" # Ronda
    r"([\w/]+)"                               # Superficie
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
    player_a_full_name: str | None = None
    player_b_full_name: str | None = None
    player_a_photo: str | None = None  # ← NUEVO
    player_b_photo: str | None = None  # ← NUEVO


def fetch_match_detail_html(match_id: int) -> str:
    url = f"https://www.tennisexplorer.com/match-detail/?id={match_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_header(soup: BeautifulSoup) -> dict:
    h1 = soup.find('h1', class_='bg')
    if h1:
        header_div = h1.find_next_sibling('div')
        if header_div:
            header_text = header_div.get_text(separator=" ", strip=True)
            
            HEADER_RE = re.compile(
                r"(\d{2}\.\d{2}\.\d{4})\s*,\s*"
                r"(\d{2}:\d{2}|--:--)\s*,\s*"
                r"(.+?)\s*,\s*"
                r"(.+?)\s*,\s*"
                r"([a-zA-Z]+)"
            )
            
            match = HEADER_RE.search(header_text)
            if match:
                date_str, time_str, tournament, raw_round, surface = match.groups()
                
                # ★ NORMALIZACIÓN AQUÍ ★
                round_canonical, is_qualifying = normalize_round(raw_round)
                
                return {
                    "date_str": date_str,
                    "time_str": time_str,
                    "tournament": tournament.strip(),
                    "round_name": round_canonical,      # ← Valor canónico
                    "is_qualifying": is_qualifying,     # ← Booleano independiente
                    "surface": SURFACE_MAP.get(surface.lower().strip(), surface.lower().strip()),
                }
    
    raise ValueError(f"No se encontró la línea de cabecera. Texto: '{header_text}'")

# Palabras que NUNCA forman parte del nombre de un jugador.
_NOISE_WORDS = {
    "hard", "clay", "grass", "indoors", "indoor", "carpet",
    "surface", "round", "final", "semifinal", "quarterfinal",
    "qualification", "challenger", "futures",
}


def _clean_player_name(raw: str) -> Optional[str]:
    """
    Limpia un nombre extraído por regex: quita superficies, guiones,
    puntos sueltos y palabras de ruido que TennisExplorer a veces mezcla.
    """
    if not raw:
        return None
    # Quitar superficies y palabras de ruido (case-insensitive, como palabras completas)
    cleaned = raw
    for word in _NOISE_WORDS:
        cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned, flags=re.IGNORECASE)
    # Quitar puntos, guiones y dígitos sueltos
    cleaned = re.sub(r"[\d\.\-\_]+", " ", cleaned)
    # Normalizar espacios
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Descartar si quedó vacío o muy corto (ruido)
    if not cleaned or len(cleaned) < 3:
        return None
    return cleaned


def parse_full_names(page_text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extrae nombres completos del marcador. La regex es permisiva y luego
    _clean_player_name elimina el ruido típico (superficies, guiones, etc.).
    """
    match = re.search(
        r"([A-Za-zÀ-ÿ'\s\.\-]+?)"
        r"\s+\d+\s*:\s*\d+\s*\([^)]*\)\s*"
        r"([A-Za-zÀ-ÿ'\s\.\-]+?)"
        r"(?=\s{2,}|Singles ranking|Birthdate|$)",
        page_text,
    )
    if not match:
        return None, None

    raw_a, raw_b = match.group(1), match.group(2)
    return _clean_player_name(raw_a), _clean_player_name(raw_b)

def parse_player_photos(html: str) -> tuple[str | None, str | None]:
    """
    Extrae las URLs de las fotos de los jugadores desde las imágenes de avatar.
    TennisExplorer usa: <img src="/res/img/player/XXX.jpeg" alt="Avatar">
    """
    soup = BeautifulSoup(html, "html.parser")
    
    # Buscar la tabla de resultados del partido
    result_table = soup.select_one("table.gDetail")
    if not result_table:
        return None, None
    
    # Las fotos están en las celdas con clase "thumb"
    thumbs = result_table.select("td.thumb img")
    
    photo_a = None
    photo_b = None
    
    if len(thumbs) >= 1:
        src_a = thumbs[0].get("src")
        if src_a:
            photo_a = f"https://www.tennisexplorer.com{src_a}"
    
    if len(thumbs) >= 2:
        src_b = thumbs[1].get("src")
        if src_b:
            photo_b = f"https://www.tennisexplorer.com{src_b}"
    
    return photo_a, photo_b


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
    """
    Limpia el valor y devuelve None si está vacío, es un guion o 'nan'.
    Usa rstrip('.') para manejar casos como '-.' que usa TennisExplorer.
    """
    if value is None:
        return None
    value = str(value).strip().rstrip(".")
    return None if value in ("-", "", "nan") else value


def parse_ranking(raw) -> int | None:
    """Convierte el ranking a entero, manejando valores nulos o inválidos."""
    value = _clean_or_none(raw)
    if value is None:
        return None
    return int(value)  # Ya viene limpio de puntos gracias a _clean_or_none


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


def parse_year(raw) -> int | None:
    """Convierte año a integer, manejando None y strings vacíos."""
    value = _clean_or_none(raw)
    if value is None:
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
        "turned_pro": parse_year(bio.get("Turned pro")),  # ← Ahora devuelve int | None
    }

def parse_match_detail(match_id: int, html: str) -> MatchDetail:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)

    detail = MatchDetail(match_id=match_id)
    detail.__dict__.update(parse_header(soup))  # ← Ahora pasa soup
    detail.closing_odds_a, detail.closing_odds_b = parse_closing_odds(page_text)
    detail.player_a_bio, detail.player_b_bio = parse_player_bio_tables(html)
    detail.player_a_full_name, detail.player_b_full_name = parse_full_names(page_text)
    detail.player_a_photo, detail.player_b_photo = parse_player_photos(html)

    return detail


if __name__ == "__main__":
    for test_id in (3307389, 3303261):  # partido normal + partido de qualy
        html = fetch_match_detail_html(test_id)
        result = parse_match_detail(test_id, html)
        print(result)
        print()