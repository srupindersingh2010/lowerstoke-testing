#!/usr/bin/env python3
"""
Builds the weekly Lower Stoke newsletter (HTML email body + PDF
attachment) directly from the SAME data/*.json files your daily site
scraper (scrape.py) already produces — no manual editing required.

Run manually for testing:
    python scripts/generate_newsletter.py --dry-run

Environment variables required to actually send (set as GitHub Actions
secrets):
    SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD,
    FROM_ADDRESS, TO_ADDRESS
"""

import argparse
import datetime
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Template
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

MAX_NEWS_ITEMS = 4
MAX_CASEWORK_ITEMS = 4
MAX_POLICE_PRIORITIES = 4


def load_json(filename, default):
    """Load a data/*.json file, returning `default` if missing/unreadable."""
    path = DATA_DIR / filename
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def truncate(text, max_len=180):
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def clean_meeting_title(raw_title, fallback):
    """council_meetings.json titles look like 'Meeting ofCabineton 07/07 at ...'
    (using non-breaking spaces around the date). Pull out just the
    committee name."""
    normalized = (raw_title or "").replace("\xa0", " ")
    match = re.match(r"^Meeting of(.*?)on\s+\d{1,2}/\d{1,2}", normalized)
    if match:
        name = match.group(1).strip()
        if name:
            return name
    return fallback


# ---------------------------------------------------------------------
# Section builders — each returns a dict {heading, entries} or None if
# there's genuinely nothing to show this week.
# ---------------------------------------------------------------------

def build_news_section():
    items = load_json("news.json", [])
    if not items:
        return None
    entries = []
    for item in items[:MAX_NEWS_ITEMS]:
        entries.append({
            "title": item.get("title", "Untitled"),
            "detail": truncate(item.get("summary", "")),
            "meta": item.get("date", ""),
            "link": item.get("link"),
        })
    return {"heading": "This Week's Council News", "entries": entries}


def build_meetings_section():
    items = load_json("council_meetings.json", [])
    this_week = [m for m in items if m.get("withinWeek")]
    if not this_week:
        return None
    entries = []
    for m in this_week:
        name = clean_meeting_title(m.get("title", ""), "Council meeting")
        attendees = m.get("ourCouncillors") or []
        meta_bits = [f"{m.get('dayOfWeek', '')} {m.get('date', '')}", m.get("time", "")]
        meta = " · ".join(b for b in meta_bits if b)
        detail = f"Our councillor(s) attending: {', '.join(attendees)}" if attendees else None
        entries.append({
            "title": name,
            "detail": detail,
            "meta": meta,
            "link": m.get("agendaUrl"),
        })
    return {"heading": "Council Meetings This Week", "entries": entries}


def build_casework_section():
    items = load_json("casework.json", [])
    if not items:
        return None
    entries = []
    for item in items[:MAX_CASEWORK_ITEMS]:
        title = item.get("title", "Casework update")
        location = item.get("locationFocus")
        status = item.get("status", "")
        meta_bits = [location, status]
        meta = " · ".join(b for b in meta_bits if b)
        entries.append({
            "title": title,
            "detail": truncate(item.get("bodyText", "")),
            "meta": meta,
            "link": None,
        })
    return {"heading": "Councillor Casework Updates", "entries": entries}


def build_planning_section():
    items = load_json("planning.json", [])
    if not items:
        return None
    if items and items[0].get("siteDown"):
        return {
            "heading": "Planning Applications",
            "entries": [{
                "title": "The council's planning portal was temporarily unavailable when this was compiled",
                "detail": "Check directly for any new applications affecting Lower Stoke.",
                "meta": None,
                "link": items[0].get("sourceUrl"),
            }],
        }
    entries = []
    for item in items[:6]:
        entries.append({
            "title": item.get("address", item.get("reference", "Planning application")),
            "detail": truncate(item.get("description", "")),
            "meta": f"{item.get('reference', '')} · {item.get('status', '')}",
            "link": item.get("portalLink"),
        })
    return {"heading": "Planning Applications", "entries": entries}


def build_policing_section():
    crimes = load_json("police_crimes.json", [])
    events = load_json("police_events.json", [])
    team = load_json("police_team.json", [])
    entries = []

    for c in crimes[:MAX_POLICE_PRIORITIES]:
        entries.append({
            "title": f"{c.get('title', 'Priority')} — {c.get('issue', '')}",
            "detail": c.get("action", ""),
            "meta": c.get("status", ""),
            "link": c.get("sourceUrl"),
        })

    if events:
        for e in events[:3]:
            entries.append({
                "title": e.get("title", "Policing event"),
                "detail": e.get("description", ""),
                "meta": e.get("date", ""),
                "link": e.get("link") or e.get("sourceUrl"),
            })
    elif team:
        lead = team[0]
        entries.append({
            "title": f"No PACT meetings currently listed — contact {lead.get('name', 'the neighbourhood team')}",
            "detail": lead.get("bio", ""),
            "meta": lead.get("rank", ""),
            "link": lead.get("sourceUrl"),
        })

    if not entries:
        return None
    return {"heading": "Local Policing", "entries": entries}


