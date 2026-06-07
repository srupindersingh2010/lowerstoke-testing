"""
Lower Stoke Ward — Daily Data Scraper
Runs via GitHub Actions every day at 08:00.
Writes JSON files into data/ folder which index.html reads.
"""

import json, re, sys, traceback, csv, io
from datetime import datetime, timezone, timedelta, date as dt_date
from pathlib import Path
import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NOW_UTC = datetime.now(timezone.utc)
NOW_UK  = NOW_UTC + timedelta(hours=1)
STAMP   = NOW_UK.strftime("%-d %B %Y at %H:%M")

PORTAL  = "https://planandregulatory.coventry.gov.uk/planning/index.html"

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
    """Convert YYYY-MM-DD to '7 June 2026'"""
    try:
        d = dt_date.fromisoformat(iso_date)
        return d.strftime("%-d %b %Y")
    except Exception:
        return iso_date

# =============================================================================
# 1. COVENTRY COUNCIL NEWS — scrape HTML page (RSS is broken)
# =============================================================================
def scrape_news():
    print("\n-- Council News --")
    entries = []
    r = safe_get("https://www.coventry.gov.uk/news")
    if r and r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        for h2 in soup.find_all("h2"):
            a = h2.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            href  = a["href"]
            if not href or not title or title in seen or len(title) < 10:
                continue
            seen.add(title)
            link = href if href.startswith("http") else "https://www.coventry.gov.uk" + href
            date_str = ""
            parent = h2.find_parent()
            if parent:
                strong = parent.find("strong", string=re.compile(r"Published", re.I))
                if strong:
                    date_str = strong.get_text(strip=True).replace("Published:", "").strip()
            entries.append({
                "title":     title,
                "link":      link,
                "date":      date_str or "Recent",
                "focused":   len(entries) == 0,
                "source":    "coventry.gov.uk/news",
                "sourceUrl": "https://www.coventry.gov.uk/news",
                "fetchedAt": STAMP
            })
            if len(entries) >= 6:
                break
    if not entries:
        entries = [{"title": "Visit Coventry Council for the latest news",
                    "link": "https://www.coventry.gov.uk/news", "date": "See website",
                    "focused": True, "source": "coventry.gov.uk/news",
                    "sourceUrl": "https://www.coventry.gov.uk/news", "fetchedAt": STAMP}]
    print(f"  News articles: {len(entries)}")
    write_json("news.json", entries)

