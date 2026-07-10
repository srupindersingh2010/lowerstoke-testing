"""
Lower Stoke Ward — Daily Data Scraper
Runs via GitHub Actions every day at 08:00.

PLANNING DATA:
  Single source of truth: Coventry Planning Portal weekly received list.
  Only applications currently listed on the Council's portal are shown.
  If the portal is unreachable, planning.json will contain a siteDown marker
  and the website will display a notice rather than stale or incorrect data.
"""

import json, re, sys, traceback, csv, io, os, tempfile
from datetime import datetime, timezone, timedelta, date as dt_date
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# =============================================================================
# BROWSER FETCHER (Playwright)
# coventry.gov.uk and westmidlands.police.uk return 403 to plain HTTP requests
# from server IPs (GitHub Actions). Playwright headless Chrome bypasses this.
# Install once in workflow: pip install playwright && playwright install chromium
# =============================================================================
def browser_get(url, wait="domcontentloaded", timeout=30000):
    """
    Fetch a URL using a headless Chromium browser.
    Falls back gracefully if Playwright is not installed.
    Returns the page HTML string, or "" on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
            )
            ctx  = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()
            page.goto(url, wait_until=wait, timeout=timeout)
            html = page.content()
            browser.close()
            print(f"  BROWSER GET {url[:80]} -> {len(html)} chars")
            return html
    except Exception as e:
        print(f"  BROWSER GET {url[:80]} -> ERROR: {e}")
        return ""


DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NOW_UTC = datetime.now(timezone.utc)
NOW_UK  = NOW_UTC + timedelta(hours=1)
STAMP   = NOW_UK.strftime("%-d %B %Y at %H:%M")

PORTAL             = "https://planandregulatory.coventry.gov.uk/planning/index.html"
GALLERY_FOLDER_ID  = "1ukfcyO4BPjeAv40XVsvcJ-ds4ilwML3y"
SHEET_ID           = "1CiCnq-WvIL0KmEv3RldjV0u9KxpTttHQkbN1igNILhQ"
WMP_BASE           = "https://www.westmidlands.police.uk/area/your-area/west-midlands/coventry/stoke-and-wyken"
WMP_SUFFIX         = "top-reported-crimes-in-this-area"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

def safe_get(url, timeout=20):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        print(f"  GET {url[:90]} -> {r.status_code} ({len(r.text)} chars)")
        return r
    except Exception as e:
        print(f"  GET {url[:90]} -> ERROR: {e}")
        return None

def write_json(filename, data):
    path = DATA_DIR / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Wrote {filename} ({len(data) if isinstance(data, list) else 'object'})")

def fmt_date(iso_date):
    try:
        d = dt_date.fromisoformat(str(iso_date)[:10])
        return d.strftime("%-d %b %Y")
    except Exception:
        return str(iso_date)

def fmt_excel_date(value):
    """
    Convert an Excel/Google Sheets serial date number to a readable date string.
    Excel counts days from 1 Jan 1900. e.g. 46179.75 = 7 June 2026 at 18:00.
    Also handles: plain date strings, ISO dates, and empty values.
    """
    if not value:
        return ""
    # Already a readable string (not a number)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return ""
        # Try parsing as ISO date first
        try:
            d = dt_date.fromisoformat(value[:10])
            return d.strftime("%-d %b %Y")
        except Exception:
            pass
        # Try as a float serial
        try:
            value = float(value)
        except Exception:
            # Return as-is if it looks like a date string already
            if re.search(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', value):
                return value
            if re.search(r'\d{4}', value) and len(value) > 6:
                return value
            return value
    try:
        serial = float(value)
        # Excel serial: days since 30 Dec 1899 (accounting for leap year bug)
        base = dt_date(1899, 12, 30)
        d = base + timedelta(days=int(serial))
        return d.strftime("%-d %b %Y")
    except Exception:
        return str(value)

# =============================================================================
# 1. COUNCIL NEWS
# =============================================================================
def scrape_news():
    print("\n-- Council News --")
    entries = []
    html = browser_get("https://www.coventry.gov.uk/news")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        seen = set()

        # Coventry news page structure (confirmed June 2026):
        # Each article is a <li> containing:
        #   <h2><a href="/news/article/XXXX/title">Title</a></h2>
        #   <p>Summary text</p>
        #   <strong>Published: Day, Nth Month YYYY</strong>
        # Articles are inside <li> elements in a list

        # Try finding articles via li > h2 > a pattern first
        for li in soup.find_all("li"):
            h2 = li.find("h2")
            if not h2:
                continue
            a = h2.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            href  = a["href"]
            if not href or not title or title in seen or len(title) < 10:
                continue
            # Must be a news article link
            if "/news/article/" not in href and "/news/" not in href:
                continue
            seen.add(title)
            link = href if href.startswith("http") else "https://www.coventry.gov.uk" + href

            # Get published date — Coventry HTML structure:
            # <strong>Published:</strong> Monday, 8th June 2026
            # The <strong> only wraps "Published:" — date is sibling text after it.
            # Solution: read full li text and extract date with regex.
            date_str = ""
            li_text = li.get_text(" ", strip=True)
            date_m = re.search(
                r"Published.{0,5}(?:[A-Za-z]+,?\s*)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
                li_text, re.I)
            if date_m:
                date_str = date_m.group(1) + " " + date_m.group(2) + " " + date_m.group(3)
                print(f"    Date found: {date_str}")
            # Fallback: <time datetime="YYYY-MM-DD"> tag
            if not date_str:
                time_tag = li.find("time")
                if time_tag:
                    dt_attr = time_tag.get("datetime", "")
                    if dt_attr:
                        try:
                            d = dt_date.fromisoformat(dt_attr[:10])
                            date_str = d.strftime("%-d %B %Y")
                        except Exception:
                            date_str = time_tag.get_text(strip=True)
            # Get summary text from <p> in same li
            summary = ""
            p = li.find("p")
            if p:
                summary = p.get_text(strip=True)[:200]

            entries.append({
                "title":     title,
                "summary":   summary,
                "link":      link,
                "date":      date_str or "Recent",
                "focused":   len(entries) == 0,
                "source":    "coventry.gov.uk/news",
                "sourceUrl": "https://www.coventry.gov.uk/news",
                "fetchedAt": STAMP
            })
            print(f"  Article: {title[:70]}")
            if len(entries) >= 6:
                break

        # Fallback: try any h2 > a with news link if li approach found nothing
        if not entries:
            print("  li approach found nothing, trying direct h2 scan...")
            for h2 in soup.find_all("h2"):
                a = h2.find("a", href=True)
                if not a:
                    continue
                title = a.get_text(strip=True)
                href  = a["href"]
                if not href or not title or title in seen or len(title) < 10:
                    continue
                if "/news" not in href:
                    continue
                seen.add(title)
                link = href if href.startswith("http") else "https://www.coventry.gov.uk" + href
                # Look for date in surrounding elements
                date_str = ""
                container = h2.find_parent()
                if container:
                    strong = container.find("strong", string=re.compile(r"Published", re.I))
                    if strong:
                        date_str = strong.get_text(strip=True).replace("Published:", "").strip()
                entries.append({
                    "title": title, "summary": "", "link": link,
                    "date": date_str or "Recent", "focused": len(entries) == 0,
                    "source": "coventry.gov.uk/news",
                    "sourceUrl": "https://www.coventry.gov.uk/news",
                    "fetchedAt": STAMP
                })
                print(f"  Article (h2): {title[:70]}")
                if len(entries) >= 6:
                    break

    if not entries:
        if not html:
            # Browser couldn't reach the site at all
            print("  Coventry news site unreachable — writing siteDown marker")
            write_json("news.json", [{"siteDown": True, "fetchedAt": STAMP,
                                      "sourceUrl": "https://www.coventry.gov.uk/news"}])
            return
        # Site was reachable but we couldn't parse any articles
        entries = [{"title": "Visit Coventry Council for the latest news",
                    "link": "https://www.coventry.gov.uk/news",
                    "date": "See website", "summary": "",
                    "focused": True, "source": "coventry.gov.uk/news",
                    "sourceUrl": "https://www.coventry.gov.uk/news",
                    "fetchedAt": STAMP}]

    print(f"  Total news articles: {len(entries)}")
    write_json("news.json", entries)

# =============================================================================
# 2. PLANNING APPLICATIONS
#    Source 1: Coventry Planning Portal weekly list    (Playwright)
#    Source 2: Coventry Planning Portal ward search    (Playwright)
#    Source 3: planning.data.gov.uk open API           (plain HTTP, no auth)
#    Sources are tried in order; whichever succeeds first is used.
#    All three draw from Coventry Council data — nothing else is shown.
#    If all three fail, a siteDown marker is written.
# =============================================================================
def scrape_planning():
    print("\n-- Planning Applications --")
    apps = []

    WEEKLY_URL = "https://planandregulatory.coventry.gov.uk/planning/index.html?fa=getReceivedWeeklyList"
    WARD_URL   = "https://planandregulatory.coventry.gov.uk/planning/index.html?fa=getApplications&ward=Lower+Stoke"

    def parse_portal_table(html, source_url):
        """Parse a planning portal HTML page and return Lower Stoke apps."""
        found = []
        soup  = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_=re.compile(r"search", re.I)) or soup.find("table")
        if not table:
            print("  No table found in portal page")
            return found
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        print(f"  Portal table headers: {headers}")
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all("td")
            if not cells:
                continue
            col = {}
            for i, h in enumerate(headers):
                if i < len(cells):
                    col[h] = cells[i].get_text(strip=True)
            ward = col.get("ward", "")
            if "lower stoke" not in ward.lower():
                continue
            ref_link    = cells[0].find("a") if cells else None
            reference   = ref_link.get_text(strip=True) if ref_link else col.get("reference", "")
            href        = ref_link.get("href", "") if ref_link else ""
            portal_link = (
                f"https://planandregulatory.coventry.gov.uk{href}"
                if href.startswith("/")
                else PORTAL + "?fa=getApplication&ref=" + reference
            )
            found.append({
                "reference":   reference,
                "address":     col.get("address", ""),
                "description": col.get("proposal", col.get("description", "")),
                "status":      col.get("status", "Received"),
                "dateLodged":  col.get("valid date", col.get("date", "")),
                "ward":        ward,
                "portalLink":  portal_link,
                "source":      "planandregulatory.coventry.gov.uk",
                "sourceUrl":   source_url,
                "fetchedAt":   STAMP,
            })
            print(f"  Portal app: {reference} | {col.get('address','')[:40]}")
        return found

    def browser_get_planning(url):
        """
        Fetch a planning portal page, waiting up to 20s for the results
        table to appear after JavaScript renders it.
        Falls back to plain browser_get if Playwright or the wait fails.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"]
                )
                ctx  = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-GB",
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait for the results table OR any <table> to appear
                try:
                    page.wait_for_selector("table", timeout=20000)
                    print(f"  Table appeared in DOM")
                except PWTimeout:
                    print(f"  No table appeared after 20s — grabbing HTML anyway")
                html = page.content()
                browser.close()
                print(f"  BROWSER GET (planning) {url[:80]} -> {len(html)} chars")
                # Log a snippet so we can diagnose what the portal returned
                snippet = html[html.lower().find("<body"):html.lower().find("<body")+500] if "<body" in html.lower() else html[:500]
                print(f"  HTML snippet: {snippet[:300]!r}")
                return html
        except Exception as e:
            print(f"  BROWSER GET (planning) {url[:80]} -> ERROR: {e}")
            return browser_get(url)

    # ------------------------------------------------------------------
    # Source 1: weekly received list (Playwright, waits for table)
    # ------------------------------------------------------------------
    print("  Trying weekly received list...")
    html = browser_get_planning(WEEKLY_URL)
    if html and len(html) > 1000:
        apps = parse_portal_table(html, WEEKLY_URL)

    # ------------------------------------------------------------------
    # Source 2: ward search page (Playwright, waits for table)
    # ------------------------------------------------------------------
    if not apps:
        print("  Weekly list empty — trying ward search page...")
        html2 = browser_get_planning(WARD_URL)
        if html2 and len(html2) > 1000:
            apps = parse_portal_table(html2, WARD_URL)

    # ------------------------------------------------------------------
    # Source 3: planning.data.gov.uk open API
    # Free government API, no auth, no bot detection.
    # Lower Stoke ward entity = 800137.
    # Returns Coventry Council planning data — same underlying source.
    # ------------------------------------------------------------------
    if not apps:
        print("  Portal blocked — trying planning.data.gov.uk API...")
        try:
            ninety_ago = NOW_UTC - timedelta(days=90)
            api_url = (
                "https://www.planning.data.gov.uk/entity.json"
                "?dataset=planning-application"
                "&geometry_entity=800137"
                "&geometry_relation=intersects"
                f"&entry_date__gte={ninety_ago.strftime('%Y-%m-%d')}"
                "&limit=100"
            )
            r = safe_get(api_url)
            if r and r.status_code == 200:
                entities = r.json().get("entities", [])
                print(f"  planning.data.gov.uk: {len(entities)} entities returned")
                for e in entities:
                    ref  = (e.get("reference") or "").strip()
                    if not ref:
                        continue
                    addr = (e.get("name") or e.get("address-text") or "Lower Stoke, Coventry").strip()
                    desc = (e.get("description") or e.get("development-description") or
                            "Click reference to view full details.").strip()
                    stat = (e.get("status") or e.get("decision") or "Received").strip()
                    date = e.get("start-date") or e.get("entry-date") or ""
                    ref_enc = ref.replace("/", "%2F")
                    apps.append({
                        "reference":   ref,
                        "dateLodged":  fmt_date(date) if date else "",
                        "address":     addr,
                        "description": desc,
                        "status":      stat,
                        "portalLink":  f"{PORTAL}?fa=getApplication&id={ref_enc}",
                        "source":      "planning.data.gov.uk (Coventry Council data)",
                        "sourceUrl":   PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
                        "fetchedAt":   STAMP,
                    })
                    print(f"  API: {ref} | {addr[:45]} | {stat}")
            else:
                print(f"  planning.data.gov.uk: {r.status_code if r else 'no response'}")
        except Exception as e:
            print(f"  planning.data.gov.uk error: {e}")

    # ------------------------------------------------------------------
    # Source 4: Idox search.do backend API (two-step: session then POST)
    # The Idox JS frontend calls this endpoint directly. We replicate the
    # same two-step flow: GET the search page for a session cookie, then
    # POST the ward search form. Works from server IPs on many councils.
    # ------------------------------------------------------------------
    if not apps:
        print("  Trying Idox search.do backend API...")
        try:
            BASE = "https://planandregulatory.coventry.gov.uk/planning"
            session = requests.Session()
            session.headers.update(HEADERS)

            # Step 1: GET search page to establish session cookie
            r1 = session.get(f"{BASE}/index.html?fa=search", timeout=15)
            print(f"  Idox session GET: {r1.status_code} ({len(r1.text)} chars)")

            if r1.status_code == 200:
                # Step 2: POST the advanced search form with ward=Lower Stoke
                # These are the standard Idox PublicAccess form field names
                form_data = {
                    "searchType":        "Application",
                    "ward":              "Lower Stoke",
                    "application_ward":  "Lower Stoke",
                    "applicationType":   "",
                    "applicantName":     "",
                    "description":       "",
                    "address":           "",
                    "caseNo":            "",
                    "date(applicationValidatedStart)": "",
                    "date(applicationValidatedEnd)":   "",
                    "action":            "Search",
                }
                r2 = session.post(
                    f"{BASE}/search.do?action=Search&searchType=Application",
                    data=form_data,
                    timeout=20
                )
                print(f"  Idox search POST: {r2.status_code} ({len(r2.text)} chars)")
                # Log snippet for diagnosis
                body_start = r2.text.lower().find("<body")
                snippet = r2.text[body_start:body_start+400] if body_start > -1 else r2.text[:400]
                print(f"  Idox response snippet: {snippet[:300]!r}")

                if r2.status_code == 200 and len(r2.text) > 1000:
                    found = parse_portal_table(r2.text,
                                               f"{BASE}/search.do")
                    if found:
                        apps = found
                        print(f"  Idox API: {len(apps)} Lower Stoke apps found")
                    else:
                        print("  Idox API: no Lower Stoke rows in response")
            else:
                print(f"  Idox session blocked: {r1.status_code}")
        except Exception as e:
            print(f"  Idox search.do error: {e}")

    # ------------------------------------------------------------------
    # Write results — or siteDown if all sources failed
    # ------------------------------------------------------------------
    if apps:
        print(f"  Total planning apps: {len(apps)}")
        for a in apps:
            print(f"    {a['reference']} | {a['address'][:40]} | {a['status']}")
        write_json("planning.json", apps)
    else:
        print("  All sources failed — writing siteDown marker")
        write_json("planning.json", [{"siteDown": True, "fetchedAt": STAMP,
                                      "sourceUrl": WEEKLY_URL}])

