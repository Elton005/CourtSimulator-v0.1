"""
fix_tournament_slugs.py
Script para corregir los source_tournament_id incorrectos en la base de datos.
Ejecutar DESPUÉS de corregir la función slugify_tournament en pipeline_run.py.
"""

import os
import re
import unicodedata
import psycopg2
from dotenv import load_dotenv

load_dotenv(override=True)


def slugify_tournament(name: str) -> str:
    """Versión corregida de slugify (misma que en pipeline_run.py)"""
    s = unicodedata.normalize('NFKD', name)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = s.replace('-', ' ')
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = s.replace(' ', '-')
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s


def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise RuntimeError("Falta la variable de entorno SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def fix_tournament_slugs():
    """
    Recorre todos los torneos y actualiza los source_tournament_id incorrectos.
    """
    conn = get_connection()
    cur = conn.cursor()

    print("=" * 80)
    print("CORRECCIÓN DE SLUGS DE TORNEOS")
    print("=" * 80)
    print()

    # Obtener todos los torneos
    cur.execute("SELECT id, name, source_tournament_id FROM tournaments")
    tournaments = cur.fetchall()

    print(f"Total de torneos en la base de datos: {len(tournaments)}")
    print()

    updated_count = 0
    mismatch_count = 0

    for tournament_id, name, current_slug in tournaments:
        # Calcular el slug correcto
        correct_slug = slugify_tournament(name)

        # Si el slug actual es diferente al correcto, actualizarlo
        if current_slug != correct_slug:
            mismatch_count += 1
            print(f"❌ MISMATCH encontrado:")
            print(f"   ID: {tournament_id}")
            print(f"   Nombre: {name}")
            print(f"   Slug actual: {current_slug}")
            print(f"   Slug correcto: {correct_slug}")
            print()

            # Actualizar en la base de datos
            cur.execute(
                "UPDATE tournaments SET source_tournament_id = %s WHERE id = %s",
                (correct_slug, tournament_id)
            )
            updated_count += 1

    # Confirmar los cambios
    conn.commit()

    print("=" * 80)
    print(f"RESUMEN:")
    print(f"  Torneos con slug incorrecto: {mismatch_count}")
    print(f"  Torneos actualizados: {updated_count}")
    print("=" * 80)

    cur.close()
    conn.close()


if __name__ == "__main__":
    fix_tournament_slugs()