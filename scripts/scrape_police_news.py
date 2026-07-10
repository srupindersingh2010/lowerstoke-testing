#!/usr/bin/env python3
"""
Pulls Coventry policing stories for the weekly newsletter and writes them
to data/police_news.json.

Sources, in order:
  1. West Midlands Police official news RSS (usually 403-blocked for
     GitHub's servers, but tried first in case that ever changes).
  2. Google News RSS scoped to "West Midlands Police" + Coventry — reliable
     from GitHub Actions and aggregates WMP's own releases plus local
     outlets such as CoventryLive.

IMPORTANT: if no stories can be fetched, the existing police_news.json is
LEFT UNTOUCHED (the daily site scraper also maintains it), so the email
never loses its news section to a transient fetch failure.

Runs as a step in the newsletter workflow, right before
generate_newsletter.py. Uses only requests + the standard library.
"""

import datetime
import json
import re
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = ROOT / "data" / "police_news.json"

WMP_RSS = "https://www.westmidlands.police.uk/news/west-midlands/news/GetNewsRss/"
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=%22West%20Midlands%20Police%22%20Coventry%20when:7d"
    "&hl=en-GB&gl=GB&ceid=GB:en"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}
DAYS_BACK = 7


def fetch_feed():
    """Return (xml_text, is_google). Empty string if nothing worked."""
    try:
        r = requests.get(WMP_RSS, headers=HEADERS, timeout=20)
        print(f"WMP RSS -> {r.status_code}")
        if r.status_code == 200 and "<item" in r.text:
            return r.text, False
    except requests.RequestException as e:
        print(f"WMP RSS error: {e}")
    try:
        r = requests.get(GOOGLE_NEWS_RSS, headers=HEADERS, timeout=20)
        print(f"Google News RSS -> {r.status_code}")
        if r.status_code == 200 and "<item" in r.text:
            return r.text, True
    except requests.RequestException as e:
        print(f"Google News RSS error: {e}")
    return "", False


def parse_articles(xml_text, is_google):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DAYS_BACK)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%-d %B %Y at %H:%M")
    articles = []
    root = ET.fromstring(xml_text.encode("utf-8"))
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
        summary = re.sub(r"\s+", " ", summary)[:400]
        publisher = (item.findtext("source") or "").strip()
        if is_google and publisher and title.endswith(" - " + publisher):
            title = title[: -(len(publisher) + 3)].strip()
        try:
            pub = parsedate_to_datetime((item.findtext("pubDate") or "").strip())
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        if pub < cutoff:
            continue
        # WMP's force-wide feed needs a Coventry filter; the Google News
        # query is already scoped to Coventry.
        if not is_google and "coventry" not in f"{title} {summary}".lower():
            continue
        articles.append({
            "title": title,
            "link": link,
            "summary": summary,
            "published": pub.isoformat(),
            "published_display": pub.strftime("%-d %B %Y"),
            "source": publisher or "westmidlands.police.uk",
            "fetchedAt": stamp,
        })
    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles


def main():
    xml_text, is_google = fetch_feed()
    articles = []
    if xml_text:
        try:
            articles = parse_articles(xml_text, is_google)
        except Exception as e:
            print(f"Feed parse error: {e}")

    if articles:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(articles, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(articles)} Coventry police news item(s) from the last {DAYS_BACK} days to {OUTPUT_FILE}")
    elif OUTPUT_FILE.exists():
        print("No stories fetched — keeping the existing police_news.json (maintained by the daily site update).")
    else:
        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        print("No stories fetched and no existing file — wrote an empty list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
