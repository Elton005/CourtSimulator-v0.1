"""
verify_tournament_slugs.py
Verifica que los slugs generados coinciden con los de Tennis Explorer.
Solo hace ~20 peticiones HTTP (muestreo aleatorio).
"""
import os
import re
import random
import psycopg2
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(override=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
}


def get_connection():
    return psycopg2.connect(os.environ["SUPABASE_DB_URL"])


def verify_sample(sample_size: int = 20):
    conn = get_connection()
    cur = conn.cursor()

    # Tomar una muestra aleatoria de partidos
    cur.execute(
        """
        SELECT m.source_match_id, t.name, t.source_tournament_id
        FROM matches m
        JOIN tournaments t ON m.tournament_id = t.id
        WHERE t.source_tournament_id IS NOT NULL
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (sample_size,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"\n{'='*80}")
    print(f"VERIFICACIÓN DE SLUGS (muestra de {sample_size} partidos)")
    print(f"{'='*80}\n")

    matches_ok = 0
    matches_fail = 0

    for source_match_id, db_name, db_slug in rows:
        try:
            url = f"https://www.tennisexplorer.com/match-detail/?id={source_match_id}"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")
            header_div = soup.find('h1', class_='bg')
            if header_div:
                link = header_div.find_next_sibling('div')
                if link:
                    a_tag = link.find('a')
                    if a_tag:
                        href = a_tag.get('href', '')
                        parts = href.strip('/').split('/')
                        te_slug = parts[0] if parts else None

                        if te_slug == db_slug:
                            status = "✅ OK"
                            matches_ok += 1
                        else:
                            status = f"❌ MISMATCH (TE: {te_slug})"
                            matches_fail += 1

                        print(f"  {status} | BD: {db_slug:<30} | DB name: {db_name}")
        except Exception as e:
            print(f"  ⚠️ ERROR | {source_match_id}: {e}")

        import time
        time.sleep(1)

    print(f"\n{'='*80}")
    print(f"RESULTADO: {matches_ok} OK, {matches_fail} MISMATCH de {sample_size}")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    verify_sample(20)