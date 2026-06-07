"""
Lower Stoke Ward — Daily Data Scraper
Runs via GitHub Actions every day at 08:00.

PLANNING SPREADSHEET WORKFLOW:
  - Each week, save the planning spreadsheet you receive by email
    into this Google Drive folder:
    https://drive.google.com/drive/folders/1reuhUHzInEHjWdOT4lHEHsQbmYRSl-qo
  - The scraper reads it automatically next morning.
  - No renaming needed — it always uses the newest file.
"""

import json, re, sys, traceback, csv, io, os, tempfile
from datetime import datetime, timezone, timedelta, date as dt_date
from pathlib import Path
import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NOW_UTC = datetime.now(timezone.utc)
NOW_UK  = NOW_UTC + timedelta(hours=1)
STAMP   = NOW_UK.strftime("%-d %B %Y at %H:%M")

PORTAL             = "https://planandregulatory.coventry.gov.uk/planning/index.html"
PLANNING_FOLDER_ID = "1reuhUHzInEHjWdOT4lHEHsQbmYRSl-qo"
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

# =============================================================================
# GOOGLE DRIVE API — service account authentication
# Reads GDRIVE_SERVICE_ACCOUNT secret from environment (set in GitHub secrets)
# =============================================================================
def get_drive_service():
    """
    Returns an authenticated Google Drive API session using the service account
    JSON stored in the GDRIVE_SERVICE_ACCOUNT environment variable.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT", "")
        if not sa_json:
            print("  WARNING: GDRIVE_SERVICE_ACCOUNT secret not set")
            return None

        sa_info = json.loads(sa_json)
        creds   = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        print("  Google Drive API authenticated OK")
        return service
    except Exception as e:
        print(f"  Drive API auth error: {e}")
        traceback.print_exc()
        return None

def list_drive_folder_api(service, folder_id):
    """
    Lists all files in a Drive folder using the API.
    Returns list sorted newest modified first.
    """
    try:
        result = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=50
        ).execute()
        files = result.get("files", [])
        print(f"  Drive folder contains {len(files)} file(s):")
        for f in files:
            print(f"    {f['name']} | {f['mimeType']} | {f.get('modifiedTime','')[:10]}")
        return files
    except Exception as e:
        print(f"  Drive list error: {e}")
        traceback.print_exc()
        return []

def download_drive_file_api(service, file_id, mime_type, file_name):
    """
    Downloads a Drive file and returns its content as text.
    Handles Google Sheets (export to CSV) and uploaded CSV/Excel files.
    """
    try:
        if "google-apps.spreadsheet" in mime_type:
            # Google Sheets — export as CSV
            request = service.files().export_media(
                fileId=file_id,
                mimeType="text/csv"
            )
        else:
            # Uploaded file (CSV, Excel, etc.) — download directly
            request = service.files().get_media(fileId=file_id)

        content = request.execute()
        if isinstance(content, bytes):
            # Try UTF-8 first, then latin-1 for Excel exports
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1")
        return str(content)
    except Exception as e:
        print(f"  Drive download error for {file_name}: {e}")
        traceback.print_exc()
        return None

# =============================================================================
# PARSE PLANNING SPREADSHEET
# Coventry planning export columns (typical):
#   Reference | Application Type | Address | Proposal | Ward | Status | Date
# We filter rows where Ward contains "Lower Stoke"
# =============================================================================
def parse_planning_csv(csv_text, source_label):
    apps = []
    try:
        # Handle Excel files that may have been converted — clean BOM
        csv_text = csv_text.lstrip("\ufeff")
        reader   = csv.DictReader(io.StringIO(csv_text))
        headers  = reader.fieldnames or []
        print(f"  Spreadsheet columns: {headers}")

        def col(keywords):
            for h in (headers or []):
                for kw in keywords:
                    if kw.lower() in h.lower():
                        return h
            return None

        c_ref    = col(["reference", "ref", "app ref", "application ref", "app no"])
        c_type   = col(["type", "application type", "app type"])
        c_addr   = col(["address", "location", "site", "property"])
        c_desc   = col(["proposal", "description", "development", "works"])
        c_ward   = col(["ward"])
        c_status = col(["status", "decision", "stage", "current status"])
        c_date   = col(["date", "received", "validated", "lodged", "registered"])

        print(f"  Column mapping: ref={c_ref} addr={c_addr} ward={c_ward} status={c_status}")

        row_count = 0
        for row in reader:
            row_count += 1
            # Filter for Lower Stoke ward — check ward column AND full row text
            ward_val = (row.get(c_ward, "") if c_ward else "").strip()
            row_text = " ".join(str(v) for v in row.values())
            if "lower stoke" not in ward_val.lower() and "lower stoke" not in row_text.lower():
                continue

            ref   = (row.get(c_ref,    "") if c_ref    else "").strip()
            addr  = (row.get(c_addr,   "") if c_addr   else "").strip()
            desc  = (row.get(c_desc,   "") if c_desc   else "").strip()
            atype = (row.get(c_type,   "") if c_type   else "").strip()
            stat  = (row.get(c_status, "") if c_status else "Received").strip() or "Received"
            date  = (row.get(c_date,   "") if c_date   else "").strip()

            if not ref and not addr:
                continue

            # Combine application type + description for a richer display
            full_desc = desc
            if atype and desc and atype.lower() not in desc.lower():
                full_desc = f"{atype} — {desc}"
            elif atype and not desc:
                full_desc = atype

            ref_enc = ref.replace("/", "%2F")
            apps.append({
                "reference":   ref or "Unknown",
                "dateLodged":  date or STAMP,
                "address":     addr or "Lower Stoke, Coventry",
                "description": full_desc or "Click reference for full details on the planning portal.",
                "status":      stat,
                "portalLink":  f"{PORTAL}?fa=getApplication&id={ref_enc}" if ref else PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
                "source":      source_label,
                "sourceUrl":   PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
                "fetchedAt":   STAMP,
                "storedAt":    NOW_UTC.timestamp()
            })
            print(f"  Lower Stoke: {ref} | {addr[:45]} | {stat}")

        print(f"  Rows read: {row_count}, Lower Stoke matches: {len(apps)}")
    except Exception as e:
        print(f"  CSV parse error: {e}")
        traceback.print_exc()
    return apps

# =============================================================================
# 1. COUNCIL NEWS
# =============================================================================
def scrape_news():
    print("\n-- Council News --")
    entries = []
    r = safe_get("https://www.coventry.gov.uk/news")
    if r and r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")
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

            # Get published date from <strong>Published:...</strong> in same li
            date_str = ""
            strong = li.find("strong", string=re.compile(r"Published", re.I))
            if strong:
                date_str = strong.get_text(strip=True).replace("Published:", "").strip()

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
#    Priority: A) Drive folder spreadsheet  B) planning.data.gov.uk API
#              C) Coventry weekly list       D) Manual hardcoded entries
# =============================================================================
def scrape_planning():
    print("\n-- Planning Applications --")
    apps = []

    # ------------------------------------------------------------------
    # SOURCE A: Google Drive folder — weekly spreadsheet from your email
    # ------------------------------------------------------------------
    print("  Checking Drive planning folder via API...")
    drive = get_drive_service()
    if drive:
        files = list_drive_folder_api(drive, PLANNING_FOLDER_ID)

        # Find spreadsheet or CSV files — newest first (already sorted)
        planning_files = [
            f for f in files
            if any(x in f.get("mimeType","") for x in
                   ["spreadsheet","csv","excel","sheet","vnd.ms-excel",
                    "officedocument.spreadsheetml","text/plain"])
            or any(f["name"].lower().endswith(e) for e in [".csv",".xlsx",".xls",".ods"])
        ]
        print(f"  Spreadsheet files found: {len(planning_files)}")

        for pf in planning_files[:3]:   # try up to 3 newest files
            print(f"  Reading: {pf['name']}")
            csv_text = download_drive_file_api(
                drive, pf["id"], pf.get("mimeType",""), pf["name"])
            if csv_text:
                found = parse_planning_csv(csv_text, f"Coventry Planning (via Drive: {pf['name']})")
                if found:
                    apps.extend(found)
                    print(f"  Got {len(found)} Lower Stoke apps from {pf['name']}")
                    break   # stop once we find a file with Lower Stoke data
            else:
                print(f"  Could not read {pf['name']}")
    else:
        print("  Drive API not available — skipping Drive source")

    # ------------------------------------------------------------------
    # SOURCE B: planning.data.gov.uk open API (free, no blocking)
    # Lower Stoke ward entity = 800137
    # ------------------------------------------------------------------
    try:
        ninety_ago = NOW_UTC - timedelta(days=90)
        # Single f-string avoids multiline concat dropping variables
        api_url = f"https://www.planning.data.gov.uk/entity.json?dataset=planning-application&geometry_entity=800137&geometry_relation=intersects&entry_date_year={ninety_ago.year}&entry_date_month={ninety_ago.month}&entry_date_day={ninety_ago.day}&entry_date_match=after&limit=100"
        print(f"  Calling: {api_url}")
        r = safe_get(api_url)
        if r and r.status_code == 200:
            entities = r.json().get("entities", [])
            print(f"  planning.data.gov.uk: {len(entities)} entities returned")
            existing_refs = {a["reference"] for a in apps}
            for e in entities:
                ref  = (e.get("reference") or "").strip()
                if not ref or ref in existing_refs:
                    continue
                addr = (e.get("name") or e.get("address-text") or "Lower Stoke, Coventry").strip()
                desc = (e.get("description") or e.get("development-description") or
                        "Click reference to view full details.").strip()
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
                print(f"  API: {ref} | {addr[:45]} | {stat}")
        else:
            print(f"  planning.data.gov.uk status: {r.status_code if r else 'no response'}")
    except Exception as e:
        print(f"  planning.data.gov.uk error: {e}")

    # ------------------------------------------------------------------
    # SOURCE C: Coventry weekly list (often blocked — last resort)
    # ------------------------------------------------------------------
    if not apps:
        print("  Trying Coventry weekly list...")
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
                        "reference": ref, "dateLodged": date_l or NOW_UK.strftime("%-d %b %Y"),
                        "address": addr or "Lower Stoke, Coventry",
                        "description": desc or "Click reference for full details.",
                        "status": stat, "portalLink": app_link,
                        "source": "planandregulatory.coventry.gov.uk",
                        "sourceUrl": PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
                        "fetchedAt": STAMP, "storedAt": NOW_UTC.timestamp()
                    })

    # ------------------------------------------------------------------
    # SOURCE D: Manual entries — always shown, never removed
    # ------------------------------------------------------------------
    MANUAL = [
        {
            "reference":   "PL/2026/0000951/TCA",
            "dateLodged":  "2026",
            "address":     "13 Central Avenue, Coventry, CV2 4DN",
            "description": "Trees in a Conservation Area. T1 Damson: Cut back overhanging lawn. T2 Sycamore: Remove self-set Sycamore to ground level. T3 Lime: Reduce by 3-4m and cut back over garden.",
            "status":      "Under Consultation",
            "portalLink":  PORTAL + "?fa=getApplication&id=PL%2F2026%2F0000951%2FTCA",
            "source":      "planandregulatory.coventry.gov.uk",
            "sourceUrl":   PORTAL + "?fa=getApplications&ward=Lower%20Stoke",
            "fetchedAt":   "Manually added",
            "storedAt":    1749340800
        },
    ]
    manual_refs = {a["reference"] for a in MANUAL}

    # ------------------------------------------------------------------
    # MERGE into rolling 90-day store
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
    for a in stored:
        if a.get("storedAt", 0) > cutoff or a.get("reference") in manual_refs:
            store_map[a["reference"]] = a
    for a in MANUAL:
        if a["reference"] not in store_map:
            store_map[a["reference"]] = a
    seen_refs = set()
    for a in apps:
        ref = a.get("reference","")
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            store_map[ref] = a

    merged = sorted(store_map.values(),
                    key=lambda x: x.get("storedAt", 0), reverse=True)
    merged = [a for a in merged
              if a.get("storedAt", 0) > cutoff or a.get("reference") in manual_refs]

    store_path.write_text(json.dumps(merged[:60], ensure_ascii=False, indent=2))
    print(f"  Total planning apps: {len(merged)}")
    for a in merged:
        print(f"    {a['reference']} | {a['address'][:40]} | {a['status']}")
    write_json("planning.json", merged)

# =============================================================================
# 3. WEST MIDLANDS POLICE
# =============================================================================
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
                events.append({"title": title, "date": date_str,
                                "address": address or "Coventry",
                                "sourceUrl": f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}",
                                "fetchedAt": STAMP})
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
                    "issue": f"{count} reported (latest period, Stoke & Wyken)" if count else "Reported in Stoke & Wyken",
                    "count": count,
                    "action": "West Midlands Police are actively targeting this crime type in the Stoke & Wyken neighbourhood.",
                    "status": "Active Priority",
                    "source": "westmidlands.police.uk",
                    "sourceUrl": f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}",
                    "fetchedAt": STAMP
                })
    try:
        r = requests.get("https://data.police.uk/api/priorities?neighbourhood=west-midlands/stoke-and-wyken", timeout=10)
        if r.status_code == 200:
            for item in r.json():
                t = item.get("issue_title","")
                if t and not any(p["title"].lower() == t.lower() for p in priorities):
                    priorities.append({
                        "title": t,
                        "issue": re.sub(r'<[^>]+>','',item.get("issue","")).strip(),
                        "action": re.sub(r'<[^>]+>','',item.get("action","")).strip() or "Active policing response in place.",
                        "status": "Active Priority", "source": "data.police.uk",
                        "sourceUrl": "https://data.police.uk", "fetchedAt": STAMP
                    })
    except Exception as e:
        print(f"  data.police.uk error: {e}")
    if not priorities:
        wmp_url = f"{WMP_BASE}/meetings-and-events/{WMP_SUFFIX}"
        priorities = [
            {"title":"Violence and Sexual Offences","issue":"161 reported (Apr 2026)","count":"161","action":"Targeted policing operations and victim support services in place.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
            {"title":"Shoplifting","issue":"45 reported (Apr 2026)","count":"45","action":"High-visibility patrols at retail locations including Binley Road.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
            {"title":"Criminal Damage and Arson","issue":"41 reported (Apr 2026)","count":"41","action":"Increased patrols in hotspot areas.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
            {"title":"Other Theft","issue":"33 reported (Apr 2026)","count":"33","action":"Intelligence-led operations targeting repeat offenders.","status":"Active Priority","source":"westmidlands.police.uk","sourceUrl":wmp_url,"fetchedAt":STAMP},
        ]
    print(f"  Total crime priorities: {len(priorities)}")
    write_json("police_crimes.json", priorities)

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
       c_date  = col(["date","when","received","timestamp","start time","start"])
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
    sys.exit(0)
