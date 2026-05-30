"""
parser.py — парсер House.kg и Lalafo + Claude классификатор

House.kg: https://www.house.kg/kupit-kvartiru?region=1&town=2&sort_by=upped_at+desc
Lalafo:   https://lalafo.kg/bishkek/kvartiry/prodam-kvartiru
"""

import re
import asyncio
import logging
import httpx
from bs4 import BeautifulSoup

from database import (
    save_listing, get_unclassified, update_classification,
    register_phone, register_photo_hashes,
)
from defense import classify_text, normalize_phone
from photo_guard import phash_from_url

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _price_usd(text: str):
    text = text.replace(" ", "").replace("\xa0", "").replace(",", "")
    m = re.search(r"\$(\d+)|(\d+)\$|(\d+)USD", text)
    if m:
        return int(m.group(1) or m.group(2) or m.group(3))
    m2 = re.search(r"(\d+)(?:сом|som|KGS|kgs)", text, re.I)
    if m2:
        return int(m2.group(1)) // 89
    return None

def _rooms(text: str):
    m = re.search(r"(\d)\s*[-–]?\s*комн", text, re.I)
    if m: return int(m.group(1))
    m2 = re.search(r"(\d)\s*-\s*к\b", text, re.I)
    if m2: return int(m2.group(1))
    return None

def _district(text: str):
    for d in ["Свердловский","Октябрьский","Ленинский","Первомайский","Аламединский"]:
        if d.lower() in text.lower(): return d
    return ""

def _area(text: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:кв\.?\s*м|m²|кв\.м)", text, re.I)
    return float(m.group(1).replace(",",".")) if m else None

def _phone(text: str):
    m = re.search(r"(\+?996[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}[\s\-]?\d{2})", text)
    if m: return m.group(1).strip()
    m2 = re.search(r"(0\d{9})", text)
    if m2: return m2.group(1).strip()
    return ""


async def parse_house_kg(pages: int = 2) -> list[dict]:
    listings = []
    base = "https://www.house.kg"
    url_template = base + "/kupit-kvartiru?region=1&town=2&sort_by=upped_at+desc&page={page}"

    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for page in range(1, pages + 1):
            url = url_template.format(page=page)
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                # Ищем карточки объявлений
                cards = soup.select("div.listing, article.listing, .listing-card, [class*='listing-item']")

                # Запасной вариант через ссылки
                if not cards:
                    links = soup.select("a[href]")
                    links = [l for l in links if re.search(r"/\d{4,}", l.get("href",""))]
                    for link in links[:50]:
                        href = link.get("href","")
                        full_url = href if href.startswith("http") else base + href
                        eid = re.search(r"/(\d{4,})", href)
                        if not eid: continue
                        text = link.get_text(separator=" ", strip=True)
                        if len(text) < 5: continue
                        listings.append({
                            "source":"house_kg","external_id":eid.group(1),
                            "url":full_url,"title":text[:200],"description":"",
                            "price_usd":_price_usd(text),"district":_district(text),
                            "rooms":_rooms(text),"area_sqm":_area(text),
                            "floor":"","phone":"","phone_norm":"",
                        })
                    logger.info(f"house.kg page {page}: {len(listings)} через ссылки")
                    await asyncio.sleep(2)
                    continue

                for card in cards:
                    try:
                        link_el = card.select_one("a[href]")
                        if not link_el: continue
                        href = link_el["href"]
                        full_url = href if href.startswith("http") else base + href
                        eid = re.search(r"/(\d{4,})", href)
                        if not eid: continue
                        title_el = card.select_one("h2,h3,[class*='title'],[class*='name']")
                        price_el = card.select_one("[class*='price'],[class*='cost']")
                        desc_el  = card.select_one("[class*='desc'],[class*='info'],p")
                        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
                        desc  = desc_el.get_text(strip=True)  if desc_el  else ""
                        price_text = price_el.get_text(strip=True) if price_el else ""
                        phone = _phone(card.get_text())
                        listings.append({
                            "source":"house_kg","external_id":eid.group(1),
                            "url":full_url,"title":title[:200],"description":desc[:500],
                            "price_usd":_price_usd(price_text or title),
                            "district":_district(title+" "+desc),"rooms":_rooms(title+" "+desc),
                            "area_sqm":_area(title+" "+desc),"floor":"",
                            "phone":phone,"phone_norm":normalize_phone(phone) if phone else "",
                        })
                    except Exception as e:
                        logger.debug(f"house.kg card: {e}")
                logger.info(f"house.kg page {page}: {len(cards)} карточек")
            except Exception as e:
                logger.warning(f"house.kg page {page}: {e}")
            await asyncio.sleep(2)

    logger.info(f"house.kg итого: {len(listings)}")
    return listings


