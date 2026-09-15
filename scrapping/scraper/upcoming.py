"""
scraper/upcoming.py
Scrapea los partidos programados (upcoming) desde /matches/
"""
import re
import logging
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


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


def _parse_player_slug(href: str) -> str:
    """Extrae el slug del jugador de '/player/alcaraz-c/' → 'alcaraz-c'"""
    if not href:
        return ""
    match = re.search(r'/player/([^/]+)/?', href)
    return match.group(1) if match else ""


def fetch_upcoming_html(target_date: date) -> str:
    """
    Descarga la página de partidos para una fecha específica.
    CORRECCIÓN: Usar SIEMPRE los parámetros de fecha para evitar problemas 
    de caché o zona horaria del servidor.
    """
    url = (
        f"https://www.tennisexplorer.com/matches/"
        f"?type=atp-single"
        f"&year={target_date.year}"
        f"&month={target_date.month:02d}"
        f"&day={target_date.day:02d}"
    )
    
    log.debug(f"Descargando: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_upcoming_matches(html: str, target_date: date) -> List[UpcomingMatch]:
    """
    Parsea el HTML de /matches/ y extrae los partidos que AÚN no tienen resultado.
    CORRECCIÓN: Ahora busca al Jugador B en la fila "hermana" (ej: r10b).
    """
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    
    tables = soup.select("table.result")
    
    for table in tables:
        current_tournament = None
        current_tournament_slug = None
        current_tournament_year = None
        current_surface = None
        
        rows = table.find_all("tr")
        
        for row in rows:
            row_classes = row.get("class", [])
            
            # 1. Fila de cabecera del torneo
            if "head" in row_classes:
                tournament_link = row.find("a", href=True)
                if tournament_link:
                    current_tournament = tournament_link.get_text(strip=True)
                    href = tournament_link.get("href", "")
                    current_tournament_slug = _parse_slug_from_href(href)
                    current_tournament_year = _parse_year_from_href(href)
                
                # Detectar superficie del texto de la cabecera
                row_text = row.get_text(" ", strip=True).lower()
                if "hard" in row_text:
                    current_surface = "hard"
                elif "clay" in row_text:
                    current_surface = "clay"
                elif "grass" in row_text:
                    current_surface = "grass"
                elif "indoor" in row_text or "indoors" in row_text:
                    current_surface = "indoor"
                continue
            
            # 2. Fila de partido (tiene id tipo "r10", "r11", etc.)
            row_id = row.get("id", "")
            if not row_id or not row_id.startswith("r"):
                continue
            
            # 3. Verificar si el partido YA tiene resultado
            # Los partidos upcoming tienen celdas con clase "nbr" vacías
            # Los partidos finalizados tienen celdas con clase "result" con números
            has_result = False
            for cell in row.find_all("td"):
                if "result" in cell.get("class", []):
                    text = cell.get_text(strip=True)
                    if text and text.isdigit():
                        has_result = True
                        break
            
            if has_result:
                continue  # Saltar partidos ya finalizados
            
            # 4. Extraer hora
            time_cell = row.find("td", class_="time")
            if not time_cell:
                continue
            
            time_text = time_cell.get_text(strip=True).split("\n")[0].strip()
            
            # Si la hora es "--:--" o no tiene formato, saltamos
            if time_text == "--:--" or not time_text or not re.match(r"\d{2}:\d{2}", time_text):
                continue
            
            # 5. Extraer Jugador A (de la fila principal)
            player_a_cell = row.find("td", class_="t-name")
            if not player_a_cell:
                continue
            
            player_a_link = player_a_cell.find("a", href=True)
            if not player_a_link:
                continue
            
            player_a_name = player_a_link.get_text(strip=True)
            player_a_slug = _parse_player_slug(player_a_link.get("href", ""))
            
            # 6. Extraer Jugador B (de la fila compañera "b", ej: "r10b")
            buddy_row = table.find("tr", id=f"{row_id}b")
            if not buddy_row:
                continue
            
            player_b_cell = buddy_row.find("td", class_="t-name")
            if not player_b_cell:
                continue
            
            player_b_link = player_b_cell.find("a", href=True)
            if not player_b_link:
                continue
            
            player_b_name = player_b_link.get_text(strip=True)
            player_b_slug = _parse_player_slug(player_b_link.get("href", ""))
            
            # 7. Extraer match_id (puede estar en la fila principal o la compañera)
            match_id = None
            info_link = row.find("a", href=re.compile(r'/match-detail/\?id=\d+'))
            if not info_link:
                info_link = buddy_row.find("a", href=re.compile(r'/match-detail/\?id=\d+'))
            
            if info_link:
                match_id_match = re.search(r'id=(\d+)', info_link.get("href", ""))
                if match_id_match:
                    match_id = int(match_id_match.group(1))
            
            if not match_id:
                continue
            
            # 8. Extraer cuotas (de la fila principal)
            odds_a, odds_b = None, None
            course_cells = row.find_all("td", class_="course")
            if len(course_cells) >= 2:
                try:
                    odds_a_text = course_cells[0].get_text(strip=True)
                    odds_b_text = course_cells[1].get_text(strip=True)
                    if odds_a_text and odds_a_text not in ("", "&nbsp;"):
                        odds_a = float(odds_a_text)
                    if odds_b_text and odds_b_text not in ("", "&nbsp;"):
                        odds_b = float(odds_b_text)
                except ValueError:
                    pass
            
            # 9. Construir el objeto
            matches.append(UpcomingMatch(
                match_id=match_id,
                player_a_name=player_a_name,
                player_b_name=player_b_name,
                player_a_slug=player_a_slug,
                player_b_slug=player_b_slug,
                tournament_name=current_tournament or "Unknown",
                tournament_slug=current_tournament_slug or "unknown",
                tournament_year=current_tournament_year or target_date.year,
                scheduled_time=time_text,
                round_name="Unknown",
                surface=current_surface or "unknown",
                odds_a=odds_a,
                odds_b=odds_b,
                match_date=target_date,
            ))
    
    return matches


def run(days_ahead: int = 1) -> List[UpcomingMatch]:
    """
    Función principal: descarga y parsea los partidos upcoming.
    """
    from datetime import timedelta
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
    logging.basicConfig(level=logging.DEBUG)
    matches = run(days_ahead=1)
    for m in matches[:5]:
        print(f"[{m.match_date}] {m.scheduled_time} | {m.tournament_name} | {m.player_a_name} vs {m.player_b_name}")