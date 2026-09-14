"""
elo_engine.py
Motor de cálculo Elo por superficie para CourtSimulator.

Características:
- 4 Elos independientes (hard, clay, grass, indoor)
- Umbrales: hard(5), clay(5), indoor(5), grass(3) para contar en el Elo General
- Qualifying: K-factor * 0.50, Bonus * 0.30
- Decaimiento temporal (W): max(0.1, 1.0 - (dias / 720))
- Decaimiento de bonus: 0-365 días (100%), 366-730 (50%), 731+ (25%)
"""

import os
import math
import logging
import argparse
from datetime import date, datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s'
)
log = logging.getLogger(__name__)

# ============================================================
# CONFIGURACIÓN Y CONSTANTES
# ============================================================

K_FACTORS = {
    "gs": 100, "atpfinal": 80, "og": 80, "1000": 75,
    "atp500": 60, "atp250": 50, "nextgen": 45,
    "atpcup": 25, "lavercup": 25, "unitedcup": 25,
    "ch175": 20, "ch125": 16, "ch100": 12, "ch75": 10,
    "ch50": 8, "futures": 5, "other": 10,
}

BONUS_POINTS = {
    "gs": 60, "atpfinal": 48, "og": 48, "1000": 38,
    "atp500": 28, "atp250": 20, "nextgen": 16,
    "atpcup": 12, "lavercup": 12, "unitedcup": 12,
    "ch175": 12, "ch125": 8, "ch100": 4, "ch75": 3,
    "ch50": 2, "futures": 1, "other": 0,
}

MIN_MATCHES = {"hard": 5, "clay": 5, "indoor": 5, "grass": 3}
DEFAULT_ELO = 1500.0
DECAY_WINDOW_DAYS = 720


# ============================================================
# ESTRUCTURAS DE DATOS
# ============================================================

@dataclass
class PlayerState:
    player_id: int
    elo_hard: float = DEFAULT_ELO
    elo_clay: float = DEFAULT_ELO
    elo_grass: float = DEFAULT_ELO
    elo_indoor: float = DEFAULT_ELO
    
    matches_hard: int = 0
    matches_clay: int = 0
    matches_grass: int = 0
    matches_indoor: int = 0
    
    # Historial: (match_date, surface, bonus_earned, category)
    bonus_history: List[Tuple[date, str, float, str]] = field(default_factory=list)

    def get_elo(self, surface: str) -> float:
        return getattr(self, f"elo_{surface}")

    def set_elo(self, surface: str, value: float):
        setattr(self, f"elo_{surface}", value)

    def inc_matches(self, surface: str):
        setattr(self, f"matches_{surface}", getattr(self, f"matches_{surface}") + 1)

    def get_matches(self, surface: str) -> int:
        return getattr(self, f"matches_{surface}")


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def normalize_surface(surface: str) -> str:
    s = surface.lower().strip()
    if s in ["hard"]: return "hard"
    if s in ["clay"]: return "clay"
    if s in ["grass"]: return "grass"
    if s in ["indoor", "indoors", "carpet"]: return "indoor"
    return "hard" # Default fallback

def get_k_factor(category: str, is_qualifying: bool) -> float:
    k = K_FACTORS.get(category, 10)
    return k * 0.50 if is_qualifying else k

def get_bonus(category: str, is_qualifying: bool) -> float:
    bonus = BONUS_POINTS.get(category, 0)
    return bonus * 0.30 if is_qualifying else bonus

def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + math.pow(10, (elo_b - elo_a) / 400.0))

def temporal_decay(match_date: date, reference_date: date) -> float:
    days = (reference_date - match_date).days
    return max(0.1, 1.0 - (days / DECAY_WINDOW_DAYS))

def calculate_accumulated_bonus(bonus_history: List[Tuple], reference_date: date) -> float:
    total = 0.0
    for b_date, b_surface, b_value, b_category in bonus_history:
        age_days = (reference_date - b_date).days
        if age_days <= 365:
            total += b_value
        elif age_days <= 730:
            total += b_value * 0.50
        else:
            total += b_value * 0.25
    return total


