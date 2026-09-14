"""
elo_engine.py
Motor de cálculo Elo por superficie para CourtSimulator.

Características:
- 4 Elos independientes por jugador (hard, clay, grass, indoor)
- Umbrales mínimos: 5 partidos (hard/clay/indoor), 3 partidos (grass)
- Ignora categorías "other" y exhibiciones
- Qualifying cuenta para el mínimo pero con 30% del bonus
- Decaimiento temporal W = max(0.1, 1.0 - (dias/720))
"""

import os
import math
import logging
from datetime import date, datetime
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict

import psycopg2
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s'
)
log = logging.getLogger(__name__)

# ============================================================
# CONFIGURACIÓN
# ============================================================

# Factores K por categoría de torneo
K_FACTORS = {
    "gs": 100,
    "atpfinal": 80,
    "og": 80,
    "1000": 75,
    "atp500": 60,
    "atp250": 50,
    "nextgen": 45,
    "atpcup": 25,
    "lavercup": 25,
    "unitedcup": 25,
    "ch175": 20,
    "ch125": 16,
    "ch100": 12,
    "ch75": 10,
    "ch50": 8,
    "futures": 5,
}

# Bonus points por categoría
BONUS_POINTS = {
    "gs": 60,
    "atpfinal": 48,
    "og": 48,
    "1000": 38,
    "atp500": 28,
    "atp250": 20,
    "nextgen": 16,
    "atpcup": 12,
    "lavercup": 12,
    "unitedcup": 12,
    "ch175": 12,
    "ch125": 8,
    "ch100": 4,
    "ch75": 3,
    "ch50": 2,
    "futures": 1,
}

# Categorías que se ignoran completamente
IGNORED_CATEGORIES = {"other", None, ""}

# Umbrales mínimos de partidos para que el Elo cuente en el general
MIN_MATCHES_THRESHOLD = {
    "hard": 5,
    "clay": 5,
    "indoor": 5,
    "grass": 3,  # Menos torneos de grass al año
}

# Elo inicial por defecto
DEFAULT_ELO = 1500.0

# Ventana de decaimiento temporal (días)
DECAY_WINDOW_DAYS = 720

# Multiplicadores para qualifying
QUALIFYING_K_MULTIPLIER = 0.50
QUALIFYING_BONUS_MULTIPLIER = 0.30


# ============================================================
# CLASES DE DATOS
# ============================================================