# =============================================================================
# 3. WEST MIDLANDS POLICE
# =============================================================================
def wmp_fetch(section):
    """Fetch a WMP neighbourhood page using a real browser (bypasses 403 block)."""
    return browser_get(f"{WMP_BASE}/{section}/{WMP_SUFFIX}")

def scrape_police_events():
    print("\n-- Police PACT Events --")
    events = []
    html   = wmp_fetch("meetings-and-events")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for h5 in soup.find_all("h5"):
            title = h5.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            if re.search(r'cookie|report|contact|skip|nav|menu|station|social', title, re.I):
                continue

            date_str = address = ""
            for sib in h5.next_siblings:
                # Stop at the next heading
                if hasattr(sib, "name") and sib.name in ["h2","h3","h4","h5","h6"]:
                    break
                # Split raw text into lines — date and address are on separate lines
                raw   = sib.get_text(" ", strip=False) if hasattr(sib, "get_text") else str(sib)
                lines_inner = [l.strip() for l in raw.splitlines() if l.strip()]
                for line in lines_inner:
                    if "calendar" in line.lower() or "add to" in line.lower():
                        continue
                    # A date/time line has HH:MM and a month name or 4-digit year
                    if (re.search(r'\d{1,2}:\d{2}', line) and
                            re.search(r'\d{4}|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b', line, re.I)):
                        if not date_str:
                            date_str = line
                    else:
                        # Address line — strip leading dots WMP sometimes adds
                        clean = line.strip('. ,').strip()
                        if clean:
                            address += (', ' if address else '') + clean

            if title and date_str:
                # Only include Lower Stoke meetings — exclude Upper Stoke, Wyken, etc.
                if not re.search(r'lower stoke', title, re.I):
                    print(f"  Skipping (not Lower Stoke): {title[:60]}")
                    continue
                events.append({"title": title, "date": date_str,
                                "address": address.strip() or "Coventry",
                                "sourceUrl": f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}",
                                "fetchedAt": STAMP})
    wmp_url = f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}"

    # Supplement / fallback: official data.police.uk neighbourhood events API.
    # This works even when the WMP website blocks server requests.
    api_events = police_api_get(f"{get_neighbourhood_path()}/events") or []
    for ev in api_events:
        title = re.sub(r'\s+', ' ', (ev.get("title") or "Neighbourhood event")).strip()
        start = str(ev.get("start_date") or "")
        try:
            dt = datetime.fromisoformat(start)
            date_str = dt.strftime("%-I:%M%p, %a %-d %B %Y")
        except Exception:
            date_str = start
        addr = re.sub(r'\s+', ' ', (ev.get("address") or "Coventry")).strip()
        if not any(e["title"].lower() == title.lower() and e["date"] == date_str for e in events):
            events.append({"title": title, "date": date_str, "address": addr,
                           "sourceUrl": wmp_url, "fetchedAt": STAMP})

    if not html and not events:
        print("  WMP site unreachable and no API events — writing siteDown marker")
        write_json("police_events.json", [{"siteDown": True, "fetchedAt": STAMP,
                                           "sourceUrl": wmp_url}])
        return
    print(f"  Total events: {len(events)}")
    write_json("police_events.json", events)

