#!/usr/bin/env python3
"""
Generates the weekly Lower Stoke newsletter (HTML email body + PDF
attachment) from data/content.yaml, then emails it to the Google Group,
which fans it out to every subscriber automatically.

Run manually for testing:
    python scripts/generate_newsletter.py --dry-run

Environment variables required to actually send (set as GitHub Actions
secrets — see .github/workflows/newsletter.yml):
    SMTP_SERVER     e.g. smtp.gmail.com
    SMTP_PORT       e.g. 465
    SMTP_USERNAME   the mailbox that sends the newsletter
    SMTP_PASSWORD   an app password (not your normal password)
    FROM_ADDRESS    e.g. newsletter@lowerstoke.co.uk
    TO_ADDRESS      e.g. lowerstokenewsletter@googlegroups.com
"""

import argparse
import datetime
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

import yaml
from jinja2 import Template
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "data" / "content.yaml"
OUTPUT_DIR = ROOT / "output"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Arial, Helvetica, sans-serif; color: #222; max-width: 640px; margin: 0 auto; }
  h1 { color: #14532d; font-size: 22px; }
  h2 { color: #14532d; font-size: 16px; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-top: 28px; }
  ul { padding-left: 18px; }
  li { margin-bottom: 8px; }
  a { color: #1a5276; }
  .intro { font-size: 14px; margin-bottom: 20px; }
  .footer { font-size: 11px; color: #777; margin-top: 32px; border-top: 1px solid #eee; padding-top: 12px; }
</style>
</head>
<body>
  <h1>{{ issue_title }} &mdash; {{ issue_date }}</h1>
  <p class="intro">{{ intro }}</p>

  {% for section in sections %}
  <h2>{{ section.heading }}</h2>
  <ul>
    {% for item in section.entries %}
    <li><a href="{{ item.link }}">{{ item.text }}</a></li>
    {% endfor %}
  </ul>
  {% endfor %}

  <div class="footer">{{ footer_note }}</div>
</body>
</html>
"""


def load_content() -> dict:
    with open(CONTENT_FILE, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)
    if not content.get("issue_date"):
        content["issue_date"] = datetime.date.today().strftime("%d %B %Y")
    return content


def render_html(content: dict) -> str:
    return Template(HTML_TEMPLATE).render(**content)


def build_pdf(html_str: str, out_path: Path) -> None:
    HTML(string=html_str).write_pdf(str(out_path))


def send_email(html_str: str, pdf_path: Path, content: dict) -> None:
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
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=pdf_path.name,
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
        server.login(smtp_username, smtp_password)
        server.send_message(msg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the HTML/PDF but do not send an email",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    content = load_content()
    html_str = render_html(content)

    pdf_path = OUTPUT_DIR / f"newsletter-{datetime.date.today().isoformat()}.pdf"
    build_pdf(html_str, pdf_path)

    html_path = OUTPUT_DIR / f"newsletter-{datetime.date.today().isoformat()}.html"
    html_path.write_text(html_str, encoding="utf-8")

    print(f"Built {pdf_path} and {html_path}")

    if args.dry_run:
        print("Dry run — email not sent.")
        return

    send_email(html_str, pdf_path, content)
    print(f"Newsletter sent to {os.environ.get('TO_ADDRESS')}")


if __name__ == "__main__":
    sys.exit(main())