# =============================================================================
# 2. PLANNING APPLICATIONS
#    Primary:  planning.data.gov.uk open API — free, no blocking, ward-filtered
#    Backup:   Coventry weekly list HTML (often blocked but worth trying)
#    Fallback: Manual entries hardcoded below (always shown)
# =============================================================================
def scrape_planning():
    print("\n-- Planning Applications --")
    apps = []

    # ------------------------------------------------------------------
    # SOURCE 1: planning.data.gov.uk API
    # Ward entity 800137 = Lower Stoke (confirmed from planning.data.gov.uk)
    # Free open government API, no key needed, never blocks
    # ------------------------------------------------------------------
    try:
        ninety_ago = NOW_UTC - timedelta(days=90)
        api_url = (
            "https://www.planning.data.gov.uk/entity.json"
            "?dataset=planning-application"
            "&geometry_entity=800137"
            "&geometry_relation=intersects"
            f"&entry_date_year={ninety_ago.year}"
            f"&entry_date_month={ninety_ago.month}"
            f"&entry_date_day={ninety_ago.day}"
            "&entry_date_match=after"
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
                        "Click reference to view full details on the planning portal.").strip()
                stat = (e.get("status") or e.get("decision") or "Received").strip()
                date = e.get("start-date") or e.get("entry-date") or ""
                ref_enc = ref.replace("/", "%2F")
                apps.append({
                    "reference":   ref,
                    "dateLodged":  fmt_date(date) if date else STAMP,
                    "address":     addr,
                    "description": desc,
                    "status":      stat,
                    "portalLink":  f"{PORTAL}?fa=getApplication&id={ref_enc}",
                    "source":      "planning.data.gov.uk",
                    "sourceUrl":   PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
                    "fetchedAt":   STAMP,
                    "storedAt":    NOW_UTC.timestamp()
                })
                print(f"  App: {ref} | {addr[:50]} | {stat}")
        else:
            print(f"  API status: {r.status_code if r else 'no response'}")
    except Exception as e:
        print(f"  planning.data.gov.uk error: {e}")
        traceback.print_exc()

    # ------------------------------------------------------------------
    # SOURCE 2: Coventry weekly list (backup — often returns 406 but try)
    # ------------------------------------------------------------------
    if not apps:
        print("  Trying Coventry weekly list as backup...")
        for fa in ["getReceivedWeeklyList", "getDeterminedWeeklyList"]:
            r2 = safe_get(f"{PORTAL}?fa={fa}")
            if r2 and r2.status_code == 200:
                soup = BeautifulSoup(r2.text, "html.parser")
                for row in soup.find_all("tr"):
                    text = row.get_text(" ", strip=True)
                    if "lower stoke" not in text.lower():
                        continue
                    cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                    ref_m = re.search(r'\b(PL/\d{4}/\d+/[A-Z]+|[A-Z]{2,5}/\d{4}/\d{3,6}(?:/[A-Z]+)?)\b', text, re.I)
                    ref   = ref_m.group(1).upper() if ref_m else None
                    if not ref:
                        continue
                    link_tag = row.find("a", href=re.compile(r'fa=getApplication', re.I))
                    href_val = link_tag["href"] if link_tag else ""
                    app_link = (href_val if href_val.startswith("http")
                                else "https://planandregulatory.coventry.gov.uk" + href_val
                                if href_val else PORTAL + "?fa=getApplications&ward=Lower%20Stoke")
                    addr = desc = date_l = ""
                    stat = "Received"
                    for c in cells:
                        if not c or len(c) < 3 or c.upper() == ref:
                            continue
                        if re.search(r'lower\s*stoke', c, re.I) and len(c) < 30:
                            continue
                        if not date_l and re.search(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{4}', c):
                            date_l = re.search(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{4}', c).group()
                            continue
                        if re.search(r'granted|refused|pending|received|withdrawn|determined', c, re.I) and len(c) < 30:
                            stat = c
                            continue
                        if not addr and re.search(r'CV\d|Road|Street|Avenue|Lane|Close|Drive|Way|Court|Grove', c, re.I):
                            addr = c
                            continue
                        if not desc and len(c) > 20:
                            desc = c
                    apps.append({
                        "reference":   ref,
                        "dateLodged":  date_l or NOW_UK.strftime("%-d %b %Y"),
                        "address":     addr or "Lower Stoke, Coventry",
                        "description": desc or "Click reference for full details.",
                        "status":      stat,
                        "portalLink":  app_link,
                        "source":      "planandregulatory.coventry.gov.uk",
                        "sourceUrl":   PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
                        "fetchedAt":   STAMP,
                        "storedAt":    NOW_UTC.timestamp()
                    })

    # ------------------------------------------------------------------
    # SOURCE 3: Manual entries — always included, never removed
    # These are applications you know about that may not yet be in the
    # government API. They are kept permanently.
    # ------------------------------------------------------------------
    MANUAL = [
        {
            "reference":   "PL/2026/0000951/TCA",
            "dateLodged":  "2026",
            "address":     "13 Central Avenue, Coventry, CV2 4DN",
            "description": "Trees in a Conservation Area. T1 Damson: Cut back overhanging lawn area. T2 Sycamore: Remove self-set Sycamore to ground level. T3 Lime: Reduce by 3-4m and cut back over garden.",
            "status":      "Under Consultation",
            "portalLink":  PORTAL + "?fa=getApplication&id=PL%2F2026%2F0000951%2FTCA",
            "source":      "planandregulatory.coventry.gov.uk",
            "sourceUrl":   PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
            "fetchedAt":   "Manually added",
            "storedAt":    1749340800
        },
        # Add more entries here if needed — copy the block above
    ]
    manual_refs = {a["reference"] for a in MANUAL}

    # ------------------------------------------------------------------
    # MERGE: rolling store + fresh API data + manual entries
    # ------------------------------------------------------------------
    store_path = DATA_DIR / "planning_store.json"
    stored = []
    if store_path.exists():
        try:
            stored = json.loads(store_path.read_text())
        except Exception:
            pass

    cutoff    = (NOW_UTC - timedelta(days=90)).timestamp()
    store_map = {}

    # Load stored history (drop entries older than 90 days, keep manual ones)
    for a in stored:
        if a.get("storedAt", 0) > cutoff or a.get("reference") in manual_refs:
            store_map[a["reference"]] = a

    # Manual entries fill gaps
    for a in MANUAL:
        if a["reference"] not in store_map:
            store_map[a["reference"]] = a

    # Fresh API data overwrites stored (newer info wins)
    seen = set()
    for a in apps:
        if a["reference"] not in seen:
            seen.add(a["reference"])
            store_map[a["reference"]] = a

    merged = sorted(store_map.values(),
                    key=lambda x: x.get("storedAt", 0), reverse=True)

    store_path.write_text(json.dumps(merged[:60], ensure_ascii=False, indent=2))
    print(f"  Total planning apps: {len(merged)}")
    for a in merged:
        print(f"    {a['reference']} | {a['address'][:45]} | {a['status']}")

    write_json("planning.json", merged)

# =============================================================================
# 3. WEST MIDLANDS POLICE
# =============================================================================
WMP_BASE   = "https://www.westmidlands.police.uk/area/your-area/west-midlands/coventry/stoke-and-wyken"
WMP_SUFFIX = "top-reported-crimes-in-this-area"

def wmp_fetch(section):
    r = safe_get(f"{WMP_BASE}/{section}/{WMP_SUFFIX}")
    return r.text if r and r.status_code == 200 else ""

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
                sib_text = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if not sib_text or len(sib_text) < 3:
                    continue
                if re.search(r'\d{1,2}:\d{2}(AM|PM)', sib_text, re.I) and re.search(r'\d{4}', sib_text):
                    date_str = sib_text
                elif not address and len(sib_text) > 5 and "calendar" not in sib_text.lower():
                    address = sib_text
                if hasattr(sib, "name") and sib.name in ["h4","h5","h3","h2"]:
                    break
            if title and date_str:
                events.append({
                    "title": title, "date": date_str,
                    "address": address or "Coventry",
                    "sourceUrl": f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}",
                    "fetchedAt": STAMP
                })
                print(f"  Event: {title} | {date_str}")

    # Known meetings always included
    wmp_url = f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}"
    known = [
        {"title":"Lower Stoke PACT Meeting",            "date":"6:00PM-7:00PM, Mon 08 June 2026","address":"St Margaret's Church, 50 Walsgrave Road, Ball Hill, Coventry"},
        {"title":"Community PACT Meeting - Upper Stoke", "date":"6:00PM-7:00PM, Mon 08 June 2026","address":"St Margaret's Church, 50 Walsgrave Road, Ball Hill, Coventry"},
        {"title":"Wyken Community PACT Meeting",         "date":"6:00PM-7:00PM, Tue 09 June 2026","address":"Wyken Community Centre, Westmorland Road, Coventry CV2 5PY"},
        {"title":"Community PACT Meeting - Upper Stoke", "date":"6:00PM-8:00PM, Fri 28 Aug 2026", "address":"Stoke St Michael's Church, Coventry"},
    ]
    existing = {e["title"].lower() for e in events}
    for k in known:
        if k["title"].lower() not in existing:
            events.append({**k, "sourceUrl": wmp_url, "fetchedAt": STAMP})

    print(f"  Total events: {len(events)}")
    write_json("police_events.json", events)

