"""
pipeline_run.py — CLI profesional de CourtSimulator

Subcomandos:
    scrape      Ejecuta el scraping de uno o varios días (con checkpoint resumible).
    backfill    Enriquece perfiles de jugadores con datos de /player/{slug}/.
    status      Muestra el estado de las últimas corridas registradas.

Uso:
    python pipeline_run.py scrape 2026-08-31
    python pipeline_run.py scrape 2026-08-01 2026-08-31
    python pipeline_run.py backfill --limit 200
    python pipeline_run.py status

Requiere la variable de entorno SUPABASE_DB_URL.
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Imports de módulos del scraper. Si la estructura de carpetas es distinta,
# añadir el directorio al sys.path antes de ejecutar el script.
# ---------------------------------------------------------------------------
try:
    import results
    import match_detail
except ImportError as e:
    sys.stderr.write(
        f"[FATAL] No se pudieron importar los módulos del scraper: {e}\n"
        "Asegúrate de ejecutar el script desde la raíz del proyecto "
        "o de que 'results.py' y 'match_detail.py' estén en el PYTHONPATH.\n"
    )
    sys.exit(2)

# El módulo de backfill es opcional: si no existe, el subcomando `backfill`
# mostrará un mensaje informativo en lugar de romper la CLI.
try:
    import backfill_players
    HAS_BACKFILL = True
except ImportError:
    backfill_players = None
    HAS_BACKFILL = False

# ---------------------------------------------------------------------------
# Configuración global
# ---------------------------------------------------------------------------
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("court-sim")

MIN_DELAY = 2.0
MAX_DELAY = 3.5
CHECKPOINT_FILE = Path("backfill_checkpoint.json")


# ---------------------------------------------------------------------------
# Utilidades: checkpoint, HTTP backoff, conexión a Supabase
# ---------------------------------------------------------------------------
def load_checkpoint() -> set[str]:
    if not CHECKPOINT_FILE.exists():
        return set()
    try:
        with CHECKPOINT_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("Checkpoint corrupto, se reiniciará.")
            return set()
        return set(data)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("No se pudo leer el checkpoint (%s), se reiniciará.", e)
        return set()


def save_checkpoint(done_days: set[str]) -> None:
    try:
        with CHECKPOINT_FILE.open("w", encoding="utf-8") as f:
            json.dump(sorted(done_days), f, indent=2)
    except OSError as e:
        log.error("No se pudo escribir el checkpoint: %s", e)


def fetch_with_backoff(fetch_fn, *args, max_retries: int = 5, **kwargs):
    """Reintenta con backoff exponencial ante 429/403/5xx y errores de red."""
    delay = MIN_DELAY
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            return fetch_fn(*args, **kwargs)
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            # Reintentamos solo ante códigos típicos de bloqueo / servidor.
            if status in (429, 403, 500, 502, 503, 504):
                wait = delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                log.warning(
                    "HTTP %s recibido, esperando %.1fs antes del intento %d/%d",
                    status, wait, attempt, max_retries,
                )
                time.sleep(wait)
                continue
            # Cualquier otro HTTPError (404, 400, ...) se propaga de inmediato.
            raise
        except requests.RequestException as exc:
            # Errores de red (timeout, DNS, conexión reseteada, ...).
            last_exc = exc
            wait = delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log.warning(
                "Error de red (%s), reintentando en %.1fs (%d/%d)",
                exc, wait, attempt, max_retries,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"Agotados los {max_retries} reintentos. Último error: {last_exc}"
    )


def polite_sleep() -> None:
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError(
            "Falta la variable de entorno SUPABASE_DB_URL. "
            "Defínela en tu archivo .env o en el entorno."
        )
    try:
        conn = psycopg2.connect(db_url)
        return conn
    except psycopg2.Error as e:
        raise RuntimeError(f"No se pudo conectar a Supabase: {e}") from e


# ---------------------------------------------------------------------------
# Operaciones de base de datos (players, tournaments, matches, closing_odds)
# ---------------------------------------------------------------------------
def get_or_create_player(
    cur,
    slug: str,
    name_guess: str,
    bio: dict,
    full_name: Optional[str],
    photo_url: Optional[str] = None,
) -> int:
    """
    Busca al jugador por su alias (slug) de TennisExplorer. Si existe,
    actualiza sus datos (con COALESCE para no sobrescribir con NULL).
    Si no existe, lo crea junto con su alias.
    """
    canonical_name = full_name or name_guess

    # 1. ¿Ya existe este alias?
    cur.execute(
        "SELECT player_id FROM player_aliases WHERE source = %s AND alias_name = %s",
        ("tennisexplorer", slug),
    )
    row = cur.fetchone()

    if row:
        player_id = row[0]
        cur.execute(
            """
            UPDATE players SET
                canonical_name  = COALESCE(%s, canonical_name),
                current_ranking = COALESCE(%s, current_ranking),
                birth_date      = COALESCE(%s, birth_date),
                height_cm       = COALESCE(%s, height_cm),
                weight_kg       = COALESCE(%s, weight_kg),
                plays           = COALESCE(%s, plays),
                turned_pro      = COALESCE(%s, turned_pro),
                photo_url       = COALESCE(%s, photo_url)
            WHERE id = %s
            """,
            (
                canonical_name,
                bio.get("current_ranking"),
                bio.get("birth_date"),
                bio.get("height_cm"),
                bio.get("weight_kg"),
                bio.get("plays"),
                bio.get("turned_pro"),
                photo_url,
                player_id,
            ),
        )
        return player_id

    # 2. No existe: INSERT en players + INSERT en player_aliases.
    cur.execute(
        """
        INSERT INTO players (
            canonical_name, current_ranking, birth_date,
            height_cm, weight_kg, plays, turned_pro, photo_url
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            canonical_name,
            bio.get("current_ranking"),
            bio.get("birth_date"),
            bio.get("height_cm"),
            bio.get("weight_kg"),
            bio.get("plays"),
            bio.get("turned_pro"),
            photo_url,
        ),
    )
    player_id = cur.fetchone()[0]

    cur.execute(
        "INSERT INTO player_aliases (player_id, source, alias_name) VALUES (%s, %s, %s)",
        (player_id, "tennisexplorer", slug),
    )
    return player_id


