"""
Run Analysis page.

One button. Clicking it:
  1. Pulls the latest archive from permanent storage (in case a different
     staff session uploaded something since this session started).
  2. Recomputes every KPI from everything currently in the archive.
  3. Saves the result and backs it up to permanent storage.

This is deliberately the ONLY place in the whole system where the actual
number-crunching happens. The Dashboard page only ever reads what this page
produced -- it never recalculates anything, so it stays fast no matter how
much data has piled up in the archive.
"""
import glob
import json
import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))

from pipeline import write_kpi_output, PipelineError  # noqa: E402
from git_sync import pull_latest, commit_and_push, sync_configured, GitSyncError  # noqa: E402

st.set_page_config(page_title="Run Analysis -- Felbry KPI System", page_icon="🔄", layout="wide")
st.title("🔄 Run Analysis")

if sync_configured():
    try:
        pull_latest()
    except GitSyncError as e:
        st.warning(str(e), icon="⚠️")
else:
    st.warning(
        "Persistent storage isn't connected (no GITHUB_TOKEN configured). "
        "Analysis will still run, but the result will be lost when this app "
        "restarts.",
        icon="⚠️",
    )

ARCHIVE_TYPES = ["enrollment", "withdrawal", "gradebook", "attendance", "engagement"]
KPI_PATH = os.path.join("data_archive", "kpi_output", "REAL_DATA.json")


def _counts():
    out = {}
    for t in ARCHIVE_TYPES:
        out[t] = len(glob.glob(os.path.join("data_archive", t, "*")))
    return out


st.subheader("What's currently in the archive")
counts = _counts()
cols = st.columns(len(ARCHIVE_TYPES))
for col, (t, n) in zip(cols, counts.items()):
    col.metric(t.title(), n)

if sum(counts.values()) == 0:
    st.info("The archive is empty. Go to **Upload Data** first.")
    st.stop()

previous_summary = None
if os.path.exists(KPI_PATH):
    with open(KPI_PATH) as f:
        previous = json.load(f)
    previous_summary = previous.get("_archive_summary")
    st.caption(f"Last run: {previous.get('generated_at', 'unknown')}")

st.divider()

if st.button("▶️ Run Analysis", type="primary"):
    started = time.time()
    with st.spinner("Recomputing every KPI from the full archive -- this can "
                     "take a little while as more data accumulates..."):
        try:
            result = write_kpi_output(archive_root="data_archive")
        except PipelineError as e:
            st.error(str(e), icon="🚫")
            st.stop()
        except Exception as e:
            st.error(f"Something went wrong during analysis: {e}", icon="🚫")
            st.stop()
    elapsed = time.time() - started

    st.success(f"Analysis complete in {elapsed:.1f} seconds.", icon="✅")

    summary = result["_archive_summary"]
    m1, m2, m3 = st.columns(3)
    m1.metric(
        "Students covered",
        summary["roster_unique_students"],
        delta=(
            summary["roster_unique_students"] - previous_summary["roster_unique_students"]
            if previous_summary else None
        ),
    )
    m2.metric("Terms covered", len(result["term_order"]))
    m3.metric(
        "Enrollment rows",
        summary["roster_rows"],
        delta=(summary["roster_rows"] - previous_summary["roster_rows"]) if previous_summary else None,
    )

    if sync_configured():
        try:
            pushed = commit_and_push(
                [KPI_PATH],
                message=f"Recompute KPIs ({result['generated_at']})",
            )
            if pushed:
                st.success("New KPI results backed up to permanent storage.", icon="💾")
            else:
                st.info("Results matched the previous run exactly -- nothing new to back up.")
        except GitSyncError as e:
            st.error(
                f"KPIs were recomputed on this server but could NOT be backed "
                f"up to permanent storage: {e}",
                icon="🚫",
            )
    st.page_link("pages/3_Dashboard.py", label="Go to the Dashboard →", icon="📊") \
        if hasattr(st, "page_link") else st.caption("Go to the Dashboard page in the sidebar to view the results.")