@dataclass
class PlayerEloState:
    """Estado completo del Elo de un jugador."""
    player_id: int
    
    # Elos por superficie
    elo_hard: float = DEFAULT_ELO
    elo_clay: float = DEFAULT_ELO
    elo_grass: float = DEFAULT_ELO
    elo_indoor: float = DEFAULT_ELO
    
    # Contadores de partidos por superficie
    matches_hard: int = 0
    matches_clay: int = 0
    matches_grass: int = 0
    matches_indoor: int = 0
    
    # Historial de bonos (fecha, superficie, bonus, categoría, is_qualifying)
    bonus_history: List[Tuple[date, str, float, str, bool]] = field(default_factory=list)
    
    def get_elo_for_surface(self, surface: str) -> float:
        """Obtiene el Elo actual para una superficie específica."""
        surface_key = self._normalize_surface(surface)
        return getattr(self, f"elo_{surface_key}")
    
    def set_elo_for_surface(self, surface: str, new_elo: float):
        """Actualiza el Elo para una superficie específica."""
        surface_key = self._normalize_surface(surface)
        setattr(self, f"elo_{surface_key}", new_elo)
    
    def increment_matches(self, surface: str):
        """Incrementa el contador de partidos para una superficie."""
        surface_key = self._normalize_surface(surface)
        current = getattr(self, f"matches_{surface_key}")
        setattr(self, f"matches_{surface_key}", current + 1)
    
    def get_matches_for_surface(self, surface: str) -> int:
        """Obtiene el número de partidos jugados en una superficie."""
        surface_key = self._normalize_surface(surface)
        return getattr(self, f"matches_{surface_key}")
    
    def calculate_general_elo(self, reference_date: date) -> float:
        """
        Calcula el Elo general sumando solo las superficies válidas.
        Una superficie es válida si tiene suficientes partidos jugados.
        """
        total = 0.0
        valid_surfaces = 0
        
        for surface in ["hard", "clay", "grass", "indoor"]:
            matches_count = self.get_matches_for_surface(surface)
            threshold = MIN_MATCHES_THRESHOLD[surface]
            
            if matches_count >= threshold:
                surface_elo = self.get_elo_for_surface(surface)
                # Aplicar decaimiento al bonus acumulado
                bonus_with_decay = self._calculate_bonus_with_decay(surface, reference_date)
                total += surface_elo + bonus_with_decay
                valid_surfaces += 1
        
        # Si no hay superficies válidas, devolver Elo por defecto
        if valid_surfaces == 0:
            return DEFAULT_ELO
        
        return total
    
    def _calculate_bonus_with_decay(self, surface: str, reference_date: date) -> float:
        """
        Calcula el bonus acumulado con decaimiento por antigüedad.
        - 0-365 días: 100%
        - 366-730 días: 50%
        - 731+ días: 25%
        """
        total = 0.0
        for bonus_date, bonus_surface, bonus_value, category, is_qualifying in self.bonus_history:
            if bonus_surface != surface:
                continue
            
            age_days = (reference_date - bonus_date).days
            if age_days <= 365:
                total += bonus_value
            elif age_days <= 730:
                total += bonus_value * 0.50
            else:
                total += bonus_value * 0.25
        
        return total
    
    @staticmethod
    def _normalize_surface(surface: str) -> str:
        """Normaliza el nombre de la superficie."""
        surface = surface.lower().strip()
        if surface in ["hard"]:
            return "hard"
        elif surface in ["clay"]:
            return "clay"
        elif surface in ["grass"]:
            return "grass"
        elif surface in ["indoor", "indoors", "carpet"]:
            return "indoor"
        else:
            return "hard"  # Default a hard si no se reconoce


@dataclass
class MatchData:
    """Datos de un partido para procesar."""
    match_id: int
    match_date: date
    winner_id: int
    loser_id: int
    surface: str
    category: str
    round_name: str
    is_qualifying: bool


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def get_k_factor(category: str, is_qualifying: bool) -> float:
    """Calcula el factor K para un partido."""
    base_k = K_FACTORS.get(category, 10)
    if is_qualifying:
        return base_k * QUALIFYING_K_MULTIPLIER
    return base_k


def get_bonus(category: str, is_qualifying: bool) -> float:
    """Calcula el bonus point para un partido."""
    base_bonus = BONUS_POINTS.get(category, 0)
    if is_qualifying:
        return base_bonus * QUALIFYING_BONUS_MULTIPLIER
    return base_bonus


def expected_score(elo_a: float, elo_b: float) -> float:
    """Calcula la probabilidad esperada de que A gane contra B."""
    return 1.0 / (1.0 + math.pow(10, (elo_b - elo_a) / 400.0))


def temporal_decay(match_date: date, reference_date: date) -> float:
    """Calcula el factor de decaimiento temporal W."""
    days = (reference_date - match_date).days
    return max(0.1, 1.0 - (days / DECAY_WINDOW_DAYS))


# ============================================================
# MOTOR ELO PRINCIPAL
# ============================================================