def get_or_create_tournament(
    cur,
    name: str,
    year: Optional[int],
    surface: Optional[str],
    level: str,
) -> int:
    if year is not None:
        cur.execute(
            "SELECT id FROM tournaments WHERE name = %s "
            "AND EXTRACT(YEAR FROM start_date) = %s LIMIT 1",
            (name, year),
        )
    else:
        cur.execute(
            "SELECT id FROM tournaments WHERE name = %s AND start_date IS NULL LIMIT 1",
            (name,),
        )

    row = cur.fetchone()
    if row:
        return row[0]

    approx_start_date = date(year, 1, 1) if year else None
    cur.execute(
        "INSERT INTO tournaments (name, surface, level, start_date) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (name, surface, level, approx_start_date),
    )
    return cur.fetchone()[0]


def upsert_match(
    cur,
    tournament_id: int,
    player_a_id: int,
    player_b_id: int,
    round_name: Optional[str],
    is_qualifying: bool,
    match_date: date,
    winner_id: Optional[int],
    sets: list[dict],
    source_match_id: str,
) -> int:
    cur.execute(
        """
        INSERT INTO matches (
            tournament_id, player_a_id, player_b_id, round, is_qualifying,
            match_date, status, winner_id, sets, source, source_match_id
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, 'finished', %s, %s, 'tennisexplorer', %s
        )
        ON CONFLICT (source, source_match_id) DO UPDATE SET
            round         = EXCLUDED.round,
            is_qualifying = EXCLUDED.is_qualifying,
            winner_id     = EXCLUDED.winner_id,
            sets          = EXCLUDED.sets
        RETURNING id
        """,
        (
            tournament_id, player_a_id, player_b_id, round_name, is_qualifying,
            match_date, winner_id, psycopg2.extras.Json(sets), source_match_id,
        ),
    )
    return cur.fetchone()[0]


