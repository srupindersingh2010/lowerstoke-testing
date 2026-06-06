# Lower Stoke Ward Action Hub — GitHub Setup Guide

## What you get
- A free website hosted at `https://YOUR-USERNAME.github.io/lowerstoke/`
- Automatic daily data updates at 08:00 every morning
- All data comes from live sources: Coventry Council RSS, planning portal,
  West Midlands Police website, and your Google Sheet casework log

---

## STEP 1 — Create a free GitHub account
1. Go to **https://github.com** and click **Sign up**
2. Choose a username (e.g. `lowerstokeward`)
3. Verify your email address

---

## STEP 2 — Create a new repository
1. Click the **+** icon (top right) → **New repository**
2. Name it exactly: `lowerstoke`
3. Set it to **Public** (required for free GitHub Pages)
4. Tick **"Add a README file"**
5. Click **Create repository**

---

## STEP 3 — Upload the files
You need to upload these files in the correct folders:

```
lowerstoke/              ← your repository root
├── index.html           ← upload this
├── scrape.py            ← upload this
├── data/                ← create this folder, upload all .json files inside it
│   ├── meta.json
│   ├── news.json
│   ├── planning.json
│   ├── planning_store.json
│   ├── police_events.json
│   ├── police_team.json
│   ├── police_crimes.json
│   ├── casework.json
│   └── gallery.json
└── .github/
    └── workflows/
        └── daily-update.yml   ← upload this (the automation)
```

### How to upload:
1. In your repository, click **"Add file"** → **"Upload files"**
2. Drag `index.html` and `scrape.py` from your computer and click **Commit changes**
3. Click **"Add file"** → **"Create new file"**
4. Type `data/meta.json` as the filename — GitHub creates the folder automatically
5. Paste the contents of `meta.json` and click **Commit new file**
6. Repeat for each file in the `data/` folder
7. Then create `.github/workflows/daily-update.yml` the same way

---

## STEP 4 — Enable GitHub Pages
1. In your repository, click **Settings** (top menu)
2. Click **Pages** (left sidebar)
3. Under "Source", select **"Deploy from a branch"**
4. Branch: **main**, Folder: **/ (root)**
5. Click **Save**
6. After 1–2 minutes your site is live at:
   `https://YOUR-USERNAME.github.io/lowerstoke/`

---

## STEP 5 — Share your Google Sheet (for casework)
The scraper reads your casework spreadsheet as a public CSV.
You must share it so the automation can read it:

1. Open your Google Sheet:
   `https://docs.google.com/spreadsheets/d/1CiCnq-WvIL0KmEv3RldjV0u9KxpTttHQkbN1igNILhQ`
2. Click **Share** (top right)
3. Under "General access", change to **"Anyone with the link"** → **Viewer**
4. Click **Done**

The sheet content is read-only — nobody can edit it, only view it.

---

## STEP 6 — Run the scraper for the first time
1. In your repository, click **Actions** (top menu)
2. Click **"Daily Data Update & Deploy"** (left sidebar)
3. Click **"Run workflow"** → **"Run workflow"** (green button)
4. Wait 1–2 minutes — you'll see a green tick when it succeeds
5. Your website now has live data!

After this, it runs automatically every morning at 08:00 without you doing anything.

---

## STEP 7 — Check it works
Visit your site: `https://YOUR-USERNAME.github.io/lowerstoke/`

The banner at the top will show today's date and time if everything worked.

---

## Keeping things updated

### When a new PACT meeting is announced:
Open `scrape.py`, find the `known` list in `scrape_police_events()`, and add a new entry:
```python
{"title": "Lower Stoke PACT Meeting", "date": "Mon 07 Sep 2026",
 "time": "6:00PM – 7:00PM", "address": "St Margaret's Church, Ball Hill, Coventry"},
```
Then commit the file. The next daily run will include it.

### When a new officer joins the team:
Open `scrape.py`, find `confirmed` in `scrape_police_team()`, add another entry to the list.

### When you know of a planning application not being picked up:
Open `scrape.py`, find the `store_map` section in `scrape_planning()` and add it manually,
or simply add it to the `data/planning_store.json` file directly.

### Casework log:
Just update your Google Sheet as normal — the next morning's automatic run picks it up.

---

## Your website URL
Once set up: `https://YOUR-USERNAME.github.io/lowerstoke/`

You can also set up a custom domain (e.g. `lowerstoke.co.uk`) for free via GitHub Pages settings.

---

## Need help?
All files are standard HTML, Python and YAML — if anything stops working,
the GitHub Actions log (Actions tab → click the failed run) shows exactly what went wrong.
