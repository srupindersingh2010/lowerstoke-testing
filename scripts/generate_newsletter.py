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
import base64
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
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "output"

MAX_NEWS_ITEMS = 4
MAX_CASEWORK_ITEMS = 4
MAX_POLICE_PRIORITIES = 4

# ---------------------------------------------------------------------
# Fixed "every edition" content — councillor contacts, mission statement,
# street surgery details, subscribe info, and the promoter statement.
# Edit these directly in this file if any details change (new mobile
# number, new photo file, etc.) — this content does NOT come from
# data/*.json since it rarely changes and should appear in every issue.
# ---------------------------------------------------------------------

MISSION_STATEMENT = (
    "We are committed to being visible, accessible, and accountable — "
    "working alongside residents and standing up for Lower Stoke."
)

MISSION_BULLETS = [
    "Regular Street Surgery",
    "Door-to-door listening",
    "Contact us: phone, WhatsApp, email, social media or in person.",
]

GREETING_TEXT = (
    "Greetings from Rupinder, John and Shahnaz, your Lower Stoke "
    "Councillor Team. We hope this newsletter finds you well."
)

# Longer, warmer version shown at the very top, just under the masthead —
# distinct styling from the rest of the newsletter. Each item pairs a
# short lead-in line with its bullet point, edit freely.
TOP_INTRO_LEAD = (
    "Rupinder Singh, John McNicholas and Shahnaz Akhter are committed to "
    "being visible, accessible, and accountable — working alongside "
    "residents and standing up for Lower Stoke. You can contact us in "
    "many ways — we're always happy to hear from you."
)

TOP_INTRO_ITEMS = [
    {
        "lead_in": "That's why we hold:",
        "bullet": "Regular Street Surgery",
    },
    {
        "lead_in": "Through our all-year-round campaign, we also do:",
        "bullet": "Door-to-door listening",
    },
    {
        "lead_in": "Because our residents are busy people, we're keen to hear from you by:",
        "bullet": "Phone, WhatsApp, email, social media, or in person",
    },
]

# NOTE: photo file names below are best-guess labels based on the order
# photos appeared in the source file — please confirm each photo matches
# the right councillor and rename the files in /assets if not, then
# update the "photo" values below to match.
COUNCILLORS = [
    {
        "name": "Dr. Shahnaz Akhter",
        "email": "shahnaz.akhter@coventry.gov.uk",
        "twitter": "@Covlabourparty",
        "facebook": "Facebook.com/shahnaz.labour",
        "mobile": None,
        "photo": "councillor_1_shahnaz_akhter.jpg",
    },
    {
        "name": "Cllr. John McNicholas",
        "email": "john.mcnicholas@coventry.gov.uk",
        "twitter": "@CllrJMcNicholas",
        "facebook": "Facebook.com/JMcN1CH",
        "mobile": "07968 498860",
        "photo": "councillor_2_john_mcnicholas.jpg",
    },
    {
        "name": "Cllr. Rupinder Singh",
        "email": "rupinder.singh@coventry.gov.uk",
        "twitter": "@Rupinder2010",
        "facebook": "Facebook.com/singh.rup",
        "mobile": "07960 962642",
        "photo": "councillor_3_rupinder_singh.jpg",
    },
]

STREET_SURGERY_NOTE = (
    "Come and talk to us: 10:00 am to 11:00 am, 1st and 3rd Saturday of "
    "the month, opposite Iceland / Lidl on Binley Road."
)

UNIFIED_EMAIL_NOTE = (
    "Unified email for Lower Stoke, which reaches all three councillors: "
    "lower-stoke-councillors@googlegroups.com"
)

WEBSITE_NOTE = "Visit the Lower Stoke Ward website: www.lowerstoke.co.uk"

SUBSCRIBE_NOTE = (
    "To get this newsletter in your inbox every week, send a blank "
    "email (no subject) to lowerstokenewsletter+subscribe@googlegroups.com"
)

PROMOTED_BY_NOTE = (
    "Promoted by Rupinder Singh on behalf of Lower Stoke Labour Party, "
    "all at 90 Short Street, Coventry."
)


