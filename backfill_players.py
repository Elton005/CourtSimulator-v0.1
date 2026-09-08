import os
import time
import random
import logging
import psycopg2
from dotenv import load_dotenv

# --- IMPORTS DEL SCRAPER (ACTUALIZADOS) ---
from scraper import player_profile

from scraper.rounds import round_sort_key

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MIN_DELAY = 2.0
MAX_DELAY = 3.5

def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("Falta la variable de entorno SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def run_backfill(limit: int = 100):
    conn = get_connection()
    cur = conn.cursor()

    # Buscar jugadores de Tennis Explorer que les falte país o foto
    cur.execute(
        """
        SELECT p.id, pa.alias_name
        FROM players p
        JOIN player_aliases pa ON p.id = pa.player_id
        WHERE pa.source = 'tennisexplorer'
          AND (p.country IS NULL OR p.photo_url IS NULL OR p.current_ranking IS NULL)
        LIMIT %s
        """,
        (limit,)
    )
    players_to_update = cur.fetchall()
    
    if not players_to_update:
        log.info("No hay jugadores pendientes de actualizar. ¡Todo al día!")
        cur.close()
        conn.close()
        return

    log.info("Se encontraron %d jugadores para actualizar.", len(players_to_update))
    updated_count = 0

    for player_id, slug in players_to_update:
        try:
            log.info(f"Procesando jugador: {slug} (ID: {player_id})")
            html = player_profile.fetch_player_profile_html(slug)
            profile_data = player_profile.parse_player_profile(html)

            if not profile_data:
                log.warning(f"No se pudieron extraer datos para {slug}")
                continue

            # Actualizar campos usando COALESCE (incluyendo flag_code ahora)
            cur.execute(
                """
                UPDATE players SET
                    country = COALESCE(%s, country),
                    flag_code = COALESCE(%s, flag_code),
                    height_cm = COALESCE(%s, height_cm),
                    weight_kg = COALESCE(%s, weight_kg),
                    birth_date = COALESCE(%s, birth_date),
                    current_ranking = COALESCE(%s, current_ranking),
                    career_high_ranking = COALESCE(%s, career_high_ranking),
                    plays = COALESCE(%s, plays),
                    photo_url = COALESCE(%s, photo_url)
                WHERE id = %s
                """,
                (
                    profile_data.get("country"),
                    profile_data.get("flag_code"),       # <-- CORREGIDO: Ahora se guarda
                    profile_data.get("height_cm"),
                    profile_data.get("weight_kg"),
                    profile_data.get("birth_date"),
                    profile_data.get("current_ranking"),
                    profile_data.get("career_high_ranking"),
                    profile_data.get("plays"),
                    profile_data.get("photo_url"),
                    player_id,
                )
            )
            conn.commit()
            updated_count += 1
            log.info(f"  ✓ Actualizado: {slug} (País: {profile_data.get('country')})")

        except Exception as exc:
            conn.rollback()
            log.error(f"  ✗ Error al procesar {slug}: {exc}")

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    cur.close()
    conn.close()
    log.info(f"Backfill completado. {updated_count} jugadores actualizados exitosamente.")


if __name__ == "__main__":
    run_backfill(limit=100)