"""
Felbry College KPI System -- home page.

This is a small internal tool with exactly three jobs, one per page in the
sidebar:

  1. Upload Data     -- add a new export file to the permanent archive.
  2. Run Analysis     -- recompute every KPI from everything in the archive
                         and save the results.
  3. Dashboard        -- view the results. This page NEVER recalculates
                         anything itself -- it only displays whatever the
                         last "Run Analysis" produced, so it opens instantly.

WHO CAN SEE THIS APP: access control is configured on Streamlit Community
Cloud's side (App -> Settings -> Sharing -> Who can view this app), not in
this code. When that is set to "Only specific people" with the school's
staff emails listed, Streamlit itself shows a Google sign-in screen before
anyone reaches so much as this page -- nobody outside that list can open the
app even with the direct link. See README.md for the exact setup steps.
"""
import os
import sys
from datetime import datetime, timezone

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis"))

from git_sync import pull_latest, sync_configured, GitSyncError  # noqa: E402

st.set_page_config(page_title="Felbry College KPI System", page_icon="🎓", layout="wide")

KPI_PATH = os.path.join("data_archive", "kpi_output", "REAL_DATA.json")
ARCHIVE_TYPES = ["enrollment", "withdrawal", "gradebook", "attendance", "engagement"]


def _current_viewer() -> str | None:
    """Returns the signed-in viewer's email when running on Streamlit
    Community Cloud with viewer authentication turned on; None otherwise
    (e.g. running locally). Defensive against Streamlit version differences
    since this attribute has moved/been renamed across releases."""
    for attr in ("user", "experimental_user"):
        user_obj = getattr(st, attr, None)
        if user_obj is not None:
            email = getattr(user_obj, "email", None)
            if email:
                return email
    return None


def _archive_counts() -> dict:
    counts = {}
    for t in ARCHIVE_TYPES:
        folder = os.path.join("data_archive", t)
        if os.path.isdir(folder):
            counts[t] = len([f for f in os.listdir(folder) if not f.startswith(".")])
        else:
            counts[t] = 0
    return counts


# --- Keep the local archive in sync with GitHub before showing anything ----
sync_warning = None
if sync_configured():
    try:
        pull_latest()
    except GitSyncError as e:
        sync_warning = str(e)
else:
    sync_warning = (
        "Persistent storage isn't connected yet (no GITHUB_TOKEN configured). "
        "Uploads and analysis results will be lost the next time this app "
        "restarts. This must be fixed before real use -- see the README's "
        "'Connecting persistent storage' section."
    )

st.title("🎓 Felbry College KPI System")

viewer = _current_viewer()
if viewer:
    st.caption(f"Signed in as **{viewer}**")

if sync_warning:
    st.warning(sync_warning, icon="⚠️")

st.markdown(
    """
This tool keeps the College's KPI dashboard up to date from the school's own
data exports. There are three steps, one per page in the left sidebar:

1. **Upload Data** -- add a new enrollment, withdrawal, gradebook, attendance,
   or LMS engagement export. The system figures out which kind of file it is
   automatically and asks you to confirm before saving it.
2. **Run Analysis** -- click one button to recompute every KPI from
   everything that's been uploaded so far. This takes a little while (it's
   reprocessing all the data), which is exactly why it's a separate step
   from viewing the dashboard.
3. **Dashboard** -- view the results. This page always opens instantly
   because it just displays the numbers from the last time someone clicked
   "Run Analysis" -- it never recalculates anything on its own.
"""
)

st.divider()
col1, col2 = st.columns(2)

with col1:
    st.subheader("What's in the archive right now")
    counts = _archive_counts()
    for t, n in counts.items():
        st.write(f"**{t.title()}**: {n} file(s)")

with col2:
    st.subheader("Last analysis run")
    if os.path.exists(KPI_PATH):
        import json
        with open(KPI_PATH) as f:
            kpi = json.load(f)
        st.write(f"Generated: **{kpi.get('generated_at', 'unknown')}**")
        summary = kpi.get("_archive_summary", {})
        st.write(f"Covers **{summary.get('roster_unique_students', '?')}** students "
                 f"across **{len(kpi.get('term_order', []))}** terms.")
        st.caption("Go to the Dashboard page in the sidebar to view it.")
    else:
        st.info("No analysis has been run yet. Upload some data, then use "
                "the Run Analysis page.")