def image_to_data_uri(filename):
    """Reads an image from /assets and returns it as a base64 data URI,
    so it's embedded directly in both the PDF and the email — no
    external image hosting needed, and it always travels with the file."""
    path = ASSETS_DIR / filename
    if not path.exists():
        return None
    ext = path.suffix.lower().lstrip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def build_static_info():
    councillors = []
    for c in COUNCILLORS:
        councillors.append({**c, "photo_data_uri": image_to_data_uri(c["photo"])})

    phones = [
        {"name": c["name"], "mobile": c["mobile"]}
        for c in COUNCILLORS if c.get("mobile")
    ]

    return {
        "mission_statement": MISSION_STATEMENT,
        "mission_bullets": MISSION_BULLETS,
        "greeting_text": GREETING_TEXT,
        "top_intro_lead": TOP_INTRO_LEAD,
        "top_intro_items": TOP_INTRO_ITEMS,
        "councillor_phones": phones,
        "councillors": councillors,
        "street_surgery_note": STREET_SURGERY_NOTE,
        "unified_email_note": UNIFIED_EMAIL_NOTE,
        "website_note": WEBSITE_NOTE,
        "subscribe_note": SUBSCRIBE_NOTE,
        "promoted_by_note": PROMOTED_BY_NOTE,
    }


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


def build_police_priorities_section():
    """Current Stoke & Wyken neighbourhood policing priorities — the
    issue/action statements the local team publishes (data/police_priorities.json,
    maintained by the daily site update from the official data.police.uk API)."""
    items = load_json("police_priorities.json", [])
    if not items:
        return None
    entries = []
    for item in items[:4]:
        issue = item.get("issue", "").strip()
        if not issue:
            continue
        meta = "Neighbourhood Priority"
        if item.get("issueDate"):
            meta += f" · issued {item['issueDate']}"
        entries.append({
            "title": issue if len(issue) <= 120 else issue[:117] + "...",
            "detail": truncate(item.get("action", "")),
            "meta": meta,
            "link": item.get("sourceUrl"),
        })
    if not entries:
        return None
    return {"heading": "Policing Priorities in Stoke & Wyken", "entries": entries}


def build_police_news_section():
    items = load_json("police_news.json", [])
    if not items:
        return None
    entries = []
    for item in items[:6]:
        entries.append({
            "title": item.get("title", "Police news"),
            "detail": truncate(item.get("summary", "")),
            "meta": item.get("published_display", ""),
            "link": item.get("link"),
        })
    return {"heading": "Coventry Police News (Last 7 Days)", "entries": entries}