class EloEngine:
    """Motor de cálculo Elo por superficie."""
    
    def __init__(self):
        self.players: Dict[int, PlayerEloState] = {}
        self.reference_date = date.today()
    
    def get_or_create_player(self, player_id: int) -> PlayerEloState:
        """Obtiene o crea el estado de Elo de un jugador."""
        if player_id not in self.players:
            self.players[player_id] = PlayerEloState(player_id=player_id)
        return self.players[player_id]
    
    def process_match(self, match: MatchData) -> dict:
        """
        Procesa un partido y actualiza los Elos.
        Retorna un diccionario con los datos de auditoría.
        """
        # Ignorar categorías no válidas
        if match.category in IGNORED_CATEGORIES:
            return None
        
        # Obtener estados de los jugadores
        winner = self.get_or_create_player(match.winner_id)
        loser = self.get_or_create_player(match.loser_id)
        
        # Obtener Elos actuales para la superficie del partido
        surface = match.surface
        elo_winner = winner.get_elo_for_surface(surface)
        elo_loser = loser.get_elo_for_surface(surface)
        
        # Calcular factores
        k = get_k_factor(match.category, match.is_qualifying)
        bonus = get_bonus(match.category, match.is_qualifying)
        E = expected_score(elo_winner, elo_loser)
        W = temporal_decay(match.match_date, self.reference_date)
        
        # Calcular delta
        delta = k * (1 - E) * W
        
        # Actualizar Elos
        new_elo_winner = elo_winner + delta
        new_elo_loser = elo_loser - delta
        
        winner.set_elo_for_surface(surface, new_elo_winner)
        loser.set_elo_for_surface(surface, new_elo_loser)
        
        # Incrementar contadores de partidos
        winner.increment_matches(surface)
        loser.increment_matches(surface)
        
        # Registrar bonus para ambos jugadores
        if bonus > 0:
            winner.bonus_history.append((
                match.match_date, surface, bonus, match.category, match.is_qualifying
            ))
            loser.bonus_history.append((
                match.match_date, surface, bonus, match.category, match.is_qualifying
            ))
        
        # Retornar datos de auditoría
        return {
            "match_id": match.match_id,
            "winner_id": match.winner_id,
            "loser_id": match.loser_id,
            "surface": surface,
            "category": match.category,
            "round_name": match.round_name,
            "is_qualifying": match.is_qualifying,
            "k_factor": k,
            "bonus_point": bonus,
            "expected_winner": E,
            "decay_w": W,
            "delta_elo": delta,
            "winner_elo_before": elo_winner,
            "loser_elo_before": elo_loser,
            "winner_elo_after": new_elo_winner,
            "loser_elo_after": new_elo_loser,
            "match_date": match.match_date,
        }
    
    def update_general_elos(self):
        """Actualiza el Elo general de todos los jugadores."""
        for player in self.players.values():
            player.elo_general = player.calculate_general_elo(self.reference_date)


# ============================================================
# PERSISTENCIA EN BASE DE DATOS
# ============================================================

def load_matches_from_db(conn, start_date: Optional[date] = None, start_match_id: Optional[int] = None) -> List[MatchData]:
    """
    Carga partidos desde la base de datos.
    Si se proporciona start_date o start_match_id, solo carga partidos posteriores.
    """
    cur = conn.cursor()
    
    query = """
        SELECT 
            m.id AS match_id,
            m.match_date,
            m.winner_id,
            CASE 
                WHEN m.winner_id = m.player_a_id THEN m.player_b_id
                ELSE m.player_a_id
            END AS loser_id,
            t.surface,
            t.category,
            m.round,
            m.is_qualifying
        FROM matches m
        JOIN tournaments t ON m.tournament_id = t.id
        WHERE m.winner_id IS NOT NULL
          AND m.match_status = 'finished'
    """
    
    params = []
    
    if start_date:
        query += " AND m.match_date >= %s"
        params.append(start_date)
    
    if start_match_id:
        query += " AND m.id > %s"
        params.append(start_match_id)
    
    query += " ORDER BY m.match_date ASC, m.id ASC"
    
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    
    matches = []
    for row in rows:
        match_id, match_date, winner_id, loser_id, surface, category, round_name, is_qualifying = row
        matches.append(MatchData(
            match_id=match_id,
            match_date=match_date,
            winner_id=winner_id,
            loser_id=loser_id,
            surface=surface or "hard",
            category=category or "other",
            round_name=round_name or "Unknown",
            is_qualifying=is_qualifying or False,
        ))
    
    return matches