def build_content():
    meta = load_json("meta.json", {})
    sections = []
    for builder in (
        build_news_section,
        build_meetings_section,
        build_casework_section,
        build_planning_section,
        build_policing_section,
    ):
        section = builder()
        if section:
            sections.append(section)

    return {
        "issue_title": "Lower Stoke Ward Newsletter",
        "issue_date": datetime.date.today().strftime("%d %B %Y"),
        "intro": (
            "Here's what's happening in Lower Stoke this week — pulled "
            "automatically from council news, meetings, casework, planning "
            "and policing updates."
        ),
        "sections": sections,
        "data_refreshed": meta.get("lastUpdated", "unknown"),
        "footer_note": (
            "You're receiving this because you subscribed to the Lower "
            "Stoke Ward newsletter. To unsubscribe, email "
            "lowerstokenewsletter+unsubscribe@googlegroups.com"
        ),
    }


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Arial, Helvetica, sans-serif; color: #222; max-width: 640px; margin: 0 auto; }
  h1 { color: #14532d; font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 12px; margin-top: 0; margin-bottom: 18px; }
  h2 { color: #14532d; font-size: 16px; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-top: 28px; }
  ul { padding-left: 0; list-style: none; }
  li { margin-bottom: 14px; border-left: 3px solid #e5e5e5; padding-left: 10px; }
  li a.item-title { color: #1a5276; text-decoration: none; font-weight: bold; font-size: 14px; }
  li .item-title-plain { font-weight: bold; font-size: 14px; }
  .item-meta { color: #888; font-size: 11px; margin: 2px 0; }
  .item-detail { font-size: 13px; color: #444; margin: 2px 0 0 0; }
  .intro { font-size: 14px; margin-bottom: 20px; }
  .footer { font-size: 11px; color: #777; margin-top: 32px; border-top: 1px solid #eee; padding-top: 12px; }
</style>
</head>
<body>
  <h1>{{ issue_title }}</h1>
  <p class="subtitle">{{ issue_date }} &middot; data refreshed {{ data_refreshed }}</p>
  <p class="intro">{{ intro }}</p>

  {% for section in sections %}
  <h2>{{ section.heading }}</h2>
  <ul>
    {% for item in section.entries %}
    <li>
      {% if item.link %}
      <a class="item-title" href="{{ item.link }}">{{ item.title }}</a>
      {% else %}
      <div class="item-title-plain">{{ item.title }}</div>
      {% endif %}
      {% if item.meta %}<div class="item-meta">{{ item.meta }}</div>{% endif %}
      {% if item.detail %}<div class="item-detail">{{ item.detail }}</div>{% endif %}
    </li>
    {% endfor %}
  </ul>
  {% endfor %}

  <div class="footer">{{ footer_note }}</div>
</body>
</html>
"""


def render_html(content):
    return Template(HTML_TEMPLATE).render(**content)


def build_pdf(html_str, out_path):
    HTML(string=html_str).write_pdf(str(out_path))


def send_email(html_str, pdf_path, content):
    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    from_address = os.environ["FROM_ADDRESS"]
    to_address = os.environ["TO_ADDRESS"]

    msg = EmailMessage()
    msg["Subject"] = f"{content['issue_title']} — {content['issue_date']}"
    msg["From"] = from_address
    msg["To"] = to_address
    msg.set_content("This email requires an HTML-capable email client to view.")
    msg.add_alternative(html_str, subtype="html")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(), maintype="application", subtype="pdf", filename=pdf_path.name,
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
        server.login(smtp_username, smtp_password)
        server.send_message(msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    content = build_content()
    html_str = render_html(content)

    pdf_path = OUTPUT_DIR / f"newsletter-{datetime.date.today().isoformat()}.pdf"
    build_pdf(html_str, pdf_path)
    html_path = OUTPUT_DIR / f"newsletter-{datetime.date.today().isoformat()}.html"
    html_path.write_text(html_str, encoding="utf-8")

    print(f"Built {pdf_path} and {html_path}")
    print(f"Sections included: {[s['heading'] for s in content['sections']]}")

    if args.dry_run:
        print("Dry run — email not sent.")
        return

    send_email(html_str, pdf_path, content)
    print(f"Newsletter sent to {os.environ.get('TO_ADDRESS')}")


if __name__ == "__main__":
    sys.exit(main())