def scrape_police_team():
    print("\n-- Police Team --")
    team = []
    html = wmp_fetch("on-the-team")
    if html:
        soup     = BeautifulSoup(html, "html.parser")
        ranks_re = re.compile(
            r'\b(Inspector|Chief Inspector|Superintendent|Sergeant|Sgt|'
            r'Constable|Detective Constable|Detective Sergeant|PC|PCSO)\b', re.I)
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
                print(f"  Officer: {name} - {rank}")

    confirmed = {"name":"Manwar Porter","rank":"Inspector",
                 "bio":"Local Policing Inspector for the North East Sector of Coventry covering Stoke & Wyken. Primary focus on antisocial behaviour, vehicle crime, and retail crime.",
                 "sourceUrl":f"{WMP_BASE}/on-the-team/{WMP_SUFFIX}","fetchedAt":STAMP}
    if not any(t["name"] == "Manwar Porter" for t in team):
        team.insert(0, confirmed)

    print(f"  Total officers: {len(team)}")
    write_json("police_team.json", team)

def scrape_police_crimes():
    print("\n-- Police Crime Priorities --")
    priorities = []
    html = wmp_fetch("meetings-and-events")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        crimes_heading = soup.find(string=re.compile("Top reported crimes", re.I))
        if crimes_heading:
            section = crimes_heading.find_parent()
            for h4 in section.find_all_next("h4"):
                title = h4.get_text(strip=True)
                if not title or len(title) < 4:
                    continue
                if re.search(r'your local|on the team|about|contact|station|social|news|meeting|priority|crime map|crime level|crime per|footer', title, re.I):
                    break
                count_tag = h4.find_next_sibling()
                count = count_tag.get_text(strip=True) if count_tag else ""
                if not re.match(r'^\d+$', count):
                    count = ""
                priorities.append({
                    "title": title,
                    "issue": f"{count} reported (latest period, Stoke & Wyken)" if count else "Reported crime type in Stoke & Wyken",
                    "count": count,
                    "action": "West Midlands Police are actively targeting this crime type in the Stoke & Wyken neighbourhood.",
                    "status": "Active Priority",
                    "source": "westmidlands.police.uk",
                    "sourceUrl": f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}",
                    "fetchedAt": STAMP
                })
                print(f"  Crime: {title} ({count})")

    try:
        r = requests.get("https://data.police.uk/api/priorities?neighbourhood=west-midlands/stoke-and-wyken", timeout=10)
        if r.status_code == 200:
            for item in r.json():
                t = item.get("issue_title", "")
                if t and not any(p["title"].lower() == t.lower() for p in priorities):
                    priorities.append({
                        "title":     t,
                        "issue":     re.sub(r'<[^>]+>', '', item.get("issue","")).strip(),
                        "action":    re.sub(r'<[^>]+>', '', item.get("action","")).strip() or "Active policing response in place.",
                        "status":    "Active Priority",
                        "source":    "data.police.uk",
                        "sourceUrl": "https://data.police.uk",
                        "fetchedAt": STAMP
                    })
    except Exception as e:
        print(f"  data.police.uk error: {e}")

    if not priorities:
        wmp_url = f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}"
        priorities = [
            {"title":"Violence and Sexual Offences","issue":"161 reported (Apr 2026)","count":"161","action":"Targeted policing operations and victim support services in place.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
            {"title":"Shoplifting","issue":"45 reported (Apr 2026)","count":"45","action":"High-visibility patrols at retail locations including Binley Road.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
            {"title":"Criminal Damage and Arson","issue":"41 reported (Apr 2026)","count":"41","action":"Increased patrols in hotspot areas. Residents encouraged to report suspicious activity.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
            {"title":"Other Theft","issue":"33 reported (Apr 2026)","count":"33","action":"Intelligence-led operations targeting repeat offenders.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
        ]
    print(f"  Total crime priorities: {len(priorities)}")
    write_json("police_crimes.json", priorities)

# =============================================================================
# 4. CASEWORK LOG — Google Sheets public CSV
# Share the sheet: Share > Anyone with link > Viewer
# =============================================================================
SHEET_ID = "1CiCnq-WvIL0KmEv3RldjV0u9KxpTttHQkbN1igNILhQ"

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
            for h in headers:
                for kw in keywords:
                    if kw.lower() in h.lower():
                        return h
            return None

        c_title = col(["title","subject","issue","problem"])
        c_body  = col(["body","detail","description","update","note"])
        c_loc   = col(["location","address","area","street"])
        c_stat  = col(["status","stage","state"])
        c_log   = col(["logged by","logged","councillor","officer","assigned"])
        c_name  = col(["name","resident","contact"])
        c_email = col(["email"])
        c_date  = col(["date","when","received","timestamp"])

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
                "date":         (row.get(c_date,"")  if c_date  else "").strip(),
                "fetchedAt":    STAMP
            })
        print(f"  Cases: {len(cases)}")
    else:
        print("  Could not read sheet — make sure it is shared as 'Anyone with link can view'")
    write_json("casework.json", cases)

# =============================================================================
# 5. METADATA
# =============================================================================
def write_metadata():
    write_json("meta.json", {"lastUpdated": STAMP, "updatedAt": NOW_UTC.isoformat()})

# =============================================================================
# MAIN — exit code 0 even if individual sections fail (prevents GitHub
# treating a single source being down as a total deployment failure)
# =============================================================================
if __name__ == "__main__":
    print(f"=== Lower Stoke Ward Scraper - {STAMP} ===\n")
    for fn in [scrape_news, scrape_planning, scrape_police_events,
               scrape_police_team, scrape_police_crimes, scrape_casework,
               write_metadata]:
        try:
            fn()
        except Exception as e:
            print(f"ERROR in {fn.__name__}: {e}")
            traceback.print_exc()
    print("\n=== Done ===")
    sys.exit(0)   # Always exit 0 so GitHub doesn't mark the deploy as failed
