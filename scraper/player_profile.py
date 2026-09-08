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

COUNTRY_TO_ISO = {
    "France": "FR", "Spain": "ES", "Germany": "DE", "Italy": "IT",
    "United States": "US", "USA": "US", "U.S.A.": "US",
    "Argentina": "AR", "Brazil": "BR", "Russia": "RU", "Serbia": "RS",
    "Switzerland": "CH", "Austria": "AT", "Czech Republic": "CZ", "Czechia": "CZ",
    "Australia": "AU", "Canada": "CA", "Japan": "JP", "China": "CN", "Chinese Taipei": "TW",
    "Great Britain": "GB", "United Kingdom": "GB", "England": "GB", "Scotland": "GB",
    "Poland": "PL", "Norway": "NO", "Denmark": "DK", "Sweden": "SE", "Finland": "FI",
    "Belgium": "BE", "Netherlands": "NL", "Croatia": "HR", "Chile": "CL", "Colombia": "CO",
    "Uruguay": "UY", "Peru": "PE", "Ecuador": "EC", "Mexico": "MX", "Portugal": "PT",
    "Greece": "GR", "Hungary": "HU", "Romania": "RO", "Bulgaria": "BG", "Georgia": "GE",
    "Kazakhstan": "KZ", "Ukraine": "UA", "Belarus": "BY", "Slovakia": "SK", "Slovenia": "SI",
    "South Korea": "KR", "South Africa": "ZA", "Tunisia": "TN", "Morocco": "MA", "Egypt": "EG",
    "India": "IN", "Cuba": "CU", "Dominican Republic": "DO", "Bolivia": "BO", "Paraguay": "PY",
    "Venezuela": "VE", "Turkey": "TR", "Israel": "IL", "Bosnia & Herzegovina": "BA",
    "Montenegro": "ME", "North Macedonia": "MK", "Lithuania": "LT", "Latvia": "LV",
    "Estonia": "EE", "Moldova": "MD", "New Zealand": "NZ", "Ireland": "IE",
    "Monaco": "MC", "Luxembourg": "LU", "Cyprus": "CY", "Malta": "MT",
    "Uzbekistan": "UZ", "Thailand": "TH", "Hong Kong": "HK"
}


def fetch_player_profile_html(slug: str) -> str:
    url = f"https://www.tennisexplorer.com/player/{slug}/"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_player_profile(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    profile_data = {}

    # 1. Foto del jugador
    img = soup.select_one("table.plDetail td.photo img") or soup.select_one("td.photo img")
    if img and img.get("src"):
        src = img["src"]
        if src.startswith("/"):
            src = f"https://www.tennisexplorer.com{src}"
        profile_data["photo_url"] = src

    # 2. Extraer información buscando DIRECTAMENTE los divs con clase "date" en la tabla de perfil
    # Esto evita el error de seleccionar la celda de la foto por accidente.
    date_divs = soup.select("table.plDetail div.date")
    
    for div in date_divs:
        text = div.get_text(strip=True)
        
        if "Country:" in text:
            profile_data["country"] = text.split("Country:", 1)[1].strip()
            
        elif "Height" in text and "Weight" in text:
            match = re.search(r"(\d+)\s*cm\s*/\s*(\d+)\s*kg", text)
            if match:
                profile_data["height_cm"] = int(match.group(1))
                profile_data["weight_kg"] = int(match.group(2))
                
        elif "Age:" in text:
            match = re.search(r"\((\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\)", text)
            if match:
                day, month, year = match.groups()
                profile_data["birth_date"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                
        elif "Current/Highest rank - singles:" in text:
            match = re.search(r"(\d+)\.\s*/\s*(\d+)\.", text)
            if match:
                profile_data["current_ranking"] = int(match.group(1))
                profile_data["career_high_ranking"] = int(match.group(2))
                
        elif "Plays:" in text:
            profile_data["plays"] = text.split("Plays:", 1)[1].strip().lower()

    # 3. Mapear país a código ISO (si se encontró el país)
    if "country" in profile_data:
        country_name = profile_data["country"]
        profile_data["flag_code"] = COUNTRY_TO_ISO.get(country_name)

    return profile_data