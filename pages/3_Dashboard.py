"""
Dashboard page.

By design this page does exactly two things: a quick, lightweight pull of
the latest saved KPI file from permanent storage (so it reflects the most
recent "Run Analysis" from anyone, on any device), and then displays it.
It NEVER recomputes any KPI itself -- that only ever happens on the Run
Analysis page -- which is what keeps this page opening instantly no matter
how large the underlying data archive has grown.

The dashboard's design (charts, filters, dark mode, the Documentation page
explaining exactly how each number is calculated) is the same standalone
HTML/CSS/JS dashboard built earlier in this project; this page just embeds
it with the latest numbers filled in.
"""
import os
import sys

import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))

from render_dashboard import render_dashboard_html, RenderError  # noqa: E402
from git_sync import pull_latest, sync_configured, GitSyncError  # noqa: E402

st.set_page_config(page_title="Dashboard -- Felbry KPI System", page_icon="📊", layout="wide")

# A quick metadata pull only -- this is a fast git fetch of already-computed
# numbers, not a recalculation, so it doesn't slow this page down.
if sync_configured():
    try:
        pull_latest()
    except GitSyncError:
        pass  # don't block viewing whatever is already on disk locally

try:
    html = render_dashboard_html()
except RenderError as e:
    st.title("📊 Dashboard")
    st.info(str(e), icon="ℹ️")
    st.stop()

components.html(html, height=1600, scrolling=True)
