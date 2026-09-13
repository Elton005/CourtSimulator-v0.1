"""
backfill_players.py
Enriquece los datos de los jugadores (país, bandera, foto, etc.) usando concurrencia
para reducir el tiempo de espera drásticamente.
"""
import os
import time
import random
import logging
import argparse
import psycopg2
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- IMPORTS DEL SCRAPER ---
from scraper import player_profile

load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("backfill-players")

# Configuración de concurrencia
MAX_WORKERS = 12  # Número de peticiones simultáneas. (10-15 es seguro para no ser bloqueado)
MIN_DELAY = 0.5   # Pequeña pausa entre lotes para ser amables con el servidor
MAX_DELAY = 1.5

def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("Falta la variable de entorno SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def fetch_and_parse_worker(player_id: int, slug: str) -> dict:
    """
    Trabaja en un hilo separado: solo descarga y parsea. 
    NO toca la base de datos aquí.
    """
    try:
        # Pequeña variación aleatoria para no parecer un bot rígido
        time.sleep(random.uniform(0.1, 0.3))
        
        html = player_profile.fetch_player_profile_html(slug)
        profile_data = player_profile.parse_player_profile(html)
        
        if not profile_data:
            return {"player_id": player_id, "slug": slug, "data": None, "error": "No se encontraron datos"}
            
        return {"player_id": player_id, "slug": slug, "data": profile_data, "error": None}
        
    except Exception as exc:
        return {"player_id": player_id, "slug": slug, "data": None, "error": str(exc)}


def run_backfill_all(batch_size: int = 500, max_iterations: int = 20):
    """
    Ejecuta múltiples iteraciones de backfill hasta que no queden jugadores pendientes.
    """
    total_updated = 0
    
    for iteration in range(1, max_iterations + 1):
        log.info(f"\n{'='*70}")
        log.info(f"🔄 ITERACIÓN {iteration}/{max_iterations} - Procesando lote de {batch_size} jugadores")
        log.info(f"{'='*70}\n")
        
        conn = get_connection()
        cur = conn.cursor()
        
        # Contar jugadores pendientes ANTES de procesar
        cur.execute(
            """
            SELECT COUNT(*) FROM players p
            JOIN player_aliases pa ON p.id = pa.player_id
            WHERE pa.source = 'tennisexplorer'
              AND (p.country IS NULL OR p.flag_code IS NULL OR p.photo_url IS NULL OR p.photo_url LIKE '%default-avatar%')
            """
        )
        pending_count = cur.fetchone()[0]
        
        if pending_count == 0:
            log.info("✅ ¡No quedan jugadores pendientes! Backfill completado.")
            cur.close()
            conn.close()
            return total_updated, True
        
        log.info(f"📊 Jugadores pendientes de actualizar: {pending_count}")
        
        # Obtener lote actual
        cur.execute(
            """
            SELECT p.id, pa.alias_name
            FROM players p
            JOIN player_aliases pa ON p.id = pa.player_id
            WHERE pa.source = 'tennisexplorer'
              AND (p.country IS NULL OR p.flag_code IS NULL OR p.photo_url IS NULL OR p.photo_url LIKE '%default-avatar%')
            LIMIT %s
            """,
            (batch_size,)
        )
        players_to_update = cur.fetchall()
        cur.close()
        conn.close() # Cerramos la conexión antes de la fase de red
        
        if not players_to_update:
            log.info("✅ No hay más jugadores para procesar en esta iteración.")
            return total_updated, True
        
        # ==========================================================
        # FASE 1: Descarga y Parseo Concurrente (Rápido)
        # ==========================================================
        log.info(f"⚡ Iniciando descarga concurrente con {MAX_WORKERS} hilos...")
        results = []
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Enviamos todas las tareas al pool
            future_to_player = {
                executor.submit(fetch_and_parse_worker, pid, slug): (pid, slug) 
                for pid, slug in players_to_update
            }
            
            # Recogemos los resultados a medida que se completan
            for future in as_completed(future_to_player):
                res = future.result()
                results.append(res)
                
                if res["error"]:
                    log.warning(f"  ⚠️ Error con {res['slug']}: {res['error']}")
                else:
                    log.info(f"  ✓ Parseado: {res['slug']}")

        # ==========================================================
        # FASE 2: Actualización en Base de Datos (Secuencial y Segura)
        # ==========================================================
        log.info("💾 Guardando resultados en la base de datos...")
        conn = get_connection()
        cur = conn.cursor()
        updated_in_batch = 0
        
        for res in results:
            if res["error"] or not res["data"]:
                continue
                
            player_id = res["player_id"]
            data = res["data"]
            
            try:
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
                        data.get("country"),
                        data.get("flag_code"),
                        data.get("height_cm"),
                        data.get("weight_kg"),
                        data.get("birth_date"),
                        data.get("current_ranking"),
                        data.get("career_high_ranking"),
                        data.get("plays"),
                        data.get("photo_url"),
                        player_id,
                    )
                )
                updated_in_batch += 1
                total_updated += 1
                
            except Exception as exc:
                log.error(f"  ✗ Error de BD con ID {player_id}: {exc}")
                conn.rollback()
                continue
                
        # Confirmar todos los cambios del lote de una vez
        conn.commit()
        cur.close()
        conn.close()
        
        log.info(f"\n📊 Lote {iteration} completado: {updated_in_batch}/{len(players_to_update)} jugadores actualizados")
        log.info(f"📈 Total acumulado: {total_updated} jugadores\n")
        
        # Pausa entre lotes para no saturar a Tennis Explorer
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    
    return total_updated, False  # No terminado (llegó al máximo de iteraciones)


if __name__ == "__main__":
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
        # Modo simple para pruebas
        log.info(f"🚀 Iniciando backfill simple para {args.limit} jugadores...")
        total, _ = run_backfill_all(batch_size=args.limit, max_iterations=1)
        log.info(f"✅ Backfill simple finalizado. Total actualizado: {total}")