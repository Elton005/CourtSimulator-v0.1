"""
scraper/upcoming.py
Scrapea los partidos programados para HOY y MAÑANA desde /matches/
"""
import re
import logging
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, date, timedelta

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

BASE_URL = "https://www.tennisexplorer.com/matches/"


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
    scheduled_time: str  # "19:00"
    round_name: str
    surface: str
    odds_a: Optional[float] = None
    odds_b: Optional[float] = None
    match_date: Optional[date] = None


def fetch_upcoming_html(target_date: date) -> str:
    """Descarga la página de partidos para una fecha específica."""
    params = {
        "type": "atp-single",
        "year": target_date.year,
        "month": f"{target_date.month:02d}",
        "day": f"{target_date.day:02d}"
    }
    resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def _parse_slug_from_href(href: str) -> str:
    if not href:
        return "unknown"
    parts = href.strip("/").split("/")
    return parts[0] if parts else "unknown"


def _parse_year_from_href(href: str) -> Optional[int]:
    if not href:
        return None
    parts = href.strip("/").split("/")
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def _parse_player_slug(href: str) -> str:
    if not href:
        return ""
    match = re.search(r'/player/([^/]+)/?', href)
    return match.group(1) if match else ""


def parse_upcoming_matches(html: str, target_date: date) -> List[UpcomingMatch]:
    """Parsea el HTML y extrae solo los partidos que AÚN no tienen resultado."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    
    tables = soup.select("table.result")
    
    for table in tables:
        current_tournament = None
        current_tournament_slug = None
        current_tournament_year = None
        current_surface = "unknown"
        
        rows = table.find_all("tr")
        
        for row in rows:
            row_classes = row.get("class", [])
            
            # 1. Detectar cabecera de torneo
            if "head" in row_classes:
                tournament_link = row.find("a", href=True)
                if tournament_link:
                    href = tournament_link.get("href", "")
                    current_tournament = tournament_link.get_text(strip=True)
                    current_tournament_slug = _parse_slug_from_href(href)
                    current_tournament_year = _parse_year_from_href(href)
                
                # Detectar superficie en el texto de la cabecera
                row_text = row.get_text(" ", strip=True).lower()
                if "hard" in row_text: current_surface = "hard"
                elif "clay" in row_text: current_surface = "clay"
                elif "grass" in row_text: current_surface = "grass"
                elif "indoor" in row_text: current_surface = "indoor"
                continue
            
            # 2. Detectar fila de partido (id tipo "r10", "s10", etc.)
            row_id = row.get("id", "")
            if not row_id or not re.match(r"^[rs]\d+[a-z]?$", row_id):
                continue
            
            try:
                # Verificar si YA tiene resultado (celdas con números en la clase 'result' o 'score')
                has_result = False
                for cell in row.find_all(["td", "th"]):
                    if "result" in cell.get("class", []) or "score" in cell.get("class", []):
                        if cell.get_text(strip=True).isdigit():
                            has_result = True
                            break
                
                if has_result:
                    continue  # Saltar partidos ya finalizados
                
                # Extraer hora
                time_cell = row.find("td", class_="time")
                if not time_cell:
                    continue
                
                time_text = time_cell.get_text(strip=True).split("\n")[0].strip()
                if time_text == "--:--" or not re.match(r"\d{2}:\d{2}", time_text):
                    continue
                
                # Extraer jugadores
                player_links = row.find_all("a", href=re.compile(r'/player/'))
                if len(player_links) < 2:
                    continue
                
                player_a_name = player_links[0].get_text(strip=True)
                player_b_name = player_links[1].get_text(strip=True)
                player_a_slug = _parse_player_slug(player_links[0].get("href", ""))
                player_b_slug = _parse_player_slug(player_links[1].get("href", ""))
                
                # Extraer match_id
                info_link = row.find("a", href=re.compile(r'/match-detail/'))
                if not info_link:
                    continue
                
                match_href = info_link.get("href", "")
                match_id_match = re.search(r'id=(\d+)', match_href)
                if not match_id_match:
                    continue
                match_id = int(match_id_match.group(1))
                
                # Extraer cuotas (clase 'course')
                odds_a, odds_b = None, None
                course_cells = row.find_all("td", class_="course")
                if len(course_cells) >= 2:
                    try:
                        odds_a_text = course_cells[0].get_text(strip=True)
                        odds_b_text = course_cells[1].get_text(strip=True)
                        if odds_a_text and odds_a_text != "":
                            odds_a = float(odds_a_text)
                        if odds_b_text and odds_b_text != "":
                            odds_b = float(odds_b_text)
                    except (ValueError, IndexError):
                        pass
                
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
                    tournament_year=current_tournament_year or target_date.year,
                    scheduled_time=time_text,
                    round_name="Unknown",
                    surface=current_surface,
                    odds_a=odds_a,
                    odds_b=odds_b,
                    match_date=target_date,
                ))
                
            except Exception as e:
                log.debug(f"Error parseando fila {row_id}: {e}")
                continue
    
    return matches


def run(days_ahead: int = 1) -> List[UpcomingMatch]:
    """
    Descarga y parsea los partidos upcoming.
    days_ahead=1 significa: HOY (0) y MAÑANA (1).
    """
    all_matches = []
    
    for day_offset in range(days_ahead + 1):
        target_date = date.today() + timedelta(days=day_offset)
        log.info(f"🔍 Scrapeando partidos para {target_date} (día +{day_offset})")
        
        try:
            html = fetch_upcoming_html(target_date)
            matches = parse_upcoming_matches(html, target_date)
            log.info(f"  ✅ Encontrados {len(matches)} partidos para {target_date}")
            all_matches.extend(matches)
        except Exception as e:
            log.error(f"  ❌ Error scrapeando {target_date}: {e}")
            continue
    
    log.info(f"✅ Total: {len(all_matches)} partidos upcoming encontrados")
    return all_matches


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Prueba local: solo hoy y mañana
    matches = run(days_ahead=1)
    for m in matches[:5]:
        print(f"{m.match_date} {m.scheduled_time} | {m.player_a_name} vs {m.player_b_name} | {m.tournament_name}")