def scrape_police_team():
    print("\n-- Police Team --")
    team = []
    html = wmp_fetch("on-the-team")
    if html:
        soup     = BeautifulSoup(html, "html.parser")
        ranks_re = re.compile(r'\b(Inspector|Chief Inspector|Superintendent|Sergeant|Sgt|Constable|Detective Constable|Detective Sergeant|PC|PCSO)\b', re.I)
        for heading in soup.find_all(["h3","h4","h5","h6"]):
            name = heading.get_text(strip=True)
            if not re.match(r'^[A-Z][a-z]+(?: [A-Z][a-z\'-]+){1,3}$', name):
                continue
            if re.search(r'cookie|police|coventry|stoke|wyken|west midlands|your|about|contact|meeting|station|social|news|crime', name, re.I):
                continue
            rank = bio = ""
            for sib in heading.next_siblings:
                sib_text = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else ""
                if not sib_text:
                    continue
                rank_m = ranks_re.search(sib_text)
                if rank_m and not rank:
                    rank = rank_m.group(1)
                if len(sib_text) > 30 and not bio:
                    bio = sib_text[:500]
                if hasattr(sib, "name") and sib.name in ["h3","h4","h5","h6"]:
                    break
            if name not in [t["name"] for t in team]:
                team.append({"name": name, "rank": rank or "Neighbourhood Officer",
                              "bio": bio, "sourceUrl": f"{WMP_BASE}/on-the-team/{WMP_SUFFIX}",
                              "fetchedAt": STAMP})
    # Supplement from the official neighbourhood team API
    api_team = police_api_get(f"{get_neighbourhood_path()}/people") or []
    for o in api_team:
        name = re.sub(r'\s+', ' ', (o.get("name") or "")).strip()
        if not name or any(t["name"].lower() == name.lower() for t in team):
            continue
        bio = re.sub(r'<[^>]+>', ' ', o.get("bio") or "").strip()
        bio = re.sub(r'\s+', ' ', bio)[:500]
        team.append({"name": name, "rank": o.get("rank") or "Neighbourhood Officer",
                     "bio": bio, "sourceUrl": f"{WMP_BASE}/on-the-team/{WMP_SUFFIX}",
                     "fetchedAt": STAMP})

    confirmed = {"name":"Manwar Porter","rank":"Inspector",
                 "bio":"Local Policing Inspector for the North East Sector of Coventry covering Stoke & Wyken. Primary focus on antisocial behaviour, vehicle crime, and retail crime.",
                 "sourceUrl":f"{WMP_BASE}/on-the-team/{WMP_SUFFIX}","fetchedAt":STAMP}
    if not any(t["name"] == "Manwar Porter" for t in team):
        team.insert(0, confirmed)
    print(f"  Total officers: {len(team)}")
    write_json("police_team.json", team)