def upsert_closing_odds(cur, match_id: int, player_id: int, odds: Optional[float]) -> None:
    if odds is None:
        return
    if odds <= 1.0:
        # Una cuota <= 1.0 no tiene sentido (implied prob > 100%). Saltarla.
        return
    implied_probability = round(100.0 / odds, 2)
    cur.execute(
        """
        INSERT INTO closing_odds (match_id, player_id, odds, implied_probability)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (match_id, player_id) DO UPDATE SET
            odds                = EXCLUDED.odds,
            implied_probability = EXCLUDED.implied_probability,
            captured_at         = now()
        """,
        (match_id, player_id, odds, implied_probability),
    )


def sets_to_jsonb(sets_a: list[dict], sets_b: list[dict]) -> list[dict]:
    return [{"a": a["games"], "b": b["games"]} for a, b in zip(sets_a, sets_b)]


def decide_winner(player_a_id: int, player_b_id: int,
                  sets_a: list[dict], sets_b: list[dict]) -> Optional[int]:
    """Devuelve el id del ganador, o None si no se puede determinar."""
    if not sets_a or not sets_b or len(sets_a) != len(sets_b):
        return None
    won_a = sum(1 for a, b in zip(sets_a, sets_b) if a["games"] > b["games"])
    won_b = sum(1 for a, b in zip(sets_a, sets_b) if b["games"] > a["games"])
    if won_a > won_b:
        return player_a_id
    if won_b > won_a:
        return player_b_id
    return None


# ---------------------------------------------------------------------------
# Pipeline de un día
# ---------------------------------------------------------------------------
def run_pipeline_for_day(target_date: date) -> tuple[int, str, Optional[str]]:
    """
    Ejecuta el pipeline completo para una fecha.
    Devuelve: (filas_procesadas, status, error_message)
    """
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()

    # Registrar la corrida
    cur.execute(
        "INSERT INTO scrape_runs (source, data_type, status) "
        "VALUES (%s, %s, 'running') RETURNING id",
        ("tennisexplorer", "results+detail"),
    )
    run_id = cur.fetchone()[0]
    conn.commit()

    rows_processed = 0
    error_message: Optional[str] = None
    status = "success"

    try:
        match_rows = results.run(target_date)
        log.info("Descubiertos %d partidos para %s", len(match_rows), target_date)

        for row in match_rows:
            try:
                html = fetch_with_backoff(
                    match_detail.fetch_match_detail_html, row.match_id
                )
                detail = match_detail.parse_match_detail(row.match_id, html)

                player_a_id = get_or_create_player(
                    cur, row.player_a_slug, row.player_a_name,
                    detail.player_a_bio, detail.player_a_full_name,
                    getattr(detail, "player_a_photo", None),
                )
                player_b_id = get_or_create_player(
                    cur, row.player_b_slug, row.player_b_name,
                    detail.player_b_bio, detail.player_b_full_name,
                    getattr(detail, "player_b_photo", None),
                )

                tournament_id = get_or_create_tournament(
                    cur, row.tournament_name, row.tournament_year,
                    detail.surface, row.level_guess,
                )

                winner_id = decide_winner(
                    player_a_id, player_b_id, row.sets_a, row.sets_b
                )

                match_id = upsert_match(
                    cur, tournament_id, player_a_id, player_b_id,
                    detail.round_name, detail.is_qualifying,
                    target_date, winner_id,
                    sets_to_jsonb(row.sets_a, row.sets_b),
                    str(row.match_id),
                )

                upsert_closing_odds(cur, match_id, player_a_id, detail.closing_odds_a)
                upsert_closing_odds(cur, match_id, player_b_id, detail.closing_odds_b)

                conn.commit()
                rows_processed += 1
                log.info(
                    "  OK  %s vs %s  [id=%s]",
                    row.player_a_name, row.player_b_name, row.match_id,
                )

            except Exception as exc:
                conn.rollback()
                log.error("  ERR partido %s: %s", row.match_id, exc)

            polite_sleep()

    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        log.error("Error general del pipeline para %s: %s", target_date, exc)

    # Cerrar la corrida
    try:
        cur.execute(
            """
            UPDATE scrape_runs SET
                finished_at   = now(),
                status        = %s,
                rows_scraped  = %s,
                error_message = %s
            WHERE id = %s
            """,
            (status, rows_processed, error_message, run_id),
        )
        conn.commit()
    except psycopg2.Error as e:
        log.error("No se pudo actualizar scrape_runs: %s", e)
    finally:
        cur.close()
        conn.close()

    log.info(
        "Pipeline terminado para %s: %d partidos, status=%s",
        target_date, rows_processed, status,
    )
    return rows_processed, status, error_message


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------
def cmd_scrape(args: argparse.Namespace) -> int:
    start = args.start
    end = args.end

    if end < start:
        log.error("La fecha de fin (%s) es anterior a la de inicio (%s).", end, start)
        return 1

    done_days = load_checkpoint()
    current = start
    total_days = (end - start).days + 1
    processed_days = 0
    failed_days = 0

    log.info(
        "Iniciando scraping: %s → %s (%d día%s)",
        start, end, total_days, "s" if total_days > 1 else "",
    )

    while current <= end:
        day_str = current.isoformat()
        if day_str in done_days:
            log.info("Saltando %s (ya procesado según checkpoint)", day_str)
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

    log.info(
        "Resumen final: %d día(s) OK, %d día(s) con error.",
        processed_days, failed_days,
    )
    return 0 if failed_days == 0 else 1