def load_existing_elo_state(conn) -> Dict[int, PlayerEloState]:
    """Carga el estado de Elo existente desde la base de datos."""
    cur = conn.cursor()
    
    # Cargar Elos actuales
    cur.execute("""
        SELECT player_id, elo_hard, elo_clay, elo_grass, elo_indoor,
               matches_hard, matches_clay, matches_grass, matches_indoor
        FROM player_elo_current
    """)
    elo_rows = cur.fetchall()
    
    players = {}
    for row in elo_rows:
        player_id, elo_hard, elo_clay, elo_grass, elo_indoor, matches_hard, matches_clay, matches_grass, matches_indoor = row
        players[player_id] = PlayerEloState(
            player_id=player_id,
            elo_hard=elo_hard or DEFAULT_ELO,
            elo_clay=elo_clay or DEFAULT_ELO,
            elo_grass=elo_grass or DEFAULT_ELO,
            elo_indoor=elo_indoor or DEFAULT_ELO,
            matches_hard=matches_hard or 0,
            matches_clay=matches_clay or 0,
            matches_grass=matches_grass or 0,
            matches_indoor=matches_indoor or 0,
        )
    
    # Cargar historial de bonos
    cur.execute("""
        SELECT player_id, match_date, surface, bonus_earned, category, is_qualifying
        FROM player_bonus_history
        ORDER BY match_date ASC
    """)
    bonus_rows = cur.fetchall()
    
    for row in bonus_rows:
        player_id, match_date, surface, bonus_earned, category, is_qualifying = row
        if player_id in players:
            players[player_id].bonus_history.append((
                match_date, surface, bonus_earned, category, is_qualifying
            ))
    
    cur.close()
    return players


