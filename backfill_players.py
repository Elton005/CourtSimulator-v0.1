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

MIN_DELAY = 1.0
MAX_DELAY = 1.5

def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("Falta la variable de entorno SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def run_backfill_all(batch_size: int = 500, max_iterations: int = 20):
    """
    Ejecuta múltiples iteraciones de backfill hasta que no queden jugadores pendientes.
    Ideal para ejecuciones largas en GitHub Actions.
    """
    total_updated = 0
    
    for iteration in range(1, max_iterations + 1):
        log.info(f"\n{'='*60}")
        log.info(f"🔄 ITERACIÓN {iteration}/{max_iterations} - Procesando lote de {batch_size} jugadores")
        log.info(f"{'='*60}\n")
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Contar jugadores pendientes ANTES de procesar
        cur.execute(
            """
            SELECT COUNT(*) FROM players p
            JOIN player_aliases pa ON p.id = pa.player_id
            WHERE pa.source = 'tennisexplorer'
              AND (p.country IS NULL OR p.photo_url IS NULL OR p.current_ranking IS NULL)
            """
        )
        pending_count = cur.fetchone()[0]
        
        if pending_count == 0:
            log.info("✅ ¡No quedan jugadores pendientes! Backfill completado.")
            cur.close()
            conn.close()
            return total_updated, True  # Terminado
        
        log.info(f"📊 Jugadores pendientes de actualizar: {pending_count}")
        
        # Obtener lote actual
        cur.execute(
            """
            SELECT p.id, pa.alias_name
            FROM players p
            JOIN player_aliases pa ON p.id = pa.player_id
            WHERE pa.source = 'tennisexplorer'
              AND (p.country IS NULL OR p.photo_url IS NULL OR p.current_ranking IS NULL)
            LIMIT %s
            """,
            (batch_size,)
        )
        players_to_update = cur.fetchall()
        cur.close()
        conn.close()
        
        if not players_to_update:
            log.info("✅ No hay más jugadores para procesar en esta iteración.")
            return total_updated, True
        
        updated_in_batch = 0
        
        for player_id, slug in players_to_update:
            try:
                log.info(f"Procesando jugador: {slug} (ID: {player_id})")
                html = player_profile.fetch_player_profile_html(slug)
                profile_data = player_profile.parse_player_profile(html)

                if not profile_data:
                    log.warning(f"No se pudieron extraer datos para {slug}")
                    continue

                conn = get_connection()
                cur = conn.cursor()
                
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
                        profile_data.get("flag_code"),
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
                cur.close()
                conn.close()
                
                updated_in_batch += 1
                total_updated += 1
                log.info(f"  ✓ Actualizado: {slug} (País: {profile_data.get('country')})")

            except Exception as exc:
                log.error(f"  ✗ Error al procesar {slug}: {exc}")

            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        
        log.info(f"\n📊 Lote {iteration} completado: {updated_in_batch}/{len(players_to_update)} jugadores actualizados")
        log.info(f"📈 Total acumulado: {total_updated} jugadores\n")
    
    return total_updated, False  # No terminado (llegó al máximo de iteraciones)

def run_backfill(limit: int = 100):
    """
    Ejecuta un backfill simple para un número limitado de jugadores.
    """
    log.info(f"🚀 Iniciando backfill simple para {limit} jugadores...")
    total, completed = run_backfill_all(batch_size=limit, max_iterations=1)
    log.info(f"✅ Backfill simple finalizado. Total actualizado: {total}")


if __name__ == "__main__":
    import argparse
    # ... (el resto de tu código argparse se queda igual) ...

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Backfill de perfiles de jugadores")
    parser.add_argument("--limit", type=int, default=100, help="Número máximo de jugadores (modo simple)")
    parser.add_argument("--all", action="store_true", help="Procesar TODOS los jugadores en lotes")
    parser.add_argument("--batch-size", type=int, default=500, help="Tamaño de cada lote (con --all)")
    
    args = parser.parse_args()
    
    if args.all:
        total, completed = run_backfill_all(batch_size=args.batch_size)
        log.info(f"\n🏁 Backfill masivo finalizado. Total actualizado: {total}")
        if completed:
            log.info("✅ Todos los jugadores están actualizados.")
        else:
            log.info("⚠️ Se alcanzó el máximo de iteraciones. Ejecuta de nuevo para continuar.")
    else:
        run_backfill(limit=args.limit)
