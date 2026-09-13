"""
pipeline_run.py — Centro de Comando de CourtSimulator (Producción)

Subcomandos:
    scrape      Ejecuta el scraping de uno o varios días (con checkpoint resumible).
    backfill    Enriquece perfiles de jugadores con datos de /player/{slug}/.
    status      Muestra el estado de las últimas corridas registradas.
"""

import os
import sys
import time
import random
import json
import logging
import argparse
import unicodedata
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# --- IMPORTS DEL SCRAPER ---
from scraper import results, match_detail
from scraper.rounds import round_sort_key

# Importamos backfill_players si existe en la carpeta raíz
try:
    import backfill_players
    HAS_BACKFILL = True
except ImportError:
    backfill_players = None
    HAS_BACKFILL = False

# ---------------------------------------------------------------------------
# Configuración global y Logging
# ---------------------------------------------------------------------------
load_dotenv(override=True)

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "pipeline.log", mode="a", encoding="utf-8"),
    ]
)
log = logging.getLogger("court-sim")

error_handler = logging.FileHandler(LOG_DIR / "errors.log", mode="a", encoding="utf-8")
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
log.addHandler(error_handler)

MIN_DELAY = 1.0
MAX_DELAY = 1.5
CHECKPOINT_FILE = Path("backfill_checkpoint.json")
MAX_WORKERS = 4  # 4 hilos simultáneos (rápido y seguro)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def slugify_tournament(name: str) -> str:
    """Convierte un nombre de torneo en un slug limpio (ej: 'Quimper 2 challenger' -> 'quimper-2-challenger')"""
    if not name:
        return "unknown"
    s = unicodedata.normalize('NFKD', name)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = s.replace('-', ' ')
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.replace(' ', '-')
    s = re.sub(r'-+', '-', s)
    return s.strip('-')


def load_checkpoint() -> set[str]:
    if not CHECKPOINT_FILE.exists():
        return set()
    try:
        with CHECKPOINT_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return set()
        return set(data)
    except (json.JSONDecodeError, OSError):
        return set()


def save_checkpoint(done_days: set[str]) -> None:
    try:
        with CHECKPOINT_FILE.open("w", encoding="utf-8") as f:
            json.dump(sorted(done_days), f, indent=2)
    except OSError as e:
        log.error("No se pudo escribir el checkpoint: %s", e)


def fetch_with_backoff(fetch_fn, *args, max_retries: int = 5, **kwargs):
    delay = MIN_DELAY
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_fn(*args, **kwargs)
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            if status in (429, 403, 500, 502, 503, 504):
                wait = delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                log.warning("HTTP %s recibido, esperando %.1fs (intento %d/%d)", status, wait, attempt, max_retries)
                time.sleep(wait)
                continue
            raise
        except requests.RequestException as exc:
            last_exc = exc
            wait = delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log.warning("Error de red (%s), reintentando en %.1fs (%d/%d)", exc, wait, attempt, max_retries)
            time.sleep(wait)
    raise RuntimeError(f"Agotados los {max_retries} reintentos. Último error: {last_exc}")


def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("Falta la variable de entorno SUPABASE_DB_URL.")
    try:
        return psycopg2.connect(db_url)
    except psycopg2.Error as e:
        raise RuntimeError(f"No se pudo conectar a Supabase: {e}") from e