def build_content():
    meta = load_json("meta.json", {})
    sections = []
    for builder in (
        build_news_section,
        build_meetings_section,
        build_casework_section,
        build_planning_section,
        build_policing_section,
        build_police_priorities_section,
        build_police_news_section,
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
        "static_info": build_static_info(),
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
  .greeting { font-family: Georgia, 'Times New Roman', serif; font-size: 17px; color: #14532d; font-weight: bold; margin: 4px 0 14px 0; line-height: 1.4; }
  h2 { color: #14532d; font-size: 16px; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-top: 28px; }
  ul { padding-left: 0; list-style: none; }
  li { margin-bottom: 14px; border-left: 3px solid #e5e5e5; padding-left: 10px; }
  li a.item-title { color: #1a5276; text-decoration: none; font-weight: bold; font-size: 14px; }
  li .item-title-plain { font-weight: bold; font-size: 14px; }
  .item-meta { color: #888; font-size: 11px; margin: 2px 0; }
  .item-detail { font-size: 13px; color: #444; margin: 2px 0 0 0; }
  .intro { font-size: 14px; margin-bottom: 20px; }
  .footer { font-size: 11px; color: #777; margin-top: 32px; border-top: 1px solid #eee; padding-top: 12px; }

  .mission-banner { background: #f4f8f5; border-left: 4px solid #14532d; padding: 14px 16px; margin: 24px 0; font-size: 13px; }
  .mission-banner p { margin: 0 0 8px 0; font-style: italic; color: #333; }
  .mission-banner ul { list-style: none; padding: 0; margin: 0; }
  .mission-banner li { margin-bottom: 4px; color: #14532d; font-weight: bold; }
  .mission-banner .phones { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #ccd8cf; font-style: normal; color: #333; }
  .mission-banner .phones strong { color: #14532d; }

  .top-welcome { font-family: Georgia, 'Times New Roman', serif; background: #fdf6ec; border: 1px solid #e8d9b8; border-radius: 6px; padding: 16px 18px; margin: 16px 0 22px 0; }
  .top-welcome p.lead { font-size: 14px; color: #7a1f3d; margin: 0 0 12px 0; line-height: 1.5; }
  .top-welcome .intro-item { margin-bottom: 8px; }
  .top-welcome .intro-item .lead-in { font-size: 12px; color: #96622f; font-style: italic; display: block; margin-bottom: 2px; }
  .top-welcome .intro-item .bullet { font-size: 14px; color: #7a1f3d; font-weight: bold; }

  .councillors-heading { color: #14532d; font-size: 16px; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-top: 30px; }
  .councillors-row { display: table; width: 100%; margin-top: 12px; }
  .councillor-card { display: table-cell; width: 33.33%; text-align: center; padding: 0 8px; vertical-align: top; }
  .councillor-photo { width: 90px; height: 90px; border-radius: 50%; object-fit: cover; border: 2px solid #e5e5e5; }
  .councillor-name { font-weight: bold; font-size: 13px; margin-top: 8px; color: #222; }
  .councillor-detail { font-size: 11px; color: #555; margin-top: 2px; }

  .street-surgery { font-size: 12px; color: #333; margin-top: 20px; padding-top: 10px; border-top: 1px dashed #ccc; }
  .promoted-by { font-size: 10px; color: #999; margin-top: 10px; }
</style>
</head>
<body>
  <h1>{{ issue_title }}</h1>
  <p class="subtitle">{{ issue_date }} &middot; data refreshed {{ data_refreshed }}</p>

  <p class="greeting">{{ static_info.greeting_text }}</p>

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

  <div class="top-welcome">
    <p class="lead">{{ static_info.top_intro_lead }}</p>
    {% for item in static_info.top_intro_items %}
    <div class="intro-item">
      <span class="lead-in">{{ item.lead_in }}</span>
      <span class="bullet">&#10003; {{ item.bullet }}</span>
    </div>
    {% endfor %}
  </div>

  <h2 class="councillors-heading">Your Lower Stoke Councillors</h2>
  <div class="councillors-row">
    {% for c in static_info.councillors %}
    <div class="councillor-card">
      {% if c.photo_data_uri %}
      <img class="councillor-photo" src="{{ c.photo_data_uri }}" alt="{{ c.name }}">
      {% endif %}
      <div class="councillor-name">{{ c.name }}</div>
      <div class="councillor-detail">{{ c.email }}</div>
      {% if c.mobile %}<div class="councillor-detail">Mobile: {{ c.mobile }}</div>{% endif %}
      <div class="councillor-detail">{{ c.twitter }}</div>
      <div class="councillor-detail">{{ c.facebook }}</div>
    </div>
    {% endfor %}
  </div>

  <div class="street-surgery">
    <strong>{{ static_info.street_surgery_note }}</strong><br>
    {{ static_info.unified_email_note }}<br>
    {{ static_info.website_note }}<br>
    <strong>{{ static_info.subscribe_note }}</strong>
  </div>

  <div class="footer">{{ footer_note }}</div>
  <div class="promoted-by">{{ static_info.promoted_by_note }}</div>
</body>
</html>
"""


def render_html(content):
    return Template(HTML_TEMPLATE).render(**content)


def build_pdf(html_str, out_path):
    HTML(string=html_str).write_pdf(str(out_path))


def send_email(html_str, pdf_path, content):
    smtp_server = os.environ.get("SMTP_SERVER", "").strip()
    smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_address = os.environ.get("FROM_ADDRESS", "").strip()
    to_address = os.environ.get("TO_ADDRESS", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT") or "465")

    if not all([smtp_server, smtp_username, smtp_password, from_address, to_address]):
        # e.g. the testing repository, where SMTP secrets are deliberately
        # not configured: build everything but never send.
        print("SMTP secrets not configured — DRY RUN, email not sent.")
        print("Review the generated newsletter in the workflow's artifact (output/ folder).")
        return

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