# =============================================================================
# 3b. DATA.POLICE.UK — OFFICIAL OPEN DATA API
# This is the definitive source that powers the WMP website's own crime
# widgets. It's a plain JSON API (no key, no blocking), so it is far more
# reliable than scraping westmidlands.police.uk, which 403-blocks servers.
# Crime stats are published nationally ONCE A MONTH (usually ~2 months behind),
# so counts change monthly by design; priorities/events/news change more often.
# =============================================================================
POLICE_API      = "https://data.police.uk/api"
POLICE_FORCE    = "west-midlands"
NBH_DEFAULT_ID  = "CV005"   # Stoke & Wyken's API code (from police.uk event links)
_NBH_CACHE      = {"path": None}
PUBLIC_AREA_URL = "https://www.police.uk/pu/your-area/west-midlands-police/stoke-and-wyken/"

def get_neighbourhood_path():
    """
    'west-midlands/CV005' — resolved dynamically because the API uses internal
    codes (NOT the website slug 'stoke-and-wyken'), and codes can change when
    forces redraw neighbourhoods. Order: locate by coordinates -> search the
    force's neighbourhood list by name -> known default.
    """
    if _NBH_CACHE["path"]:
        return _NBH_CACHE["path"]
    loc = police_api_get("locate-neighbourhood", params={"q": "52.4105,-1.4700"})
    if isinstance(loc, dict) and loc.get("force") and loc.get("neighbourhood"):
        _NBH_CACHE["path"] = f"{loc['force']}/{loc['neighbourhood']}"
        print(f"  Neighbourhood resolved by location: {_NBH_CACHE['path']}")
        return _NBH_CACHE["path"]
    lst = police_api_get(f"{POLICE_FORCE}/neighbourhoods")
    if isinstance(lst, list):
        for n in lst:
            if re.search(r'stoke\s+and\s+wyken', str(n.get("name", "")), re.I):
                _NBH_CACHE["path"] = f"{POLICE_FORCE}/{n['id']}"
                print(f"  Neighbourhood resolved by name: {_NBH_CACHE['path']}")
                return _NBH_CACHE["path"]
    _NBH_CACHE["path"] = f"{POLICE_FORCE}/{NBH_DEFAULT_ID}"
    print(f"  Neighbourhood defaulted to: {_NBH_CACHE['path']}")
    return _NBH_CACHE["path"]