# ---------------------------------------------------------------------------
# Operaciones de base de datos
# ---------------------------------------------------------------------------
def get_or_create_player(cur, slug: str, name_guess: str, bio: dict, full_name: Optional[str], photo_url: Optional[str] = None) -> int:
    canonical_name = full_name or name_guess
    cur.execute("SELECT player_id FROM player_aliases WHERE source = %s AND alias_name = %s", ("tennisexplorer", slug))
    row = cur.fetchone()

    if row:
        player_id = row[0]
        cur.execute(
            """
            UPDATE players SET
                canonical_name  = CASE WHEN canonical_name IS NULL OR canonical_name = '' THEN %s ELSE canonical_name END,
                current_ranking = COALESCE(%s, current_ranking),
                birth_date      = COALESCE(%s, birth_date),
                height_cm       = COALESCE(%s, height_cm),
                weight_kg       = COALESCE(%s, weight_kg),
                plays           = COALESCE(%s, plays),
                turned_pro      = COALESCE(%s, turned_pro),
                photo_url       = COALESCE(%s, photo_url)
            WHERE id = %s
            """,
            (canonical_name, bio.get("current_ranking"), bio.get("birth_date"), bio.get("height_cm"),
             bio.get("weight_kg"), bio.get("plays"), bio.get("turned_pro"), photo_url, player_id),
        )
        return player_id

    cur.execute(
        """
        INSERT INTO players (canonical_name, current_ranking, birth_date, height_cm, weight_kg, plays, turned_pro, photo_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (canonical_name, bio.get("current_ranking"), bio.get("birth_date"), bio.get("height_cm"),
         bio.get("weight_kg"), bio.get("plays"), bio.get("turned_pro"), photo_url),
    )
    player_id = cur.fetchone()[0]
    cur.execute("INSERT INTO player_aliases (player_id, source, alias_name) VALUES (%s, %s, %s)", (player_id, "tennisexplorer", slug))
    return player_id


def get_or_create_tournament(cur, name: str, slug: Optional[str], year: Optional[int], surface: Optional[str], level: str) -> int:
    # Si no nos dieron slug, lo generamos
    if not slug:
        slug = slugify_tournament(name)

    # 1. Buscar por slug (ID General canónico)
    cur.execute("SELECT id FROM tournaments WHERE source_tournament_id = %s LIMIT 1", (slug,))
    row = cur.fetchone()
    if row:
        return row[0]

    # 2. Fallback: buscar por nombre y año (para compatibilidad con datos antiguos)
    if year is not None:
        cur.execute("SELECT id FROM tournaments WHERE name = %s AND EXTRACT(YEAR FROM start_date) = %s LIMIT 1", (name, year))
    else:
        cur.execute("SELECT id FROM tournaments WHERE name = %s AND start_date IS NULL LIMIT 1", (name,))
    
    row = cur.fetchone()
    if row:
        # Actualizar el slug del torneo existente para unificarlo de ahora en adelante
        cur.execute("UPDATE tournaments SET source_tournament_id = %s WHERE id = %s", (slug, row[0]))
        return row[0]

    # 3. Crear nuevo
    approx_start_date = date(year, 1, 1) if year else None
    cur.execute(
        """
        INSERT INTO tournaments (name, source_tournament_id, surface, level, start_date)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (name, slug, surface, level, approx_start_date),
    )
    return cur.fetchone()[0]


def upsert_match(cur, tournament_id: int, player_a_id: int, player_b_id: int, round_name: Optional[str], is_qualifying: bool, match_date: date, winner_id: Optional[int], sets: list[dict], source_match_id: str) -> int:
    safe_round = round_name or "UNKNOWN"
    round_order = round_sort_key(safe_round)

    cur.execute(
        """
        INSERT INTO matches (tournament_id, player_a_id, player_b_id, round, round_order, is_qualifying, match_date, status, winner_id, sets, source, source_match_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'finished', %s, %s, 'tennisexplorer', %s)
        ON CONFLICT (source, source_match_id) DO UPDATE SET
            tournament_id = EXCLUDED.tournament_id, player_a_id = EXCLUDED.player_a_id, player_b_id = EXCLUDED.player_b_id,
            round = EXCLUDED.round, round_order = EXCLUDED.round_order, is_qualifying = EXCLUDED.is_qualifying,
            match_date = EXCLUDED.match_date, winner_id = EXCLUDED.winner_id, sets = EXCLUDED.sets
        RETURNING id
        """,
        (tournament_id, player_a_id, player_b_id, safe_round, round_order, is_qualifying, match_date, winner_id, psycopg2.extras.Json(sets), str(source_match_id)),
    )
    return cur.fetchone()[0]


def upsert_closing_odds(cur, match_id: int, player_id: int, odds: Optional[float]) -> None:
    if odds is None or odds <= 1.0:
        return
    implied_probability = round(100.0 / odds, 2)
    cur.execute(
        """
        INSERT INTO closing_odds (match_id, player_id, odds, implied_probability)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (match_id, player_id) DO UPDATE SET
            odds = EXCLUDED.odds, implied_probability = EXCLUDED.implied_probability, captured_at = now()
        """,
        (match_id, player_id, odds, implied_probability),
    )


