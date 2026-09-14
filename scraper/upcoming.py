"""
scraper/upcoming.py
Scrapea los partidos programados (upcoming) desde /matches/ y /next/
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
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
    odds_a: Optional[float] = None
    odds_b: Optional[float] = None
    match_date: Optional[date] = None


def fetch_upcoming_html(target_date: date) -> str:
    """Obtiene el HTML de hoy o de un día futuro específico."""
    if target_date == date.today():
        url = "https://www.tennisexplorer.com/matches/?type=atp-single"
    else:
        url = f"https://www.tennisexplorer.com/next/?type=atp-single&year={target_date.year}&month={target_date.month:02d}&day={target_date.day:02d}"
    
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_upcoming_matches(html: str, target_date: date) -> List[UpcomingMatch]:
    """Parsea el HTML extrayendo solo los partidos que aún no tienen resultado."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    
    current_tournament_name = "Unknown"
    current_tournament_slug = "unknown"
    current_tournament_year = target_date.year
    
    # Buscamos todas las filas que representan el inicio de un partido (id="r10", "r1325", etc., pero NO "r10b")
    match_rows = soup.find_all("tr", id=re.compile(r"^r\d+$"))
    
    for row in match_rows:
        # 1. Actualizar el contexto del torneo (buscando hacia atrás el encabezado más cercano)
        prev_head = row.find_previous_sibling("tr", class_=re.compile(r"head"))
        if prev_head:
            tour_link = prev_head.find("a", href=True)
            if tour_link:
                current_tournament_name = tour_link.get_text(strip=True)
                href = tour_link["href"]
                parts = href.strip("/").split("/")
                if len(parts) >= 2:
                    current_tournament_slug = parts[0]
                    try:
                        current_tournament_year = int(parts[1])
                    except ValueError:
                        current_tournament_year = target_date.year
        
        # 2. VERIFICACIÓN CLAVE: ¿Es un partido próximo?
        # Los partidos próximos tienen <td class="nbr">&nbsp;</td> o están vacíos.
        # Los finalizados tienen <td class="result">2</td>
        result_cell = row.find("td", class_=re.compile(r"result|nbr"))
        if result_cell:
            cell_text = result_cell.get_text(strip=True)
            if cell_text not in ["", "&nbsp;"]:
                continue  # Tiene resultado, es un partido finalizado o en vivo. Lo saltamos.
        
        # 3. Extraer la hora
        time_cell = row.find("td", class_="time")
        if not time_cell:
            continue
        
        time_match = re.search(r"(\d{2}:\d{2})", time_cell.get_text())
        if not time_match:
            continue  # Si la hora es "--:--" o no tiene formato, lo saltamos
        scheduled_time = time_match.group(1)
        
        # 4. Extraer jugadores
        player_cells = row.find_all("td", class_="t-name")
        if len(player_cells) < 2:
            continue
        
        player_a_link = player_cells[0].find("a", href=True)
        player_b_link = player_cells[1].find("a", href=True)
        
        if not player_a_link or not player_b_link:
            continue
            
        player_a_name = player_a_link.get_text(strip=True)
        player_b_name = player_b_link.get_text(strip=True)
        
        player_a_slug = player_a_link["href"].strip("/").split("/")[-1]
        player_b_slug = player_b_link["href"].strip("/").split("/")[-1]
        
        # 5. Extraer Match ID (del enlace "info")
        info_link = row.find("a", href=re.compile(r"/match-detail/\?id=\d+"))
        if not info_link:
            continue
        
        match_id_match = re.search(r"id=(\d+)", info_link["href"])
        if not match_id_match:
            continue
        match_id = int(match_id_match.group(1))
        
        # 6. Extraer Cuotas (Opcional, pero útil)
        odds_a, odds_b = None, None
        course_cells = row.find_all("td", class_="course")
        if len(course_cells) >= 2:
            try:
                odds_a_text = course_cells[0].get_text(strip=True)
                odds_b_text = course_cells[1].get_text(strip=True)
                if odds_a_text and odds_a_text != "&nbsp;":
                    odds_a = float(odds_a_text)
                if odds_b_text and odds_b_text != "&nbsp;":
                    odds_b = float(odds_b_text)
            except (ValueError, IndexError):
                pass
        
        matches.append(UpcomingMatch(
            match_id=match_id,
            player_a_name=player_a_name,
            player_b_name=player_b_name,
            player_a_slug=player_a_slug,
            player_b_slug=player_b_slug,
            tournament_name=current_tournament_name,
            tournament_slug=current_tournament_slug,
            tournament_year=current_tournament_year,
            scheduled_time=scheduled_time,
            odds_a=odds_a,
            odds_b=odds_b,
            match_date=target_date,
        ))
        
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
            
    log.info(f"✅ Total: {len(all_matches)} partidos upcoming encontrados")
    return all_matches


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matches = run(days_ahead=1)
    for m in matches[:5]:  # Imprime los primeros 5 para verificar
        print(f"[{m.match_date}] {m.scheduled_time} | {m.tournament_name} | {m.player_a_name} vs {m.player_b_name} (ID: {m.match_id})")