#!/usr/bin/env python3
"""
Scrapes West Midlands Police's news search for Coventry-related stories
from the last 7 days and writes them to data/police_news.json.

Runs as a step in the newsletter workflow, right before
generate_newsletter.py, so the data is always fresh at send time.
"""

import datetime
import json
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = ROOT / "data" / "police_news.json"

SEARCH_URL = "https://www.westmidlands.police.uk/news/news-search/?q=Lower+Stoke+Coventry"
PAGINATED_URL = "https://www.westmidlands.police.uk/news/news-search/GetPaginatedResults/?q=Lower+Stoke+Coventry&page={page}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}

MAX_PAGES = 3  # safety cap so a scraper bug can't loop forever
DAYS_BACK = 7


def parse_articles(html):
    """Find article links + nearby title/summary/published-date text.
    Structure-based (link pattern + 'Published:' text) rather than CSS
    classes, since those are more likely to change under a CMS redesign.
    """
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen_links = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/news/west-midlands/news/news/" not in href:
            continue
        if href in seen_links:
            continue

        title_text = link.get_text(strip=True)
        if not title_text:
            continue  # this is probably the thumbnail image link, not the title link
        seen_links.add(href)

        # Walk up to a container that should also hold the "Published:" text
        container = link
        published_match = None
        for _ in range(5):
            if container is None:
                break
            container = container.parent
            if container is None:
                break
            text = container.get_text(" ", strip=True)
            m = re.search(r"Published:\s*(\d{2}:\d{2})\s*(\d{2}/\d{2}/\d{4})", text)
            if m:
                published_match = m
                break

        if not published_match:
            continue

        time_str, date_str = published_match.groups()
        try:
            published_dt = datetime.datetime.strptime(
                f"{date_str} {time_str}", "%d/%m/%Y %H:%M"
            )
        except ValueError:
            continue

        # Try to grab a one-line summary near the title (the <p> right after it)
        summary = ""
        next_p = link.find_next("p")
        if next_p:
            summary = next_p.get_text(strip=True)

        articles.append({
            "title": title_text,
            "link": href if href.startswith("http") else f"https://www.westmidlands.police.uk{href}",
            "summary": summary,
            "published": published_dt.isoformat(),
            "published_display": published_dt.strftime("%d %B %Y"),
        })

    return articles


def fetch_page(page_num):
    url = SEARCH_URL if page_num == 1 else PAGINATED_URL.format(page=page_num)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def main():
    cutoff = datetime.datetime.now() - datetime.timedelta(days=DAYS_BACK)
    all_articles = []
    seen_links = set()

    try:
        for page_num in range(1, MAX_PAGES + 1):
            html = fetch_page(page_num)
            page_articles = parse_articles(html)

            if not page_articles:
                break

            new_on_this_page = 0
            for a in page_articles:
                if a["link"] in seen_links:
                    continue
                seen_links.add(a["link"])
                all_articles.append(a)
                new_on_this_page += 1

            oldest_on_page = min(
                (datetime.datetime.fromisoformat(a["published"]) for a in page_articles),
                default=None,
            )
            # Stop once we've gone past the 7-day window, or the page had
            # nothing new (avoids infinite loop if pagination misbehaves)
            if oldest_on_page and oldest_on_page < cutoff:
                break
            if new_on_this_page == 0:
                break
    except requests.RequestException as e:
        print(f"Warning: could not fetch police news ({e}). Writing empty result.")
        all_articles = []

    recent = [
        a for a in all_articles
        if datetime.datetime.fromisoformat(a["published"]) >= cutoff
    ]
    recent.sort(key=lambda a: a["published"], reverse=True)

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(recent, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(recent)} police news item(s) from the last {DAYS_BACK} days to {OUTPUT_FILE}")


if __name__ == "__main__":
    sys.exit(main())