def save_elo_state_to_db(conn, engine: EloEngine, audit_data: List[dict]):
    """Guarda el estado de Elo y los datos de auditoría en la base de datos."""
    cur = conn.cursor()
    
    # Limpiar tablas de resultados anteriores
    cur.execute("DELETE FROM player_elo_current")
    cur.execute("DELETE FROM player_bonus_history")
    cur.execute("DELETE FROM match_elo_audit")
    
    # Insertar Elos actuales
    for player in engine.players.values():
        cur.execute("""
            INSERT INTO player_elo_current (
                player_id, elo_hard, elo_clay, elo_grass, elo_indoor,
                matches_hard, matches_clay, matches_grass, matches_indoor,
                elo_general, last_updated
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            player.player_id,
            player.elo_hard,
            player.elo_clay,
            player.elo_grass,
            player.elo_indoor,
            player.matches_hard,
            player.matches_clay,
            player.matches_grass,
            player.matches_indoor,
            player.elo_general,
            datetime.now(),
        ))
    
    # Insertar historial de bonos
    for player in engine.players.values():
        for match_date, surface, bonus, category, is_qualifying in player.bonus_history:
            cur.execute("""
                INSERT INTO player_bonus_history (
                    player_id, match_date, surface, bonus_earned, category, is_qualifying
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                player.player_id,
                match_date,
                surface,
                bonus,
                category,
                is_qualifying,
            ))
    
    # Insertar datos de auditoría
    for audit in audit_data:
        cur.execute("""
            INSERT INTO match_elo_audit (
                match_id, winner_id, loser_id, surface, category, round_name, is_qualifying,
                k_factor, bonus_point, expected_winner, decay_w, delta_elo,
                winner_elo_before, loser_elo_before, winner_elo_after, loser_elo_after,
                match_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            audit["match_id"],
            audit["winner_id"],
            audit["loser_id"],
            audit["surface"],
            audit["category"],
            audit["round_name"],
            audit["is_qualifying"],
            audit["k_factor"],
            audit["bonus_point"],
            audit["expected_winner"],
            audit["decay_w"],
            audit["delta_elo"],
            audit["winner_elo_before"],
            audit["loser_elo_before"],
            audit["winner_elo_after"],
            audit["loser_elo_after"],
            audit["match_date"],
        ))
    
    # Actualizar estado de procesamiento
    if audit_data:
        last_match = audit_data[-1]
        cur.execute("""
            INSERT INTO elo_processing_state (id, last_processed_match_date, last_processed_match_id, total_matches_processed, last_run_at)
            VALUES (1, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                last_processed_match_date = EXCLUDED.last_processed_match_date,
                last_processed_match_id = EXCLUDED.last_processed_match_id,
                total_matches_processed = EXCLUDED.total_matches_processed,
                last_run_at = EXCLUDED.last_run_at
        """, (
            last_match["match_date"],
            last_match["match_id"],
            len(audit_data),
            datetime.now(),
        ))
    
    conn.commit()
    cur.close()


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def run_elo_calculation(full_rebuild: bool = False):
    """
    Ejecuta el cálculo de Elo.
    
    Args:
        full_rebuild: Si es True, reconstruye todo desde cero.
                     Si es False, solo procesa partidos nuevos.
    """
    log.info("=" * 70)
    log.info("INICIANDO CÁLCULO ELO")
    log.info("=" * 70)
    
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    
    try:
        # Cargar o crear motor
        engine = EloEngine()
        
        if not full_rebuild:
            # Cargar estado existente
            log.info("Cargando estado de Elo existente...")
            engine.players = load_existing_elo_state(conn)
            log.info(f"Cargados {len(engine.players)} jugadores")
            
            # Obtener último partido procesado
            cur = conn.cursor()
            cur.execute("""
                SELECT last_processed_match_id, last_processed_match_date
                FROM elo_processing_state
                WHERE id = 1
            """)
            row = cur.fetchone()
            cur.close()
            
            if row:
                last_match_id, last_match_date = row
                log.info(f"Último partido procesado: ID {last_match_id}, fecha {last_match_date}")
                matches = load_matches_from_db(conn, start_match_id=last_match_id)
            else:
                log.info("No hay estado previo, procesando todos los partidos")
                matches = load_matches_from_db(conn)
        else:
            log.info("Modo reconstrucción completa")
            matches = load_matches_from_db(conn)
        
        log.info(f"Partidos a procesar: {len(matches)}")
        
        if not matches:
            log.info("No hay partidos nuevos para procesar")
            return
        
        # Procesar partidos
        audit_data = []
        processed_count = 0
        ignored_count = 0
        
        for match in matches:
            result = engine.process_match(match)
            if result:
                audit_data.append(result)
                processed_count += 1
            else:
                ignored_count += 1
            
            # Log de progreso cada 1000 partidos
            if (processed_count + ignored_count) % 1000 == 0:
                log.info(f"Procesados {processed_count + ignored_count} partidos ({processed_count} válidos, {ignored_count} ignorados)")
        
        # Actualizar Elos generales
        engine.update_general_elos()
        
        # Guardar en base de datos
        log.info("Guardando resultados en base de datos...")
        save_elo_state_to_db(conn, engine, audit_data)
        
        log.info("=" * 70)
        log.info("CÁLCULO ELO COMPLETADO")
        log.info(f"Partidos procesados: {processed_count}")
        log.info(f"Partidos ignorados: {ignored_count}")
        log.info(f"Jugadores con Elo: {len(engine.players)}")
        log.info("=" * 70)
        
        # Mostrar top 10
        top_players = sorted(
            engine.players.values(),
            key=lambda p: p.elo_general,
            reverse=True
        )[:10]
        
        log.info("\nTOP 10 JUGADORES (Elo General):")
        for i, player in enumerate(top_players, 1):
            log.info(f"{i}. Player {player.player_id}: {player.elo_general:.1f}")
    
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Motor de cálculo Elo")
    parser.add_argument("--full-rebuild", action="store_true", help="Reconstruir todo desde cero")
    args = parser.parse_args()
    
    run_elo_calculation(full_rebuild=args.full_rebuild)