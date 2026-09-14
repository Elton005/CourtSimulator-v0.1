"""
scripts/backfill_elo.py
Script para procesar todos los partidos históricos y calcular el Elo desde cero.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from elo_engine import run_elo_calculation

if __name__ == "__main__":
    print("⚠️  ADVERTENCIA: Esto reconstruirá todo el Elo desde cero.")
    print("   Los datos actuales serán reemplazados.")
    response = input("¿Continuar? (yes/no): ")
    
    if response.lower() == "yes":
        run_elo_calculation(full_rebuild=True)
    else:
        print("Operación cancelada.")