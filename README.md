# Felbry College KPI System

A small internal web app that replaces manually rebuilding the KPI dashboard
every time new data comes in. Staff upload a new export, click one button to
recompute the KPIs, and the dashboard shows the latest numbers -- all for
free, and restricted to the school's own staff.

## How it works (the 3-page flow)

1. **Upload Data** -- a staff member picks a file (enrollment, withdrawal,
   gradebook, attendance, or LMS engagement export). The system looks at its
   column headers, guesses which type it is, and asks the staff member to
   confirm before saving it into the permanent archive.
2. **Run Analysis** -- one button. It reprocesses *everything* in the
   archive (not just the newest file) and saves a fresh KPI result.
3. **Dashboard** -- shows the KPI result from the last "Run Analysis" click.
   This page **never** recalculates anything itself, which is why it opens
   instantly -- it only reads numbers someone already computed.

This separation (upload → analyze → view) was a specific, explicit design
requirement: the dashboard must always be fast, so all the heavy computation
is pushed into its own separate step that a staff member triggers on
purpose.

## Why Streamlit + GitHub, and not Tableau / Power BI

Everything here needed to be free and to keep using the reconciliation logic
already built and validated for the real data (matching entry-cohort
enrollment records against term-snapshot exports -- see the dashboard's
Documentation page for why that matters). That ruled out:

- **Tableau Public** -- free, but every published workbook and its
  underlying data is public on the internet. Not appropriate for student
  records.
- **Power BI** -- sharing a live report with other people requires a paid
  Pro/Premium license.
- **No-code dashboard builders** -- would mean reimplementing the bespoke
  Python reconciliation logic in a much weaker environment, or not being
  able to do it at all.

**Streamlit Community Cloud** is free, runs actual Python (so the existing
pandas logic works unchanged), and its free tier includes exactly what this
project needs: **one private app** that only specific people can open --
real sign-in, not just an unlisted link. **GitHub** (also free for a
private repo) doubles as the app's permanent storage, since Streamlit
Community Cloud's own disk is wiped every time the app restarts.

## One-time setup

### 1. Put this code in a private GitHub repository

Create a **private** GitHub repository and push everything in this folder
to it. (`git init`, `git remote add origin <your repo URL>`, `git add -A`,
`git commit -m "Initial KPI system"`, `git push -u origin main`.)

### 2. Deploy it on Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with
   GitHub (free).
2. Click **New app**, pick the repository you just created, branch `main`,
   and set the main file to `app.py`.
3. Deploy. The first build takes a couple of minutes.

### 3. Lock the app down so nobody outside the school can open it

This is the non-negotiable part: **not even someone with the direct link
should be able to open this app if they're not staff.**

In the deployed app's menu, go to **Settings → Sharing**, and set
**"Who can view this app"** to **"Only specific people"**. Add the school
email address of every staff member who should have access. Streamlit then
shows a Google sign-in screen to anyone who tries to open the link, and
only lets through the emails on that list -- everyone else is blocked
before the app even loads, regardless of the link. Add or remove staff at
any time from that same settings screen; no code changes or redeploys
needed.

### 4. Connect persistent storage (required before real use)

Streamlit Community Cloud wipes the app's disk on every restart (a new
deploy, or just its normal weekly sleep/wake cycle). Without this step,
every upload and every "Run Analysis" result would be lost the next time
that happens. This app is built to back everything up to the same GitHub
repository it's deployed from, using a scoped access token:

1. On GitHub, go to **Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token**.
2. Give it a name like `felbry-kpi-system-app`, set the **Resource owner**
   and **Repository access** to only this one repository, and under
   **Permissions → Repository permissions**, set **Contents** to
   **Read and write**. Leave everything else as "No access".
3. Copy the generated token (it starts with `github_pat_...` and is only
   shown once).
4. In the Streamlit app's **Settings → Secrets**, paste:

   ```toml
   GITHUB_TOKEN = "github_pat_...your token..."
   GITHUB_REPO = "your-org-or-username/your-repo-name"
   GITHUB_BRANCH = "main"
   ```
5. Save. The app restarts automatically. The home page will confirm
   persistent storage is connected (the warning banner disappears).

If two staff members use the app at the same moment, the app automatically
re-syncs and retries a save rather than failing -- this was tested against
a simulated two-user conflict during development.

### 5. Load the existing data

The `data_archive/` folder in this repository already contains the first
batch of real data (enrollment, withdrawal, gradebook, attendance, and LMS
engagement exports collected on 2026-09-26) and its computed KPI output, so
the dashboard has real numbers from the moment the app is deployed. From
then on, staff only need to upload *new* files as they arrive -- the
archive keeps growing, and clicking **Run Analysis** always reprocesses the
whole thing (old files included), which is what correctly handles a student
who, say, re-enrolled after withdrawing.

## Data-quality note found while building this

While generalizing the original one-off analysis script to run over an
open-ended, growing archive, a real data-export issue turned up: several of
the school's **withdrawal** and **attendance** export files for later terms
(Spring 2026 and especially Summer 2026) turned out to substantially
**overlap** with earlier terms' files -- in the Summer 2026 attendance
file, essentially every row (11,589 of 11,593) was already present in an
earlier export, and the Summer 2026 withdrawal file was a complete repeat
of the Spring 2026 one. The original one-off script assumed one file =
one term and would have (and did) double-counted these repeated rows under
whichever term the file was named for.

This system fixes that at the source: every withdrawal, attendance, and
gradebook row is matched to its term using its own date (not the filename
it happened to arrive in) and duplicate rows are automatically detected and
kept only once, using the most-recently-uploaded copy. In this batch that
brought total withdrawal rows from 227 down to 192 real ones (35 exact
repeats removed) and corrected the affected terms' withdrawal and
attendance rates downward. **The dashboard numbers this system produces are
more accurate than the first delivered prototype's** -- if that matters for
anything already reported from the earlier numbers, it's worth knowing the
later terms' withdrawal/attendance rates were previously overstated.

## Project layout

```
app.py                      Home page
pages/1_Upload_Data.py      Upload & auto-detect
pages/2_Run_Analysis.py     Recompute KPIs (the only place analysis runs)
pages/3_Dashboard.py        Displays the last computed KPIs, instantly
analysis/detectors.py       File-type auto-detection by column signature
analysis/pipeline.py        All KPI computation logic
analysis/git_sync.py        GitHub-backed persistence
analysis/render_dashboard.py  Injects the latest KPI JSON into the dashboard HTML
dashboard_template.html     The dashboard's design (charts, filters, Documentation page)
data_archive/                Permanent, ever-growing store of uploaded files
data_archive/kpi_output/     The last computed KPI result (what the dashboard reads)
```

## A note on testing

This app's Python logic (file detection, the KPI pipeline, the GitHub sync
retry behavior, and the dashboard's HTML/JS after fresh data is injected)
was all tested directly and passed, including against the real 2026-09-26
data batch and a simulated two-staff-member concurrent upload. The
Streamlit framework itself could not be installed or run in the environment
this was built in (its network access is restricted), so the page layouts
and Streamlit-specific widgets have not been visually verified end-to-end.
After deploying, click through all three pages once with a small test file
to confirm everything looks right, and check the app's deploy logs on
Streamlit Cloud if anything doesn't come up as expected.
