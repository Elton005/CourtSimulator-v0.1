"""
scraper/sources/tennisexplorer/results.py

Extrae la lista de partidos de un día desde /results/?type=all&year=Y&month=M&day=D.
Esta es la FASE 1 (descubrimiento): solo trae IDs de partido, jugadores, cuotas
"preview" y marcador -- el detalle real (ronda, superficie, cuotas de cierre
promedio, bio) se completa después llamando a match_detail.py para cada ID.

Filtra a singles bajo /atp-men/ (excluye dobles y WTA, según el scope acordado
de ATP + Challenger + Futures). El nivel (challenger/futures/main) se infiere
del nombre del torneo -- es una clasificación preliminar; la fuente de verdad
sigue siendo match-detail.
"""

import re
import time
import logging
from dataclasses import dataclass, field
from datetime import date

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://www.tennisexplorer.com"
REQUEST_DELAY_SECONDS = 4
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


@dataclass
class ResultRow:
    match_id: str
    tournament_name: str
    tournament_slug: str | None
    level_guess: str  # 'challenger' | 'futures' | 'main' -- preliminar, confirmar con match-detail
    time_str: str
    player_a_name: str
    player_a_slug: str
    player_b_name: str
    player_b_slug: str
    sets_a: list[dict] = field(default_factory=list)
    sets_b: list[dict] = field(default_factory=list)
    preview_odds_a: float | None = None
    preview_odds_b: float | None = None


def build_results_url(target_date: date, match_type: str = "all") -> str:
    return (
        f"{BASE_URL}/results/"
        f"?type={match_type}&year={target_date.year}"
        f"&month={target_date.month:02d}&day={target_date.day:02d}"
    )


def fetch_results_html(target_date: date, match_type: str = "all") -> str:
    url = build_results_url(target_date, match_type)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def slug_from_href(href: str) -> str:
    """/player/khachanov/ -> khachanov ; /player/berrettini-707ad/ -> berrettini-707ad"""
    return href.strip("/").split("/")[-1]


def guess_level(tournament_name: str) -> str:
    name_lower = tournament_name.lower()
    if "challenger" in name_lower:
        return "challenger"
    if "futures" in name_lower:
        return "futures"
    if "utr" in name_lower:
        return "utr"  # nivel bajo, sin confirmar si entra en scope -- revisar con el equipo
    return "main"


def parse_score_cell(td) -> dict:
    """
    Separa el marcador principal del tiebreak (que vive en un <sup> aparte).
    ej. '6' + <sup>5</sup> -> {'games': 6, 'tiebreak': 5}
        '6' sin sup         -> {'games': 6, 'tiebreak': None}
        '&nbsp;' (vacío)    -> None
    """
    sup = td.find("sup")
    tiebreak = int(sup.get_text(strip=True)) if sup and sup.get_text(strip=True) else None
    direct_text = "".join(td.find_all(string=True, recursive=False)).strip()
    if not direct_text:
        return None
    return {"games": int(direct_text), "tiebreak": tiebreak}


def parse_results_page(html: str) -> list[ResultRow]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.result")
    if table is None:
        raise ValueError("No se encontró table.result en la página")

    rows: list[ResultRow] = []
    current_tournament_name = None
    current_tournament_slug = None
    current_is_doubles = False
    current_is_men = False

    pending = None  # datos de la primera fila del partido en curso

    for tr in table.find_all("tr"):
        classes = tr.get("class") or []

        if "head" in classes and "flags" in classes:
            t_link = tr.select_one("td.t-name a")
            if t_link:
                current_tournament_name = t_link.get_text(strip=True)
                current_tournament_slug = t_link["href"].strip("/").split("/")[0]
            else:
                current_tournament_name = tr.select_one("td.t-name").get_text(strip=True)
                current_tournament_slug = None

            type_span = tr.select_one('td.t-name span[class*="type-"]')
            type_class = type_span["class"][0] if type_span and type_span.get("class") else ""
            # type-men2/type-women2 = singles, type-men4/type-women4 = dobles
            current_is_doubles = type_class.endswith("4")
            current_is_men = type_class.startswith("type-men")
            pending = None
            continue

        first_time_td = tr.select_one("td.first.time")

        if first_time_td is not None:
            name_td = tr.select_one("td.t-name")
            link = name_td.find("a") if name_td else None
            if link is None or current_is_doubles:
                pending = None
                continue

            info_link = tr.find("a", href=re.compile(r"match-detail"))
            match_id_match = re.search(r"id=(\d+)", info_link["href"]) if info_link else None

            coursew = tr.select_one("td.coursew")
            course = tr.select_one("td.course")

            pending = {
                "time_str": first_time_td.get_text(strip=True),
                "match_id": match_id_match.group(1) if match_id_match else None,
                "player_a_name": link.get_text(strip=True),
                "player_a_slug": slug_from_href(link["href"]),
                "sets_a": [c for c in (parse_score_cell(td) for td in tr.select("td.score")) if c is not None],
                "odds_a": float(coursew.get_text(strip=True)) if coursew and coursew.get_text(strip=True) else None,
                "odds_b": float(course.get_text(strip=True)) if course and course.get_text(strip=True) else None,
            }
            continue

        if pending is not None and not current_is_doubles:
            name_td = tr.select_one("td.t-name")
            link = name_td.find("a") if name_td else None
            if link is None:
                pending = None
                continue

            sets_b = [c for c in (parse_score_cell(td) for td in tr.select("td.score")) if c is not None]

            level = guess_level(current_tournament_name)
            in_scope = level != "utr"  # UTR Pro Tennis Series queda fuera del scope

            if pending["match_id"] and current_is_men and not current_is_doubles and in_scope:
                rows.append(ResultRow(
                    match_id=pending["match_id"],
                    tournament_name=current_tournament_name,
                    tournament_slug=current_tournament_slug,
                    level_guess=level,
                    time_str=pending["time_str"],
                    player_a_name=pending["player_a_name"],
                    player_a_slug=pending["player_a_slug"],
                    player_b_name=link.get_text(strip=True),
                    player_b_slug=slug_from_href(link["href"]),
                    sets_a=pending["sets_a"],
                    sets_b=sets_b,
                    preview_odds_a=pending["odds_a"],
                    preview_odds_b=pending["odds_b"],
                ))
            pending = None

    return rows


def run(target_date: date, match_type: str = "all") -> list[ResultRow]:
    html = fetch_results_html(target_date, match_type)
    return parse_results_page(html)


if __name__ == "__main__":
    resultados = run(date(2026, 8, 31))
    log.info("Encontrados %d partidos de singles ATP", len(resultados))
    for r in resultados[:5]:
        print(r)