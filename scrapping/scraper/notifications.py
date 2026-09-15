"""
scraper/notifications.py
Envía notificaciones a Telegram con resúmenes de la base de datos.
"""
import os
import requests
import psycopg2
from datetime import datetime, timezone

def get_connection():
    db_url = os.environ.get("SUPABASE_DB_URL")
    # Blindaje: Si está vacía, fallamos inmediatamente con un mensaje claro
    if not db_url or db_url.strip() == "":
        raise RuntimeError("❌ ERROR CRÍTICO: SUPABASE_DB_URL está vacía. Revisa los Secrets de GitHub.")
    return psycopg2.connect(db_url)

def get_daily_summary() -> dict:
    conn = get_connection()
    cur = conn.cursor()
    summary = {}
    
    cur.execute("SELECT COUNT(*) FROM matches WHERE match_date = CURRENT_DATE AND match_status = 'finished'")
    summary["finished_today"] = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM matches WHERE match_date IN (CURRENT_DATE, CURRENT_DATE + INTERVAL '1 day') AND match_status = 'scheduled'")
    summary["scheduled_upcoming"] = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(DISTINCT t.id) FROM matches m JOIN tournaments t ON m.tournament_id = t.id WHERE m.match_date = CURRENT_DATE")
    summary["active_tournaments"] = cur.fetchone()[0]
    
    cur.execute("SELECT t.name, COUNT(*) FROM matches m JOIN tournaments t ON m.tournament_id = t.id WHERE m.match_date = CURRENT_DATE GROUP BY t.name ORDER BY COUNT(*) DESC LIMIT 3")
    summary["top_tournaments"] = cur.fetchall()
    
    cur.execute("SELECT COUNT(*) FROM matches")
    summary["total_matches"] = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM players WHERE canonical_name IS NOT NULL")
    summary["total_players"] = cur.fetchone()[0]
    
    cur.close()
    conn.close()
    return summary

def send_telegram_message(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    
    if not token or not channel_id:
        print("⚠️ Faltan credenciales de Telegram en las variables de entorno")
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": channel_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print("✅ Notificación enviada a Telegram exitosamente")
        return True
    except Exception as e:
        print(f"❌ Error enviando a Telegram: {e}")
        return False

def build_update_message(summary: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    top_tournaments = "".join([f"  🏆 <b>{name}</b>: {count} partidos\n" for name, count in summary["top_tournaments"]])
    if not top_tournaments:
        top_tournaments = "  • Sin torneos activos hoy"

    return f"""
🎾 <b>CourtSimulator Update</b>
📅 <i>{now}</i>

<b>📊 Resumen del Día:</b>
✅ Partidos finalizados: <b>{summary['finished_today']}</b>
📅 Próximos partidos (hoy/mañana): <b>{summary['scheduled_upcoming']}</b>
🌍 Torneos activos: <b>{summary['active_tournaments']}</b>

<b>🔥 Top Torneos:</b>
{top_tournaments}
<b>📈 Base de Datos Histórica:</b>
🎾 Total partidos: <b>{summary['total_matches']:,}</b>
👤 Jugadores registrados: <b>{summary['total_players']:,}</b>

<i>🔄 Próxima actualización en 6 horas. ¡Mantente atento!</i>
""".strip()

def send_daily_update():
    try:
        summary = get_daily_summary()
        message = build_update_message(summary)
        success = send_telegram_message(message)
        return 0 if success else 1
    except Exception as e:
        print(f"❌ Error crítico en send_daily_update: {e}")
        return 1

if __name__ == "__main__":
    exit(send_daily_update())