CRIME_CATEGORY_NAMES = {
    "anti-social-behaviour":  "Anti-Social Behaviour",
    "bicycle-theft":          "Bicycle Theft",
    "burglary":               "Burglary",
    "criminal-damage-arson":  "Criminal Damage and Arson",
    "drugs":                  "Drugs",
    "other-theft":            "Other Theft",
    "possession-of-weapons":  "Possession of Weapons",
    "public-order":           "Public Order",
    "robbery":                "Robbery",
    "shoplifting":            "Shoplifting",
    "theft-from-the-person":  "Theft from the Person",
    "vehicle-crime":          "Vehicle Crime",
    "violent-crime":          "Violence and Sexual Offences",
    "other-crime":            "Other Crime",
}

def police_api_get(path, params=None, timeout=25):
    """
    GET a data.police.uk API endpoint. Returns parsed JSON or None.
    Tries a plain HTTP request first (fast); if the CDN refuses it
    (bot-filtering of datacenter IPs like GitHub Actions), falls back
    to fetching through a real Chromium browser.
    """
    url = f"{POLICE_API}/{path.lstrip('/')}"
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    import time
    for attempt in range(3):
        try:
            r = requests.get(url, headers=dict(HEADERS, Accept="application/json"), timeout=timeout)
            print(f"  API GET {url[:90]} -> {r.status_code}")
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:          # rate-limited: back off and retry
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404:          # wrong path — retrying won't help
                return None
            break
        except Exception as e:
            print(f"  API GET {url[:90]} -> ERROR: {e}")
            break
    return browser_fetch_json(url)

def close_shared_browser():
    pass  # browser fallback is now self-contained per call

def browser_fetch_json(url, post_data=None, origin="https://data.police.uk/api/forces"):
    """GET/POST a JSON endpoint from inside a real browser page (same-origin fetch)."""
    txt = _browser_fetch_text(url, post_data=post_data, origin=origin)
    if txt:
        try:
            return json.loads(txt)
        except Exception as e:
            print(f"  BROWSER API parse error for {url[:70]}: {e}")
    return None

def _browser_fetch_text(url, post_data=None, origin=None):
    """One-shot real-browser fetch: launches and closes its own Chromium so it
    can never clash with the other Playwright helper (browser_get)."""
    try:
        from playwright.sync_api import sync_playwright
        from urllib.parse import urlencode
        body = urlencode(post_data) if post_data else None
        base = origin or url
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-GB",
                                      viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(base, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)  # allow any bot-check to settle
            res = page.evaluate(
            """async ([url, body]) => {
                 const opts = body
                   ? {method:'POST',
                      headers:{'Content-Type':'application/x-www-form-urlencoded'},
                      body: body}
                   : {};
                 const r = await fetch(url, opts);
                 return {status: r.status, text: await r.text()};
               }""",
                [url, body])
            browser.close()
        print(f"  BROWSER API {'POST' if post_data else 'GET'} {url[:80]} -> {res['status']}")
        if res["status"] == 200:
            return res["text"]
    except Exception as e:
        print(f"  BROWSER API {url[:80]} -> ERROR: {e}")
    return None

def month_display(ym):
    """'2026-05' -> 'May 2026'"""
    try:
        return datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
    except Exception:
        return ym

def previous_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"

def fetch_neighbourhood_boundary():
    """Boundary of Stoke & Wyken as a list of [lat, lng] float pairs."""
    data = police_api_get(f"{get_neighbourhood_path()}/boundary")
    pts = []
    if isinstance(data, list):
        for p in data:
            try:
                pts.append([float(p["latitude"]), float(p["longitude"])])
            except Exception:
                continue
    return pts

def fetch_month_crimes(poly_pts, month):
    """
    All street-level crimes inside the neighbourhood boundary for a month.
    Uses POST because the full boundary polygon exceeds GET URL limits.
    Returns a list of crime dicts, or None on failure (as opposed to a
    genuinely empty month, which returns []).
    """
    if poly_pts:
        poly = ":".join(f"{lat:.5f},{lng:.5f}" for lat, lng in poly_pts)
        try:
            r = requests.post(f"{POLICE_API}/crimes-street/all-crime",
                              data={"poly": poly, "date": month},
                              headers={"User-Agent": HEADERS["User-Agent"]},
                              timeout=40)
            print(f"  API POST crimes-street/all-crime date={month} -> {r.status_code}")
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"  API POST crimes-street error: {e}")
        # Same POST through the real browser (CDN bot-blocking fallback)
        data = browser_fetch_json(f"{POLICE_API}/crimes-street/all-crime",
                                  post_data={"poly": poly, "date": month})
        if data is not None:
            return data
    # Last resort (also used when the boundary couldn't be fetched):
    # 1-mile radius around the centre of the ward — short GET URL
    centre = police_api_get("crimes-street/all-crime",
                            params={"lat": "52.4105", "lng": "-1.4700", "date": month})
    return centre

