"""
scraper/upcoming.py
Scrapea los partidos programados desde /matches/
"""
import re
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

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
    scheduled_time: str
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


def parse_upcoming_matches(html: str, target_date: date) -> List[UpcomingMatch]:
    """Parsea el HTML y extrae los partidos que AÚN no tienen resultado."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    
    # Buscar todas las tablas de resultados
    tables = soup.select("table.result")
    
    for table in tables:
        current_tournament = None
        current_tournament_slug = None
        current_tournament_year = None
        current_surface = None
        
        rows = table.find_all("tr")
        
        for row in rows:
            row_classes = row.get("class", [])
            
            # Fila de cabecera del torneo
            if "head" in row_classes:
                tournament_cell = row.find("td", class_="t-name")
                if tournament_cell:
                    link = tournament_cell.find("a")
                    if link:
                        current_tournament = link.get_text(strip=True)
                        href = link.get("href", "")
                        parts = href.strip("/").split("/")
                        if len(parts) >= 2:
                            current_tournament_slug = parts[0]
                            try:
                                current_tournament_year = int(parts[1])
                            except ValueError:
                                current_tournament_year = target_date.year
                        
                        # Detectar superficie del texto de la cabecera
                        head_text = row.get_text(" ", strip=True).lower()
                        if "hard" in head_text:
                            current_surface = "hard"
                        elif "clay" in head_text:
                            current_surface = "clay"
                        elif "grass" in head_text:
                            current_surface = "grass"
                        elif "indoor" in head_text or "indoors" in head_text:
                            current_surface = "indoor"
                continue
            
            # Fila de partido (tiene id tipo "r10", "r1325", etc.)
            row_id = row.get("id", "")
            if not row_id or not row_id.startswith("r"):
                continue
            
            try:
                # VERIFICACIÓN CLAVE: ¿Es un partido upcoming?
                # Los partidos upcoming tienen celdas con clase "nbr" vacías o con &nbsp;
                # Los partidos finalizados tienen celdas con clase "result" con números
                
                result_cells = row.find_all("td", class_="result")
                nbr_cells = row.find_all("td", class_="nbr")
                
                # Si tiene celdas "result" con contenido numérico, es un partido finalizado
                is_finished = False
                for cell in result_cells:
                    text = cell.get_text(strip=True)
                    if text and text.isdigit():
                        is_finished = True
                        break
                
                if is_finished:
                    continue  # Saltar partidos finalizados
                
                # Extraer hora
                time_cell = row.find("td", class_="time")
                if not time_cell:
                    continue
                
                time_text = time_cell.get_text(strip=True).split("\n")[0].strip()
                
                # Si la hora es "--:--" o está vacía, saltamos
                if time_text == "--:--" or not time_text or not re.match(r"\d{2}:\d{2}", time_text):
                    continue
                
                # Extraer jugadores
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
                
                # Extraer match_id del enlace al detalle
                info_link = row.find("a", title="Click for match detail")
                if not info_link:
                    continue
                
                match_href = info_link.get("href", "")
                match_id_match = re.search(r'id=(\d+)', match_href)
                if not match_id_match:
                    continue
                match_id = int(match_id_match.group(1))
                
                # Extraer cuotas (clase "course")
                odds_a = None
                odds_b = None
                course_cells = row.find_all("td", class_="course")
                if len(course_cells) >= 2:
                    try:
                        odds_a_text = course_cells[0].get_text(strip=True)
                        odds_b_text = course_cells[1].get_text(strip=True)
                        if odds_a_text and odds_a_text != "" and odds_a_text != "&nbsp;":
                            odds_a = float(odds_a_text)
                        if odds_b_text and odds_b_text != "" and odds_b_text != "&nbsp;":
                            odds_b = float(odds_b_text)
                    except ValueError:
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
                    surface=current_surface or "unknown",
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
    matches = run(days_ahead=1)
    for m in matches[:5]:
        print(f"{m.match_date} {m.scheduled_time} | {m.player_a_name} vs {m.player_b_name} | {m.tournament_name}")