# ============================================================
# MOTOR PRINCIPAL
# ============================================================

class EloEngine:
    def __init__(self, reference_date: date = None):
        self.players: dict[int, PlayerState] = {}
        self.reference_date = reference_date or date.today()

    def get_or_create_player(self, player_id: int) -> PlayerState:
        if player_id not in self.players:
            self.players[player_id] = PlayerState(player_id=player_id)
        return self.players[player_id]

    def load_from_db(self, conn):
        """Carga el estado actual desde la BD (para cálculos incrementales o reanudación)."""
        log.info("Cargando estado Elo desde la base de datos...")
        with conn.cursor() as cur:
            # 1. Cargar Elos actuales
            cur.execute("""
                SELECT player_id, elo_hard, elo_clay, elo_grass, elo_indoor,
                       matches_hard, matches_clay, matches_grass, matches_indoor
                FROM player_elo_current
            """)
            for row in cur.fetchall():
                pid, eh, ec, eg, ei, mh, mc, mg, mi = row
                self.players[pid] = PlayerState(
                    player_id=pid, elo_hard=eh or DEFAULT_ELO, elo_clay=ec or DEFAULT_ELO,
                    elo_grass=eg or DEFAULT_ELO, elo_indoor=ei or DEFAULT_ELO,
                    matches_hard=mh or 0, matches_clay=mc or 0,
                    matches_grass=mg or 0, matches_indoor=mi or 0,
                )
            
            # 2. Cargar historial de bonos
            cur.execute("""
                SELECT player_id, match_date, surface, bonus_earned, category
                FROM player_bonus_history
            """)
            for row in cur.fetchall():
                pid, mdate, surf, bval, cat = row
                if pid in self.players:
                    self.players[pid].bonus_history.append((mdate, surf, bval, cat))
        
        log.info(f"Cargados {len(self.players)} jugadores.")

    def process_all_matches(self, conn):
        """Procesa todos los partidos finalizados en orden cronológico."""
        log.info("Cargando partidos para procesar...")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.id, m.match_date, m.winner_id, m.player_a_id, m.player_b_id, 
                       m.is_qualifying, t.category, t.surface
                FROM matches m
                JOIN tournaments t ON m.tournament_id = t.id
                WHERE m.match_status = 'finished' AND m.winner_id IS NOT NULL
                ORDER BY m.match_date ASC, m.id ASC
            """)
            matches = cur.fetchall()
        
        log.info(f"Procesando {len(matches)} partidos...")
        processed = 0
        
        for match in matches:
            mid, mdate, winner_id, p_a_id, p_b_id, is_qual, category, surface = match
            
            # Normalizar superficie
            surface = normalize_surface(surface)
            category = category or "other"
            
            winner = self.get_or_create_player(winner_id)
            loser_id = p_b_id if winner_id == p_a_id else p_a_id
            loser = self.get_or_create_player(loser_id)
            
            # Factores del partido
            k = get_k_factor(category, is_qual)
            bonus = get_bonus(category, is_qual)
            
            # Elos actuales
            elo_w = winner.get_elo(surface)
            elo_l = loser.get_elo(surface)
            
            # Cálculo Elo
            E = expected_score(elo_w, elo_l)
            W = temporal_decay(mdate, self.reference_date)
            delta = k * (1 - E) * W
            
            # Actualizar Elos y contadores
            winner.set_elo(surface, elo_w + delta)
            loser.set_elo(surface, elo_l - delta)
            winner.inc_matches(surface)
            loser.inc_matches(surface)
            
            # Registrar bonus para AMBOS
            if bonus > 0:
                winner.bonus_history.append((mdate, surface, bonus, category))
                loser.bonus_history.append((mdate, surface, bonus, category))
            
            processed += 1
            if processed % 10000 == 0:
                log.info(f"  ... {processed} partidos procesados")
                
        log.info(f"Procesamiento completado: {processed} partidos.")

    def save_to_db(self, conn):
        """Guarda los resultados en la BD usando inserción por lotes."""
        log.info("Guardando resultados en la base de datos...")
        with conn.cursor() as cur:
            # 1. Limpiar tablas (para reconstrucción completa)
            cur.execute("TRUNCATE TABLE player_elo_current RESTART IDENTITY")
            cur.execute("TRUNCATE TABLE player_bonus_history RESTART IDENTITY")
            
            # 2. Preparar datos para inserción masiva
            elo_records = []
            bonus_records = []
            
            for pid, p in self.players.items():
                # Calcular Elo General (solo superficies válidas)
                general_elo = DEFAULT_ELO
                total_bonus = 0.0
                
                for surf in ["hard", "clay", "grass", "indoor"]:
                    if p.get_matches(surf) >= MIN_MATCHES[surf]:
                        surf_elo = p.get_elo(surf)
                        surf_bonus = calculate_accumulated_bonus(
                            [(d, s, v, c) for d, s, v, c in p.bonus_history if s == surf],
                            self.reference_date
                        )
                        general_elo += surf_elo + surf_bonus
                        total_bonus += surf_bonus
                
                elo_records.append((
                    pid, p.elo_hard, p.elo_clay, p.elo_grass, p.elo_indoor,
                    p.matches_hard, p.matches_clay, p.matches_grass, p.matches_indoor,
                    general_elo, total_bonus, datetime.now()
                ))
                
                for b_date, b_surf, b_val, b_cat in p.bonus_history:
                    bonus_records.append((pid, b_date, b_surf, b_val, b_cat))
            
            # 3. Inserción masiva
            if elo_records:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO player_elo_current 
                    (player_id, elo_hard, elo_clay, elo_grass, elo_indoor, 
                     matches_hard, matches_clay, matches_grass, matches_indoor, 
                     elo_general, accumulated_bonus, last_updated)
                    VALUES %s
                    """,
                    elo_records
                )
                log.info(f"  Insertados {len(elo_records)} registros en player_elo_current")
            
            if bonus_records:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO player_bonus_history 
                    (player_id, match_date, surface, bonus_earned, category)
                    VALUES %s
                    """,
                    bonus_records
                )
                log.info(f"  Insertados {len(bonus_records)} registros en player_bonus_history")


# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================

def run_elo_calculation(full_rebuild: bool = False):
    log.info("="*60)
    log.info(f"INICIANDO CÁLCULO ELO (Rebuild: {full_rebuild})")
    log.info("="*60)
    
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("Falta la variable de entorno SUPABASE_DB_URL")
        return

    conn = psycopg2.connect(db_url)
    engine = EloEngine()
    
    try:
        if not full_rebuild:
            engine.load_from_db(conn)
            
        engine.process_all_matches(conn)
        engine.save_to_db(conn)
        
        log.info("="*60)
        log.info("CÁLCULO ELO COMPLETADO EXITOSAMENTE")
        log.info("="*60)
        
        # Mostrar Top 10
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.canonical_name, e.elo_general 
                FROM player_elo_current e
                JOIN players p ON e.player_id = p.id
                ORDER BY e.elo_general DESC
                LIMIT 10
            """)
            log.info("\nTOP 10 JUGADORES (Elo General):")
            for i, (name, elo) in enumerate(cur.fetchall(), 1):
                log.info(f"  {i}. {name}: {elo:.1f}")
                
    except Exception as e:
        log.error(f"Error durante el cálculo: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Motor de cálculo Elo")
    parser.add_argument("--full-rebuild", action="store_true", help="Reconstruir todo desde cero (borra datos anteriores)")
    args = parser.parse_args()
    
    run_elo_calculation(full_rebuild=args.full_rebuild)