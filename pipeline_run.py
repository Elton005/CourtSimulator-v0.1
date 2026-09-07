"""
pipeline_run.py

Orquesta el pipeline completo para un día:
  1. results.run(fecha)          -> lista de partidos (IDs, jugadores, marcador preview)
  2. match_detail.parse_match_detail(id) -> ronda, superficie, cuotas de cierre, bio
  3. Inserta/actualiza en Supabase: players, player_aliases, tournaments, matches, closing_odds
  4. Registra la corrida en scrape_runs

Requiere la variable de entorno SUPABASE_DB_URL (connection string de
Settings > Database en Supabase). NUNCA hardcodear la clave aquí.

Uso:
    python pipeline_run.py 2026-08-31
"""

import os
import sys
import time
import random
import json
import logging
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

import results
import match_detail

load_dotenv(override=True)  # el .env manda siempre, incluso si ya hay una variable de entorno vieja del sistema

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MIN_DELAY = 2.0
MAX_DELAY = 3.5
CHECKPOINT_FILE = "backfill_checkpoint.json"


def load_checkpoint() -> set[str]:
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    with open(CHECKPOINT_FILE, "r") as f:
        return set(json.load(f))


def save_checkpoint(done_days: set[str]):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(sorted(done_days), f)


def fetch_with_backoff(fetch_fn, *args, max_retries: int = 5, **kwargs):
    """
    Envuelve cualquier función de fetch (match_detail.fetch_match_detail_html,
    results.fetch_results_html) con backoff exponencial ante 429/403/errores
    de red. Si sigue fallando tras max_retries, deja que el error suba.
    """
    delay = MIN_DELAY
    for attempt in range(max_retries):
        try:
            return fetch_fn(*args, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (429, 403):
                wait = delay * (2 ** attempt) + random.uniform(0, 1)
                log.warning("Status %s recibido, esperando %.1fs antes de reintentar (intento %d/%d)",
                            status, wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Agotados los reintentos tras recibir bloqueos repetidos ({max_retries})")


def polite_sleep():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("Falta la variable de entorno SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def get_or_create_player(cur, slug: str, name_guess: str, bio: dict, full_name: str | None) -> int:
    cur.execute(
        "SELECT player_id FROM player_aliases WHERE source = %s AND alias_name = %s",
        ("tennisexplorer", slug),
    )
    row = cur.fetchone()

    canonical_name = full_name or name_guess

    if row:
        player_id = row[0]
        cur.execute(
            """
            UPDATE players SET
                canonical_name = COALESCE(%s, canonical_name),
                current_ranking = COALESCE(%s, current_ranking),
                birth_date = COALESCE(%s, birth_date),
                height_cm = COALESCE(%s, height_cm),
                weight_kg = COALESCE(%s, weight_kg),
                plays = COALESCE(%s, plays),
                turned_pro = COALESCE(%s, turned_pro)
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
                player_id,
            ),
        )
        return player_id

    cur.execute(
        """
        INSERT INTO players (canonical_name, current_ranking, birth_date, height_cm, weight_kg, plays, turned_pro)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
    player_id = cur.fetchone()[0]

    cur.execute(
        "INSERT INTO player_aliases (player_id, source, alias_name) VALUES (%s, %s, %s)",
        (player_id, "tennisexplorer", slug),
    )
    return player_id


def get_or_create_tournament(cur, name: str, year: int | None, surface: str | None, level: str) -> int:
    if year is not None:
        cur.execute(
            "SELECT id FROM tournaments WHERE name = %s AND EXTRACT(YEAR FROM start_date) = %s LIMIT 1",
            (name, year),
        )
    else:
        cur.execute("SELECT id FROM tournaments WHERE name = %s AND start_date IS NULL LIMIT 1", (name,))

    row = cur.fetchone()
    if row:
        return row[0]

    approx_start_date = date(year, 1, 1) if year else None
    cur.execute(
        "INSERT INTO tournaments (name, surface, level, start_date) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, surface, level, approx_start_date),
    )
    return cur.fetchone()[0]


def upsert_match(cur, tournament_id, player_a_id, player_b_id, round_name, is_qualifying,
                  match_date, winner_id, sets, source_match_id) -> int:
    cur.execute(
        """
        INSERT INTO matches (
            tournament_id, player_a_id, player_b_id, round, is_qualifying,
            match_date, status, winner_id, sets, source, source_match_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, 'finished', %s, %s, 'tennisexplorer', %s)
        ON CONFLICT (source, source_match_id) DO UPDATE SET
            round = EXCLUDED.round,
            is_qualifying = EXCLUDED.is_qualifying,
            winner_id = EXCLUDED.winner_id,
            sets = EXCLUDED.sets
        RETURNING id
        """,
        (
            tournament_id, player_a_id, player_b_id, round_name, is_qualifying,
            match_date, winner_id, psycopg2.extras.Json(sets), source_match_id,
        ),
    )
    return cur.fetchone()[0]


def upsert_closing_odds(cur, match_id, player_id, odds):
    if odds is None:
        return
    implied_probability = round(100 / odds, 2)
    cur.execute(
        """
        INSERT INTO closing_odds (match_id, player_id, odds, implied_probability)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (match_id, player_id) DO UPDATE SET
            odds = EXCLUDED.odds,
            implied_probability = EXCLUDED.implied_probability,
            captured_at = now()
        """,
        (match_id, player_id, odds, implied_probability),
    )


def sets_to_jsonb(sets_a: list[dict], sets_b: list[dict]) -> list[dict]:
    return [
        {"a": a["games"], "b": b["games"]}
        for a, b in zip(sets_a, sets_b)
    ]


def run_pipeline(target_date: date):
    conn = get_connection()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO scrape_runs (source, data_type, status) VALUES (%s, %s, 'running') RETURNING id",
        ("tennisexplorer", "results+detail"),
    )
    run_id = cur.fetchone()[0]
    conn.commit()

    rows_processed = 0
    error_message = None

    try:
        match_rows = results.run(target_date)
        log.info("Descubiertos %d partidos para %s", len(match_rows), target_date)

        for row in match_rows:
            try:
                html = fetch_with_backoff(match_detail.fetch_match_detail_html, row.match_id)
                detail = match_detail.parse_match_detail(row.match_id, html)

                player_a_id = get_or_create_player(
                    cur, row.player_a_slug, row.player_a_name,
                    detail.player_a_bio, detail.player_a_full_name,
                )
                player_b_id = get_or_create_player(
                    cur, row.player_b_slug, row.player_b_name,
                    detail.player_b_bio, detail.player_b_full_name,
                )

                tournament_id = get_or_create_tournament(
                    cur, row.tournament_name, row.tournament_year, detail.surface, row.level_guess
                )

                winner_id = player_a_id if len(row.sets_a) and sum(
                    1 for a, b in zip(row.sets_a, row.sets_b) if a["games"] > b["games"]
                ) > len(row.sets_a) / 2 else player_b_id

                match_id = upsert_match(
                    cur, tournament_id, player_a_id, player_b_id,
                    detail.round_name, detail.is_qualifying,
                    target_date, winner_id,
                    sets_to_jsonb(row.sets_a, row.sets_b),
                    row.match_id,
                )

                upsert_closing_odds(cur, match_id, player_a_id, detail.closing_odds_a)
                upsert_closing_odds(cur, match_id, player_b_id, detail.closing_odds_b)

                conn.commit()
                rows_processed += 1
                log.info("  OK %s vs %s (id=%s)", row.player_a_name, row.player_b_name, row.match_id)

            except Exception as exc:
                conn.rollback()
                log.error("  Error en partido %s: %s", row.match_id, exc)

            polite_sleep()
        status = "success"

    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        log.error("Error general del pipeline: %s", exc)

    cur.execute(
        """
        UPDATE scrape_runs SET
            finished_at = now(), status = %s, rows_scraped = %s, error_message = %s
        WHERE id = %s
        """,
        (status, rows_processed, error_message, run_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    log.info("Pipeline terminado: %d partidos procesados, status=%s", rows_processed, status)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso:")
        print("  python pipeline_run.py YYYY-MM-DD                 (un solo día)")
        print("  python pipeline_run.py YYYY-MM-DD YYYY-MM-DD       (rango, resumible)")
        sys.exit(1)

    start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    end = datetime.strptime(sys.argv[2], "%Y-%m-%d").date() if len(sys.argv) > 2 else start

    done_days = load_checkpoint()
    current = start

    while current <= end:
        day_str = current.isoformat()
        if day_str in done_days:
            log.info("Saltando %s (ya procesado según checkpoint)", day_str)
        else:
            try:
                run_pipeline(current)
                done_days.add(day_str)
                save_checkpoint(done_days)
            except Exception as exc:
                log.error("Día %s falló por completo, se puede reintentar después: %s", day_str, exc)
                # No lo marcamos como hecho -- al volver a correr el script, se retoma aquí.
        current += timedelta(days=1)