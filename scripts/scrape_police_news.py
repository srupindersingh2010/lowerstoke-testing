#!/usr/bin/env python3
"""
Pulls Coventry-related stories from West Midlands Police's official news
RSS feed, filtered to the last 7 days, and writes them to
data/police_news.json.

The feed covers the whole West Midlands force area, so we filter for
items that mention Coventry in the title or summary. Runs as a step in
the newsletter workflow, right before generate_newsletter.py.
"""

import datetime
import json
import sys
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = ROOT / "data" / "police_news.json"

RSS_URL = "https://www.westmidlands.police.uk/news/west-midlands/news/GetNewsRss/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}

DAYS_BACK = 7
KEYWORD = "coventry"  # case-insensitive match against title + summary


def fetch_feed_text():
    resp = requests.get(RSS_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def main():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DAYS_BACK)
    articles = []

    try:
        feed_text = fetch_feed_text()
        parsed = feedparser.parse(feed_text)

        for entry in parsed.entries:
            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            link = getattr(entry, "link", "")

            if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
                continue
            published_dt = datetime.datetime(
                *entry.published_parsed[:6], tzinfo=datetime.timezone.utc
            )

            if published_dt < cutoff:
                continue

            haystack = f"{title} {summary}".lower()
            if KEYWORD not in haystack:
                continue

            articles.append({
                "title": title,
                "link": link,
                "summary": summary,
                "published": published_dt.isoformat(),
                "published_display": published_dt.strftime("%d %B %Y"),
            })

    except requests.RequestException as e:
        print(f"Warning: could not fetch police news RSS feed ({e}). Writing empty result.")
        articles = []

    articles.sort(key=lambda a: a["published"], reverse=True)

    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(articles)} Coventry police news item(s) from the last {DAYS_BACK} days to {OUTPUT_FILE}")


if __name__ == "__main__":
    sys.exit(main())
