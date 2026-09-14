"""
scripts/update_elo_incremental.py
Script para actualizar el Elo solo con partidos nuevos.
Se ejecuta diariamente después del scraping.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elo_engine import run_elo_calculation

if __name__ == "__main__":
    run_elo_calculation(full_rebuild=False)