async def parse_lalafo(pages: int = 2) -> list[dict]:
    listings = []
    base = "https://lalafo.kg"
    url_template = base + "/bishkek/kvartiry/prodam-kvartiru?page={page}"

    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for page in range(1, pages + 1):
            url = url_template.format(page=page)
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                cards = soup.select("article,.feed-item,[class*='AdItem'],[class*='ad-item']")

                if not cards:
                    links = soup.select("a[href]")
                    links = [l for l in links if re.search(r"/\d{4,}", l.get("href",""))]
                    for link in links[:50]:
                        href = link.get("href","")
                        full_url = href if href.startswith("http") else base + href
                        eid = re.search(r"/(\d{4,})", href)
                        if not eid: continue
                        text = link.get_text(separator=" ", strip=True)
                        if len(text) < 5: continue
                        listings.append({
                            "source":"lalafo","external_id":eid.group(1),
                            "url":full_url,"title":text[:200],"description":"",
                            "price_usd":_price_usd(text),"district":_district(text),
                            "rooms":_rooms(text),"area_sqm":_area(text),
                            "floor":"","phone":"","phone_norm":"",
                        })
                    await asyncio.sleep(2)
                    continue

                for card in cards:
                    try:
                        link_el = card.select_one("a[href]")
                        if not link_el: continue
                        href = link_el["href"]
                        full_url = href if href.startswith("http") else base + href
                        eid = re.search(r"/(\d{4,})", href)
                        if not eid: continue
                        title_el = card.select_one("h2,h3,[class*='title'],[class*='Title']")
                        price_el = card.select_one("[class*='price'],[class*='Price']")
                        desc_el  = card.select_one("[class*='desc'],[class*='Desc'],p")
                        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
                        desc  = desc_el.get_text(strip=True)  if desc_el  else ""
                        price_text = price_el.get_text(strip=True) if price_el else ""
                        phone = _phone(card.get_text())
                        listings.append({
                            "source":"lalafo","external_id":eid.group(1),
                            "url":full_url,"title":title[:200],"description":desc[:500],
                            "price_usd":_price_usd(price_text or title),
                            "district":_district(title+" "+desc),"rooms":_rooms(title+" "+desc),
                            "area_sqm":_area(title+" "+desc),"floor":"",
                            "phone":phone,"phone_norm":normalize_phone(phone) if phone else "",
                        })
                    except Exception as e:
                        logger.debug(f"lalafo card: {e}")
                logger.info(f"lalafo page {page}: {len(cards)} карточек")
            except Exception as e:
                logger.warning(f"lalafo page {page}: {e}")
            await asyncio.sleep(2)

    logger.info(f"lalafo итого: {len(listings)}")
    return listings


async def run_pipeline(pages: int = 2):
    logger.info("▶ Запуск пайплайна")
    house  = await parse_house_kg(pages)
    lalafo = await parse_lalafo(pages)
    all_listings = house + lalafo

    new_ids = []
    for raw in all_listings:
        lid = save_listing(raw)
        if lid:
            new_ids.append(lid)
            if raw.get("phone_norm"):
                register_phone(raw["phone_norm"], lid)

    logger.info(f"Новых: {len(new_ids)}")

    unclassified = get_unclassified(limit=30)
    owners = 0
    for row in unclassified:
        result = classify_text(dict(row))
        update_classification(row["id"], result["is_owner"], result["confidence"], result["reason"])
        if result["is_owner"]: owners += 1
        await asyncio.sleep(0.4)

    logger.info(f"Классифицировано: {len(unclassified)}, собственников: {owners}")
    logger.info("✅ Пайплайн завершён")
