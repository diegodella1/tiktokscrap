import logging
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

RSS_NS = {"ht": "https://trends.google.com/trending/rss"}
BASE_URL = "https://trends.google.com/trending/rss"


def _normalize_country(country_code):
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError("country_code debe ser un código ISO de 2 letras")
    return code


def _first_news_item(item, tag_name):
    news_item = item.find("ht:news_item", RSS_NS)
    if news_item is None:
        return None
    child = news_item.find(f"ht:{tag_name}", RSS_NS)
    return child.text.strip() if child is not None and child.text else None


def fetch_trending_searches(country_code, hours=4, sort="search-volume", limit=10):
    code = _normalize_country(country_code)
    params = {
        "geo": code,
        "hours": int(hours),
        "sort": sort,
    }
    resp = requests.get(BASE_URL, params=params, timeout=20)
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Google Trends RSS no devolvió channel")

    trends = []
    for item in channel.findall("item")[:limit]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        approx_traffic = item.findtext("ht:approx_traffic", default="", namespaces=RSS_NS).strip()
        pub_date = item.findtext("pubDate")
        started_at = None
        if pub_date:
            try:
                started_at = parsedate_to_datetime(pub_date).isoformat()
            except (TypeError, ValueError):
                started_at = None

        news_url = _first_news_item(item, "news_item_url")
        news_source = _first_news_item(item, "news_item_source")
        news_title = _first_news_item(item, "news_item_title")

        trends.append({
            "query": title,
            "country_code": code,
            "approx_traffic": approx_traffic or "N/A",
            "search_surge": f"Trending now - last {hours}h",
            "started_at": started_at,
            "link": news_url or f"https://trends.google.com/trending?geo={code}&hours={hours}&sort={sort}",
            "news_source": news_source,
            "news_title": news_title,
        })

    logger.info("Google Trends fetched %s items for %s", len(trends), code)
    return trends