def scrape_police_crimes():
    """
    Writes:
      police_crimes.json    — top reported crime types with real monthly counts
      police_crime_map.json — individual crime points + ward boundary for the map
      police_priorities.json— current neighbourhood policing priorities (issue/action)
    """
    print("\n-- Police Crimes (data.police.uk official API) --")

    # 1. Latest month of published crime data
    latest = police_api_get("crime-last-updated") or {}
    month  = str(latest.get("date", ""))[:7]  # '2026-05-01' -> '2026-05'
    if not re.match(r'^\d{4}-\d{2}$', month):
        month = (NOW_UTC - timedelta(days=62)).strftime("%Y-%m")

    # 2. Ward boundary + crimes (step back up to 3 months if needed)
    boundary = fetch_neighbourhood_boundary()
    crimes, used_month = None, month
    for _ in range(4):
        crimes = fetch_month_crimes(boundary, used_month)
        if crimes:
            break
        used_month = previous_month(used_month)
    crimes = crimes or []
    m_disp = month_display(used_month)
    print(f"  {len(crimes)} crimes for {m_disp}")

    # 3. Aggregate counts per category -> police_crimes.json
    counts = {}
    for c in crimes:
        cat = c.get("category", "other-crime")
        counts[cat] = counts.get(cat, 0) + 1
    rows = []
    for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        rows.append({
            "title":     CRIME_CATEGORY_NAMES.get(cat, cat.replace("-", " ").title()),
            "issue":     f"{n} reported in {m_disp}",
            "count":     str(n),
            "action":    "See the West Midlands Police neighbourhood page for the team's current activity on this crime type.",
            "status":    "Top Reported Crime",
            "month":     used_month,
            "monthDisplay": m_disp,
            "total":     len(crimes),
            "source":    "data.police.uk (official police open data)",
            "sourceUrl": PUBLIC_AREA_URL,
            "fetchedAt": STAMP,
        })
    if rows:
        write_json("police_crimes.json", rows)
    else:
        # API unreachable — keep yesterday's file rather than writing junk,
        # but leave a marker if no file exists at all.
        if not (DATA_DIR / "police_crimes.json").exists():
            write_json("police_crimes.json", [{"siteDown": True, "fetchedAt": STAMP,
                                               "sourceUrl": PUBLIC_AREA_URL}])
        print("  WARNING: no crime data fetched — keeping previous file")

    # 4. Map data -> police_crime_map.json (points + simplified boundary)
    if crimes:
        step   = max(1, len(boundary) // 150)
        b_slim = boundary[::step]
        points = []
        for c in crimes:
            loc = c.get("location") or {}
            try:
                points.append({
                    "lat":    float(loc.get("latitude")),
                    "lng":    float(loc.get("longitude")),
                    "cat":    c.get("category", "other-crime"),
                    "street": (loc.get("street") or {}).get("name", ""),
                })
            except Exception:
                continue
        write_json("police_crime_map.json", {
            "month": used_month, "monthDisplay": m_disp,
            "total": len(points), "fetchedAt": STAMP,
            "categories": CRIME_CATEGORY_NAMES,
            "boundary": b_slim, "crimes": points,
        })

    # 5. Neighbourhood policing priorities (correct endpoint) -> police_priorities.json
    pris  = police_api_get(f"{get_neighbourhood_path()}/priorities") or []
    plist = []
    for item in pris:
        issue  = re.sub(r'<[^>]+>', ' ', item.get("issue")  or "").strip()
        action = re.sub(r'<[^>]+>', ' ', item.get("action") or "").strip()
        issue  = re.sub(r'\s+', ' ', issue)
        action = re.sub(r'\s+', ' ', action)
        if not issue:
            continue
        plist.append({
            "issue":     issue,
            "action":    action,
            "issueDate": fmt_date(item.get("issue-date", "")),
            "actionDate": fmt_date(item.get("action-date", "")),
            "source":    "data.police.uk",
            "sourceUrl": PUBLIC_AREA_URL,
            "fetchedAt": STAMP,
        })
    write_json("police_priorities.json", plist)

def scrape_police_news():
    """
    West Midlands Police news filtered to Coventry -> police_news.json
    Same schema as the newsletter's scrape_police_news.py so both can share it.
    """
    print("\n-- Police News (WMP RSS, Coventry items) --")
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    RSS_URL   = "https://www.westmidlands.police.uk/news/west-midlands/news/GetNewsRss/"
    DAYS_BACK = 14
    cutoff    = NOW_UTC - timedelta(days=DAYS_BACK)
    articles  = []

    xml_text = ""
    try:
        r = requests.get(RSS_URL, headers=HEADERS, timeout=20)
        if r.status_code == 200 and "<item" in r.text:
            xml_text = r.text
        else:
            print(f"  RSS GET -> {r.status_code}")
    except Exception as e:
        print(f"  RSS GET error: {e}")

    if not xml_text:
        # WMP's site blocks GitHub's IPs outright (403 even via a real browser,
        # per the Actions log) — so fall back to Google News RSS, which is
        # server-friendly and aggregates WMP + local outlets like CoventryLive.
        gn_url = ("https://news.google.com/rss/search?"
                  "q=%22West%20Midlands%20Police%22%20Coventry%20when:14d"
                  "&hl=en-GB&gl=GB&ceid=GB:en")
        try:
            r = requests.get(gn_url, headers=HEADERS, timeout=20)
            print(f"  Google News RSS -> {r.status_code}")
            if r.status_code == 200 and "<item" in r.text:
                xml_text = r.text
        except Exception as e:
            print(f"  Google News RSS error: {e}")

    if xml_text:
        is_google = "news.google.com" in xml_text
        try:
            root = ET.fromstring(xml_text.encode("utf-8"))
            for item in root.iter("item"):
                title   = (item.findtext("title") or "").strip()
                link    = (item.findtext("link") or "").strip()
                summary = re.sub(r'<[^>]+>', ' ', item.findtext("description") or "").strip()
                summary = re.sub(r'\s+', ' ', summary)[:400]
                publisher = (item.findtext("source") or "").strip()
                if is_google and publisher and title.endswith(" - " + publisher):
                    title = title[: -(len(publisher) + 3)].strip()
                pub_raw = (item.findtext("pubDate") or "").strip()
                try:
                    pub = parsedate_to_datetime(pub_raw)
                    if pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if pub < cutoff:
                    continue
                # WMP's force-wide feed needs a Coventry filter; the Google News
                # query is already scoped to Coventry so no further filter there.
                if not is_google and "coventry" not in f"{title} {summary}".lower():
                    continue
                articles.append({
                    "title": title, "link": link, "summary": summary,
                    "published": pub.isoformat(),
                    "published_display": pub.strftime("%-d %B %Y"),
                    "source": publisher or "westmidlands.police.uk",
                    "fetchedAt": STAMP,
                })
        except Exception as e:
            print(f"  RSS parse error: {e}")

    articles.sort(key=lambda a: a["published"], reverse=True)
    print(f"  {len(articles)} Coventry police news item(s) in last {DAYS_BACK} days")
    if articles or not (DATA_DIR / "police_news.json").exists():
        write_json("police_news.json", articles)
    else:
        print("  RSS unavailable — keeping previous police_news.json")

# =============================================================================
# 4. CASEWORK LOG
# =============================================================================
def scrape_casework():
    print("\n-- Casework Log --")
    cases = []
    url   = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&sheet=Sheet1"
    r     = safe_get(url)
    if r and r.status_code == 200:
        reader  = csv.DictReader(io.StringIO(r.text))
        headers = reader.fieldnames or []
        print(f"  Headers: {headers}")
        def col(keywords):
            # Exact match first (case-insensitive)
            for h in headers:
                for kw in keywords:
                    if h.lower().strip() == kw.lower().strip():
                        return h
            # Partial match — whole word only to avoid "date" matching "update"
            for h in headers:
                for kw in keywords:
                    if re.search(r"\b" + re.escape(kw.lower()) + r"\b", h.lower()):
                        return h
            return None

        # Exact column names from your Google Sheet:
        # Id | Start time | Completion time | Email | Name | Title |
        # Location Focus | Update Body Text | Status | Logged by
        c_title = col(["Title", "title", "subject", "issue", "problem"])
        c_body  = col(["Update Body Text", "body text", "body", "detail", "description", "note"])
        c_loc   = col(["Location Focus", "location", "address", "area", "street"])
        c_stat  = col(["Status", "status", "stage", "state"])
        c_log   = col(["Logged by", "logged by", "councillor", "officer", "assigned"])
        c_name  = col(["Name", "name", "resident", "contact"])
        c_email = col(["Email", "email"])
        c_date  = col(["Start time", "start time", "Completion time", "timestamp", "date received", "date logged"])
        print(f"  Columns: title={c_title} body={c_body} date={c_date} status={c_stat}")
        for row in reader:
            title = (row.get(c_title,"") if c_title else "").strip()
            body  = (row.get(c_body, "") if c_body  else "").strip()
            if not title and not body:
                continue
            if not title:
                title = next((v.strip() for v in row.values() if v.strip()), "Ward Issue")
            cases.append({
                "title":        title or "Ward Issue",
                "bodyText":     body,
                "locationFocus":(row.get(c_loc,"")   if c_loc   else "Lower Stoke").strip() or "Lower Stoke",
                "status":       (row.get(c_stat,"")  if c_stat  else "Logged").strip()      or "Logged",
                "loggedBy":     (row.get(c_log,"")   if c_log   else "").strip(),
                "name":         (row.get(c_name,"")  if c_name  else "").strip(),
                "email":        (row.get(c_email,"") if c_email else "").strip(),
                "date":         fmt_excel_date(row.get(c_date,"") if c_date else ""),
                "fetchedAt":    STAMP
            })
        print(f"  Cases: {len(cases)}")
    else:
        print("  Could not read sheet — share as 'Anyone with link can view'")
    write_json("casework.json", cases)

# =============================================================================
# 5. METADATA
# =============================================================================
def write_metadata():
    write_json("meta.json", {"lastUpdated": STAMP, "updatedAt": NOW_UTC.isoformat()})

# =============================================================================
# MAIN
# =============================================================================
def scrape_council_meetings():
    """Scrape council meetings for next 30 days; check attendance for next 7 days."""
    print("\n-- Council Meetings (next 30 days) --")
    BASE_URL  = "https://edemocracy.coventry.gov.uk"
    OUR_CLLRS = ["mcnicholas", "rupinder singh", "shahnaz akhter"]
    meetings  = []
    seen      = set()

    for offset in range(2):
        dt   = NOW_UTC + timedelta(days=offset * 32)
        url  = (f"{BASE_URL}/mgCalendarAgendaView.aspx"
                f"?RPID=0&M={dt.month}&DD={dt.year}&CID=0&OT=&C=-1&MR=1")
        html = browser_get(url)
        if not html or len(html) < 500:
            print(f"  Could not fetch month {dt.month}/{dt.year}")
            continue
        print(f"  Month {dt.month}/{dt.year}: {len(html)} chars")

        soup        = BeautifulSoup(html, "html.parser")
        cutoff      = NOW_UTC.date() + timedelta(days=30)
        week_cutoff = NOW_UTC.date() + timedelta(days=7)

        # Walk all elements in order: <p> tags contain date headers,
        # <li> tags with ieListDocuments links contain meetings
        current_date = None
        current_day  = None
        for elem in soup.find_all(True):
            if elem.name == "p":
                date_m = re.match(
                    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
                    r"(\d{1,2})(?:st|nd|rd|th)\s+(\w+),\s+(\d{4})",
                    elem.get_text(strip=True))
                if date_m:
                    try:
                        current_date = datetime.strptime(
                            f"{date_m.group(2)} {date_m.group(3)} {date_m.group(4)}",
                            "%d %B %Y").date()
                        current_day = date_m.group(1)
                    except ValueError:
                        current_date = None
            elif elem.name == "li" and current_date:
                a = elem.find("a", href=re.compile(r"ieListDocuments"))
                if not a:
                    continue
                if current_date < NOW_UTC.date() or current_date > cutoff:
                    continue
                li_text    = elem.get_text(" ", strip=True)
                href       = a["href"]
                link_text  = a.get_text(strip=True)
                time_m     = re.match(
                    r"(\d{1,2}\.\d{2}\s*(?:am|pm)(?:\s*-\s*\d{1,2}\.\d{2}\s*(?:am|pm))?)",
                    li_text)
                time_str   = time_m.group(1).strip() if time_m else ""
                title      = re.sub(r"\s+on\s+\d{2}/\d{2}.*$", "", link_text).strip()
                loc_m      = re.search(
                    r"-\s+((?:Committee Room|Council Chamber|Diamond Room|Council House)[^-\n]+?)$",
                    li_text)
                location   = loc_m.group(1).strip() if loc_m else "Council House, Coventry"
                if href.startswith("http"):
                    agenda_url = href
                elif href.startswith("/"):
                    agenda_url = f"{BASE_URL}{href}"
                else:
                    agenda_url = f"{BASE_URL}/{href}"
                mid_m      = re.search(r"MId=(\d+)", href)
                attend_url = (f"{BASE_URL}/mgMeetingAttendance.aspx?ID={mid_m.group(1)}"
                              if mid_m else "")
                key = f"{current_date}{title}"
                if key in seen:
                    continue
                seen.add(key)

                our_cllrs_attending = []
                attendance_checked  = False
                if attend_url and current_date <= week_cutoff:
                    ra = safe_get(attend_url)
                    if ra and ra.status_code == 200:
                        attendance_checked = True
                        asoup = BeautifulSoup(ra.text, "html.parser")
                        for row in asoup.find_all("tr"):
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue
                            name   = cells[0].get_text(strip=True).lower()
                            status = cells[2].get_text(strip=True).lower()
                            for cllr in OUR_CLLRS:
                                if cllr in name and status in ("expected", "present"):
                                    display = re.sub(
                                        r"^Councillor\s+", "",
                                        cells[0].get_text(strip=True))
                                    if display not in our_cllrs_attending:
                                        our_cllrs_attending.append(display)
                    print(f"  Attendance {current_date} {title[:30]}: {our_cllrs_attending or 'none'}")

                meetings.append({
                    "date":              current_date.strftime("%-d %B %Y"),
                    "dayOfWeek":         current_day,
                    "time":              time_str,
                    "title":             title,
                    "location":          location,
                    "agendaUrl":         agenda_url,
                    "attendanceUrl":     attend_url,
                    "withinWeek":        current_date <= week_cutoff,
                    "attendanceChecked": attendance_checked,
                    "ourCouncillors":    our_cllrs_attending,
                    "sourceUrl":         url,
                    "fetchedAt":         STAMP,
                })
                print(f"  Added: {current_date} {time_str} — {title[:50]}")

    print(f"  Total meetings: {len(meetings)}")
    write_json("council_meetings.json", meetings)


def scrape_wmca_meetings():
    """Scrape WMCA meetings for next 30 days from wmca.moderngov.co.uk."""
    print("\n-- WMCA Meetings (next 30 days) --")
    BASE_URL  = "https://wmca.moderngov.co.uk"
    meetings  = []
    seen      = set()

    for offset in range(2):
        dt   = NOW_UTC + timedelta(days=offset * 32)
        url  = (f"{BASE_URL}/mgCalendarAgendaView.aspx"
                f"?RPID=0&M={dt.month}&DD={dt.year}&CID=0&OT=&C=-1&MR=1")
        html = browser_get(url)
        if not html or len(html) < 500:
            print(f"  Could not fetch WMCA month {dt.month}/{dt.year}")
            continue
        print(f"  WMCA Month {dt.month}/{dt.year}: {len(html)} chars")

        soup        = BeautifulSoup(html, "html.parser")
        cutoff      = NOW_UTC.date() + timedelta(days=30)
        week_cutoff = NOW_UTC.date() + timedelta(days=7)
        current_date = None
        current_day  = None

        for elem in soup.find_all(True):
            if elem.name == "p":
                txt = elem.get_text(strip=True)
                # WMCA format: "Monday 22nd June 2026" (no commas)
                date_m = re.match(
                    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
                    r"(\d{1,2})(?:st|nd|rd|th)\s+(\w+)\s+(\d{4})", txt)
                if date_m:
                    try:
                        current_date = datetime.strptime(
                            f"{date_m.group(2)} {date_m.group(3)} {date_m.group(4)}",
                            "%d %B %Y").date()
                        current_day = date_m.group(1)
                    except ValueError:
                        current_date = None
            elif elem.name == "li" and current_date:
                a = elem.find("a", href=re.compile(r"ieListDocuments"))
                if not a:
                    continue
                if current_date < NOW_UTC.date() or current_date > cutoff:
                    continue
                li_text    = elem.get_text(" ", strip=True)
                href       = a["href"]
                link_text  = a.get_text(strip=True)
                link_text  = re.sub(r"^PROVISIONAL\s*-\s*", "", link_text).strip()
                time_m     = re.match(
                    r"(\d{1,2}\.\d{2}\s*(?:am|pm)(?:\s*-\s*\d{1,2}\.\d{2}\s*(?:am|pm))?)",
                    li_text)
                time_str   = time_m.group(1).strip() if time_m else ""
                title      = re.sub(r"\s+on\s+\d{2}/\d{2}.*$", "", link_text).strip()
                loc_m      = re.search(r"-\s+([^-]{10,})$", li_text)
                location   = loc_m.group(1).strip() if loc_m else "16 Summer Lane, Birmingham"
                # Always build absolute URL using BASE_URL
                if href.startswith("http"):
                    agenda_url = href
                elif href.startswith("/"):
                    agenda_url = f"{BASE_URL}{href}"
                else:
                    agenda_url = f"{BASE_URL}/{href}"
                key = f"{current_date}{title}"
                if key in seen:
                    continue
                seen.add(key)
                meetings.append({
                    "date":       current_date.strftime("%-d %B %Y"),
                    "dayOfWeek":  current_day,
                    "time":       time_str,
                    "title":      title,
                    "location":   location,
                    "agendaUrl":  agenda_url,
                    "withinWeek": current_date <= week_cutoff,
                    "sourceUrl":  url,
                    "fetchedAt":  STAMP,
                })
                print(f"  Added: {current_date} {time_str} — {title[:50]}")

    print(f"  Total WMCA meetings: {len(meetings)}")
    write_json("wmca_meetings.json", meetings)


if __name__ == "__main__":
    print(f"=== Lower Stoke Ward Scraper - {STAMP} ===\n")
    for fn in [scrape_news, scrape_planning, scrape_council_meetings, scrape_wmca_meetings, scrape_police_events,
               scrape_police_team, scrape_police_crimes, scrape_police_news, scrape_casework,
               write_metadata]:
        try:
            fn()
        except Exception as e:
            print(f"ERROR in {fn.__name__}: {e}")
            traceback.print_exc()
    close_shared_browser()
    print("\n=== Done ===")
    sys.exit(0)
