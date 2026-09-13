"""
scraper/notifications.py
Envía notificaciones a Telegram con resúmenes de la base de datos.
"""

import os
import requests
import psycopg2
from datetime import datetime, timezone


def get_connection():
    return psycopg2.connect(os.environ["SUPABASE_DB_URL"])


def get_daily_summary() -> dict:
    """Consulta la BD para generar un resumen del día."""
    conn = get_connection()
    cur = conn.cursor()
    
    summary = {}
    
    # Partidos finalizados hoy
    cur.execute("""
        SELECT COUNT(*) FROM matches 
        WHERE match_date = CURRENT_DATE 
        AND match_status = 'finished'
    """)
    summary["finished_today"] = cur.fetchone()[0]
    
    # Partidos programados para mañana
    cur.execute("""
        SELECT COUNT(*) FROM matches 
        WHERE match_date = CURRENT_DATE + INTERVAL '1 day'
        AND match_status = 'scheduled'
    """)
    summary["scheduled_tomorrow"] = cur.fetchone()[0]
    
    # Torneos activos hoy
    cur.execute("""
        SELECT COUNT(DISTINCT t.id) 
        FROM matches m 
        JOIN tournaments t ON m.tournament_id = t.id
        WHERE m.match_date = CURRENT_DATE
    """)
    summary["active_tournaments"] = cur.fetchone()[0]
    
    # Top 3 torneos con más partidos hoy
    cur.execute("""
        SELECT t.name, COUNT(*) as partidos
        FROM matches m
        JOIN tournaments t ON m.tournament_id = t.id
        WHERE m.match_date = CURRENT_DATE
        GROUP BY t.name
        ORDER BY partidos DESC
        LIMIT 3
    """)
    summary["top_tournaments"] = cur.fetchall()
    
    # Total histórico
    cur.execute("SELECT COUNT(*) FROM matches")
    summary["total_matches"] = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM players")
    summary["total_players"] = cur.fetchone()[0]
    
    cur.close()
    conn.close()
    return summary


def send_telegram_message(message: str) -> bool:
    """Envía un mensaje al canal de Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    
    if not token or not channel_id:
        print("⚠️ Faltan credenciales de Telegram")
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
        return True
    except Exception as e:
        print(f"❌ Error enviando a Telegram: {e}")
        return False


def build_update_message(summary: dict, update_type: str) -> str:
    """Construye el mensaje HTML para Telegram."""
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    
    # Emoji según el tipo de actualización
    emojis = {
        "06:00": "🌅",  # Asia/Oceanía
        "12:00": "☀️",  # Europa mañana
        "18:00": "🌇",  # Europa tarde / América
        "00:00": "🌙",  # América noche
    }
    emoji = emojis.get(update_type, "🎾")
    
    # Top torneos formateados
    top_tournaments = ""
    for name, count in summary["top_tournaments"]:
        top_tournaments += f"  • {name}: {count} partidos\n"
    
    message = f"""
{emoji} <b>CourtSimulator Update</b>
📅 {now}

<b>📊 Resumen del día:</b>
✅ Partidos finalizados: <b>{summary['finished_today']}</b>
📅 Programados mañana: <b>{summary['scheduled_tomorrow']}</b>
🏆 Torneos activos: <b>{summary['active_tournaments']}</b>

<b>🔥 Top torneos hoy:</b>
{top_tournaments if top_tournaments else '  • Sin torneos activos'}

<b>📈 Base de datos:</b>
🎾 Total partidos: <b>{summary['total_matches']:,}</b>
👤 Total jugadores: <b>{summary['total_players']:,}</b>

<i>Próxima actualización en 6 horas</i>
""".strip()
    
    return message


def send_daily_update(update_type: str = None):
    """Función principal: genera y envía la actualización."""
    if not update_type:
        now_hour = datetime.now(timezone.utc).hour
        if now_hour < 9:
            update_type = "06:00"
        elif now_hour < 15:
            update_type = "12:00"
        elif now_hour < 21:
            update_type = "18:00"
        else:
            update_type = "00:00"
    
    summary = get_daily_summary()
    message = build_update_message(summary, update_type)
    success = send_telegram_message(message)
    
    if success:
        print(f"✅ Notificación enviada a Telegram ({update_type})")
    else:
        print("❌ Falló el envío a Telegram")
    
    return success


if __name__ == "__main__":
    send_daily_update()