def sets_to_jsonb(sets_a: list[dict], sets_b: list[dict]) -> list[dict]:
    return [{"a": a["games"], "b": b["games"]} for a, b in zip(sets_a, sets_b)]


def decide_winner(player_a_id: int, player_b_id: int, sets_a: list[dict], sets_b: list[dict]) -> Optional[int]:
    if not sets_a or not sets_b or len(sets_a) != len(sets_b):
        return None
    won_a = sum(1 for a, b in zip(sets_a, sets_b) if a["games"] > b["games"])
    won_b = sum(1 for a, b in zip(sets_a, sets_b) if b["games"] > a["games"])
    if won_a > won_b: return player_a_id
    if won_b > won_a: return player_b_id
    return None


# ---------------------------------------------------------------------------
# Worker para Concurrencia (Solo red y parseo, SIN base de datos)
# ---------------------------------------------------------------------------
def fetch_and_parse_worker(row) -> dict:
    try:
        html = fetch_with_backoff(match_detail.fetch_match_detail_html, row.match_id)
        detail = match_detail.parse_match_detail(row.match_id, html)
        time.sleep(random.uniform(0.3, 0.8))  # Pequeña pausa amigable por hilo
        return {"row": row, "detail": detail, "error": None}
    except Exception as exc:
        return {"row": row, "detail": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Pipeline de un día
# ---------------------------------------------------------------------------
def run_pipeline_for_day(target_date: date) -> tuple[int, str, Optional[str]]:
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("INSERT INTO scrape_runs (source, data_type, status) VALUES (%s, %s, 'running') RETURNING id", ("tennisexplorer", "results+detail"))
    run_id = cur.fetchone()[0]
    conn.commit()

    rows_processed = 0
    error_message: Optional[str] = None
    status = "success"

    try:
        match_rows = results.run(target_date)
        log.info("Descubiertos %d partidos para %s", len(match_rows), target_date)

        # FASE 1: Descarga y parseo concurrente (Rápido)
        log.info(f"Iniciando descarga concurrente con {MAX_WORKERS} hilos...")
        parsed_results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_row = {executor.submit(fetch_and_parse_worker, row): row for row in match_rows}
            for future in as_completed(future_to_row):
                result = future.result()
                if result["error"]:
                    log.error("  ERR partido %s: %s", result["row"].match_id, result["error"])
                else:
                    parsed_results.append(result)
                    log.info("  OK parseado %s vs %s [id=%s]", result["row"].player_a_name, result["row"].player_b_name, result["row"].match_id)

        # FASE 2: Escritura en base de datos secuencial (Seguro para psycopg2)
        log.info("Escritura en base de datos (%d partidos)...", len(parsed_results))
        for result in parsed_results:
            row = result["row"]
            detail = result["detail"]
            try:
                player_a_id = get_or_create_player(cur, row.player_a_slug, row.player_a_name, detail.player_a_bio, detail.player_a_full_name, getattr(detail, "player_a_photo", None))
                player_b_id = get_or_create_player(cur, row.player_b_slug, row.player_b_name, detail.player_b_bio, detail.player_b_full_name, getattr(detail, "player_b_photo", None))
                
                tournament_id = get_or_create_tournament(
                    cur, 
                    row.tournament_name, 
                    detail.tournament_slug,      # ← El slug canónico
                    detail.tournament_year,      # ← El año extraído de la URL
                    detail.surface, 
                    row.level_guess
                )
                
                winner_id = decide_winner(player_a_id, player_b_id, row.sets_a, row.sets_b)
                match_id = upsert_match(cur, tournament_id, player_a_id, player_b_id, detail.round_name, detail.is_qualifying, target_date, winner_id, sets_to_jsonb(row.sets_a, row.sets_b), str(row.match_id))
                
                upsert_closing_odds(cur, match_id, player_a_id, detail.closing_odds_a)
                upsert_closing_odds(cur, match_id, player_b_id, detail.closing_odds_b)
                
                conn.commit()
                rows_processed += 1
            except Exception as exc:
                conn.rollback()
                log.error("  ERR DB partido %s: %s", row.match_id, exc)

    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        log.error("Error general del pipeline para %s: %s", target_date, exc)

    try:
        cur.execute("UPDATE scrape_runs SET finished_at = now(), status = %s, rows_scraped = %s, error_message = %s WHERE id = %s", (status, rows_processed, error_message, run_id))
        conn.commit()
    except psycopg2.Error as e:
        log.error("No se pudo actualizar scrape_runs: %s", e)
    finally:
        cur.close()
        conn.close()

    log.info("Pipeline terminado para %s: %d partidos, status=%s", target_date, rows_processed, status)
    return rows_processed, status, error_message


# ---------------------------------------------------------------------------
# Subcomandos CLI
# ---------------------------------------------------------------------------
def cmd_scrape(args: argparse.Namespace) -> int:
    start, end = args.start, args.end
    if end < start:
        log.error("La fecha de fin (%s) es anterior a la de inicio (%s).", end, start)
        return 1

    done_days = load_checkpoint()
    current = start
    total_days = (end - start).days + 1
    processed_days = failed_days = 0

    log.info("Iniciando scraping: %s → %s (%d día%s)", start, end, total_days, "s" if total_days > 1 else "")

    while current <= end:
        day_str = current.isoformat()
        if day_str in done_days:
            log.info("Saltando %s (ya procesado)", day_str)
        else:
            try:
                rows, status, _err = run_pipeline_for_day(current)
                if status == "success":
                    done_days.add(day_str)
                    save_checkpoint(done_days)
                    processed_days += 1
                else:
                    failed_days += 1
            except Exception as exc:
                log.error("Día %s falló por completo: %s", day_str, exc)
                failed_days += 1
        current += timedelta(days=1)

    log.info("Resumen final: %d día(s) OK, %d día(s) con error.", processed_days, failed_days)
    return 0 if failed_days == 0 else 1


def cmd_backfill(args: argparse.Namespace) -> int:
    if not HAS_BACKFILL:
        log.error("El módulo 'backfill_players' no está disponible.")
        return 2
    log.info("Ejecutando backfill de perfiles (límite=%d)...", args.limit)
    try:
        backfill_players.run_backfill_all(batch_size=args.limit, max_iterations=1)
    except Exception as exc:
        log.error("El backfill falló: %s", exc)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        conn = get_connection()
    except RuntimeError as e:
        log.error(str(e))
        return 2

    cur = conn.cursor()
    cur.execute("SELECT id, source, data_type, status, rows_scraped, started_at, finished_at, error_message FROM scrape_runs ORDER BY started_at DESC LIMIT %s", (args.limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        log.info("No hay corridas registradas todavía.")
        return 0

    print(f"{'ID':>5}  {'Fuente':<15} {'Tipo':<16} {'Status':<10} {'Filas':>6}  {'Iniciado':<19} {'Finalizado':<19}  Error")
    print("-" * 120)
    for r in rows:
        rid, source, dtype, status, scraped, started, finished, err = r
        started_s = started.strftime("%Y-%m-%d %H:%M:%S") if started else ""
        finished_s = finished.strftime("%Y-%m-%d %H:%M:%S") if finished else ""
        err_s = (err[:40] + "…") if err and len(err) > 40 else (err or "")
        print(f"{rid:>5}  {source:<15} {dtype:<16} {status:<10} {scraped or 0:>6}  {started_s:<19} {finished_s:<19}  {err_s}")
    return 0


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Fecha inválida: '{s}'. Usa el formato YYYY-MM-DD.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline_run", description="CourtSimulator — pipeline de scraping y backfill de tenis.", formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="Scrapea uno o varios días (con checkpoint resumible).")
    p_scrape.add_argument("start", type=_parse_date, help="Fecha de inicio (YYYY-MM-DD).")
    p_scrape.add_argument("end", nargs="?", type=_parse_date, default=None, help="Fecha de fin (YYYY-MM-DD).")
    p_scrape.set_defaults(func=cmd_scrape)

    p_back = sub.add_parser("backfill", help="Enriquece perfiles de jugadores con datos de /player/{slug}/.")
    p_back.add_argument("--limit", type=int, default=100, help="Número máximo de jugadores a procesar.")
    p_back.set_defaults(func=cmd_backfill)

    p_status = sub.add_parser("status", help="Muestra las últimas corridas registradas.")
    p_status.add_argument("--limit", type=int, default=10, help="Número de corridas a mostrar.")
    p_status.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "scrape" and args.end is None:
        args.end = args.start

    try:
        return args.func(args)
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario.")
        return 130
    except Exception as exc:
        log.exception("Error no manejado: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())