def cmd_backfill(args: argparse.Namespace) -> int:
    if not HAS_BACKFILL:
        log.error(
            "El módulo 'backfill_players' no está disponible. "
            "Crea el archivo 'backfill_players.py' en tu proyecto para usar este comando."
        )
        return 2

    log.info("Ejecutando backfill de perfiles (límite=%d)...", args.limit)
    try:
        backfill_players.run_backfill(limit=args.limit)
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
    cur.execute(
        """
        SELECT id, source, data_type, status, rows_scraped,
               started_at, finished_at, error_message
        FROM scrape_runs
        ORDER BY started_at DESC
        LIMIT %s
        """,
        (args.limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        log.info("No hay corridas registradas todavía.")
        return 0

    print(f"{'ID':>5}  {'Fuente':<15} {'Tipo':<16} {'Status':<10} "
          f"{'Filas':>6}  {'Iniciado':<19} {'Finalizado':<19}  Error")
    print("-" * 120)
    for r in rows:
        (rid, source, dtype, status, scraped,
         started, finished, err) = r
        started_s = started.strftime("%Y-%m-%d %H:%M:%S") if started else ""
        finished_s = finished.strftime("%Y-%m-%d %H:%M:%S") if finished else ""
        err_s = (err[:40] + "…") if err and len(err) > 40 else (err or "")
        print(
            f"{rid:>5}  {source:<15} {dtype:<16} {status:<10} "
            f"{scraped or 0:>6}  {started_s:<19} {finished_s:<19}  {err_s}"
        )
    return 0


# ---------------------------------------------------------------------------
# Parser de argumentos
# ---------------------------------------------------------------------------
def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Fecha inválida: '{s}'. Usa el formato YYYY-MM-DD."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline_run",
        description="CourtSimulator — pipeline de scraping y backfill de tenis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scrape
    p_scrape = sub.add_parser(
        "scrape",
        help="Scrapea uno o varios días (con checkpoint resumible).",
    )
    p_scrape.add_argument("start", type=_parse_date, help="Fecha de inicio (YYYY-MM-DD).")
    p_scrape.add_argument(
        "end", nargs="?", type=_parse_date, default=None,
        help="Fecha de fin (YYYY-MM-DD). Si se omite, usa 'start'.",
    )
    p_scrape.set_defaults(func=cmd_scrape)

    # backfill
    p_back = sub.add_parser(
        "backfill",
        help="Enriquece perfiles de jugadores con datos de /player/{slug}/.",
    )
    p_back.add_argument(
        "--limit", type=int, default=100,
        help="Número máximo de jugadores a procesar (por defecto: 100).",
    )
    p_back.set_defaults(func=cmd_backfill)

    # status
    p_status = sub.add_parser(
        "status",
        help="Muestra las últimas corridas registradas.",
    )
    p_status.add_argument(
        "--limit", type=int, default=10,
        help="Número de corridas a mostrar (por defecto: 10).",
    )
    p_status.set_defaults(func=cmd_status)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Normalizar: si `scrape` no recibió `end`, usar `start`.
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