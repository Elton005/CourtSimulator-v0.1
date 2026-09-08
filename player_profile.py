"""
scraper/sources/tennisexplorer/player_profile.py

Extrae datos detallados del perfil de un jugador desde:
https://www.tennisexplorer.com/player/{slug}/
"""

import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
}

def fetch_player_profile_html(slug: str) -> str:
    url = f"https://www.tennisexplorer.com/player/{slug}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_player_profile(html: str) -> dict:
    """
    Extrae: country, height_cm, weight_kg, birth_date, current_ranking, 
    career_high_ranking, plays, photo_url.
    Nota: 'birthplace' y 'backhand' no están presentes en esta vista del HTML, 
    por lo que se omiten (quedarán NULL, lo cual es correcto).
    """
    soup = BeautifulSoup(html, "html.parser")
    profile_data = {}

    # 1. Extraer foto
    img = soup.select_one("table.plDetail td.photo img")
    if img and img.get("src"):
        profile_data["photo_url"] = f"https://www.tennisexplorer.com{img['src']}"

    # 2. Extraer bloques de texto <div class="date">
    profile_td = soup.select_one("table.plDetail td")
    if not profile_td:
        return profile_data

    date_divs = profile_td.find_all("div", class_="date")
    
    for div in date_divs:
        text = div.get_text(strip=True)
        
        if text.startswith("Country:"):
            profile_data["country"] = text.replace("Country:", "").strip()
            
        elif text.startswith("Height / Weight:"):
            match = re.search(r"(\d+)\s*cm\s*/\s*(\d+)\s*kg", text)
            if match:
                profile_data["height_cm"] = int(match.group(1))
                profile_data["weight_kg"] = int(match.group(2))
                
        elif text.startswith("Age:"):
            # Ej: "Age: 35 (18. 3. 1991)"
            match = re.search(r"\((\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\)", text)
            if match:
                day, month, year = match.groups()
                profile_data["birth_date"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                
        elif text.startswith("Current/Highest rank - singles:"):
            # Ej: "Current/Highest rank - singles: 286. / 36."
            match = re.search(r"(\d+)\.\s*/\s*(\d+)\.", text)
            if match:
                profile_data["current_ranking"] = int(match.group(1))
                profile_data["career_high_ranking"] = int(match.group(2))
                
        elif text.startswith("Plays:"):
            profile_data["plays"] = text.replace("Plays:", "").strip()

    return profile_data