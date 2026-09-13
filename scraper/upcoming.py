"""
scraper/upcoming.py
Scrapea los partidos programados para hoy desde /matches/
Devuelve una lista de partidos con: match_id, jugadores, torneo, hora, cuotas
"""
import re
import logging
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

URL = "https://www.tennisexplorer.com/matches/?type=atp-single"


@dataclass
class UpcomingMatch:
    match_id: int
    player_a_name: str
    player_b_name: str
    player_a_slug: str
    player_b_slug: str
    tournament_name: str
    tournament_slug: str
    tournament_year: int
    scheduled_time: str  # "19:00" o "20:30"
    round_name: str
    surface: str
    odds_a: Optional[float] = None
    odds_b: Optional[float] = None


def fetch_upcoming_html() -> str:
    """Descarga la página de partidos de hoy."""
    resp = requests.get(URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def _parse_slug_from_href(href: str) -> str:
    """Extrae el slug de una URL como '/us-open/2026/atp-men/' → 'us-open'"""
    if not href:
        return "unknown"
    parts = href.strip("/").split("/")
    return parts[0] if parts else "unknown"


def _parse_year_from_href(href: str) -> Optional[int]:
    """Extrae el año de una URL como '/us-open/2026/atp-men/' → 2026"""
    if not href:
        return None
    parts = href.strip("/").split("/")
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def parse_upcoming_matches(html: str) -> List[UpcomingMatch]:
    """
    Parsea el HTML de /matches/ y extrae los partidos programados.
    Solo devuelve partidos que AÚN no tienen resultado (scheduled).
    """
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    
    # Buscamos todas las tablas de resultados
    tables = soup.select("table.result")
    
    current_tournament = None
    current_tournament_slug = None
    current_tournament_year = None
    current_round = None
    current_surface = None
    
    for table in tables:
        rows = table.find_all("tr")
        
        for row in rows:
            # Fila de cabecera del torneo
            if row.get("class") and "head" in row.get("class"):
                tournament_cell = row.find("td", class_="t-name")
                if tournament_cell:
                    link = tournament_cell.find("a")
                    if link:
                        current_tournament = link.get_text(strip=True)
                        href = link.get("href", "")
                        current_tournament_slug = _parse_slug_from_href(href)
                        current_tournament_year = _parse_year_from_href(href)
                    
                    # Extraer ronda y superficie del texto de la cabecera
                    head_text = row.get_text(" ", strip=True)
                    
                    # Buscar superficie
                    surface_match = re.search(r"\b(hard|clay|grass|indoors|indoor|carpet)\b", head_text, re.IGNORECASE)
                    current_surface = surface_match.group(1).lower() if surface_match else "unknown"
                continue
            
            # Fila de partido (tiene id tipo "r10", "r11", etc.)
            row_id = row.get("id", "")
            if not row_id.startswith("r") or row_id.endswith("b"):
                continue
            
            try:
                # Hora programada
                time_cell = row.find("td", class_="time")
                if not time_cell:
                    continue
                time_text = time_cell.get_text(strip=True).split("\n")[0].strip()
                
                # Si la hora es "--:--", es un partido sin hora definida (lo saltamos)
                if time_text == "--:--" or not re.match(r"\d{2}:\d{2}", time_text):
                    continue
                
                # Jugadores
                player_cells = row.find_all("td", class_="t-name")
                if len(player_cells) < 2:
                    continue
                
                player_a_link = player_cells[0].find("a")
                player_b_link = player_cells[1].find("a")
                
                if not player_a_link or not player_b_link:
                    continue
                
                player_a_name = player_a_link.get_text(strip=True)
                player_b_name = player_b_link.get_text(strip=True)
                
                # Extraer slug del href del jugador
                player_a_href = player_a_link.get("href", "")
                player_b_href = player_b_link.get("href", "")
                player_a_slug = player_a_href.strip("/").split("/")[-1] if player_a_href else ""
                player_b_slug = player_b_href.strip("/").split("/")[-1] if player_b_href else ""
                
                # Verificar si el partido YA tiene resultado (no es upcoming)
                result_cell = player_cells[0].find("td", class_="result") or row.find("td", class_="result")
                # Si hay un número en el resultado, el partido ya se jugó
                has_result = False
                for cell in row.find_all("td", class_="result"):
                    text = cell.get_text(strip=True)
                    if text and text.isdigit():
                        has_result = True
                        break
                
                if has_result:
                    continue  # Saltamos partidos ya finalizados
                
                # Enlace al match-detail
                info_link = row.find("a", title="Click for match detail")
                if not info_link:
                    continue
                match_href = info_link.get("href", "")
                match_id_match = re.search(r"id=(\d+)", match_href)
                if not match_id_match:
                    continue
                match_id = int(match_id_match.group(1))
                
                # Cuotas (si existen)
                odds_a = None
                odds_b = None
                course_cells = row.find_all("td", class_="course")
                if len(course_cells) >= 2:
                    try:
                        odds_a_text = course_cells[0].get_text(strip=True)
                        odds_b_text = course_cells[1].get_text(strip=True)
                        if odds_a_text and odds_a_text != "":
                            odds_a = float(odds_a_text)
                        if odds_b_text and odds_b_text != "":
                            odds_b = float(odds_b_text)
                    except ValueError:
                        pass
                
                # Ronda (la inferimos del contexto o la dejamos vacía)
                round_name = "Unknown"
                
                if not current_tournament:
                    continue
                
                matches.append(UpcomingMatch(
                    match_id=match_id,
                    player_a_name=player_a_name,
                    player_b_name=player_b_name,
                    player_a_slug=player_a_slug,
                    player_b_slug=player_b_slug,
                    tournament_name=current_tournament,
                    tournament_slug=current_tournament_slug or "unknown",
                    tournament_year=current_tournament_year or date.today().year,
                    scheduled_time=time_text,
                    round_name=round_name,
                    surface=current_surface or "unknown",
                    odds_a=odds_a,
                    odds_b=odds_b,
                ))
                
            except Exception as e:
                log.debug(f"Error parseando fila: {e}")
                continue
    
    return matches


def run(target_date: Optional[date] = None) -> List[UpcomingMatch]:
    """Función principal: descarga y parsea los partidos upcoming."""
    log.info(f"🔍 Scrapeando partidos upcoming desde {URL}")
    html = fetch_upcoming_html()
    matches = parse_upcoming_matches(html)
    log.info(f"✅ Encontrados {len(matches)} partidos upcoming")
    return matches


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matches = run()
    for m in matches[:10]:
        print(f"{m.scheduled_time} | {m.player_a_name} vs {m.player_b_name} | {m.tournament_name} | {m.odds_a}/{m.odds_b}")