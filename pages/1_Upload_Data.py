"""
Upload Data page.

Flow for the staff member:
  1. Pick one or more files (drag-and-drop or browse).
  2. For each file, optionally say what type it is if they already know --
     defaulting to "Not sure -- let the system detect". Either way, the
     system also scores the file's column headers against every known data
     type and shows what it thinks, including what dashboard KPIs that type
     feeds, and flags it if a manual pick doesn't match what the columns
     look like.
  3. The staff member confirms (or corrects) the type for each file using a
     dropdown -- nothing is ever saved without an explicit confirmation
     click, so a wrong guess (manual or automatic) can't silently corrupt
     the archive.
  4. Saved files are archived permanently (backed up to GitHub) and are
     picked up automatically the next time someone clicks "Run Analysis" --
     uploading here does NOT recompute anything by itself.
"""
import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))

from detectors import detect_file, TYPE_LABELS, TYPE_FEEDS  # noqa: E402
from git_sync import pull_latest, commit_and_push, sync_configured, GitSyncError  # noqa: E402

NOT_SURE = "__not_sure__"

st.set_page_config(page_title="Upload Data -- Felbry KPI System", page_icon="📤", layout="wide")
st.title("📤 Upload Data")

if sync_configured():
    try:
        pull_latest()
    except GitSyncError as e:
        st.warning(str(e), icon="⚠️")
else:
    st.error(
        "Persistent storage isn't connected (no GITHUB_TOKEN configured). "
        "Anything you upload right now will be lost the next time this app "
        "restarts. Please tell whoever manages this app before uploading "
        "real data.",
        icon="🚫",
    )

st.markdown(
    "Upload one or more export files below (enrollment, withdrawal, gradebook, "
    "attendance, LMS engagement, absenteeism hotspots, academic performance, "
    "withdrawals before midterm, GPA trend, or ladder rate -- CSV or Excel). "
    "The system will guess what each one is; please double-check its guess "
    "before saving."
)

uploaded_files = st.file_uploader(
    "Choose file(s)",
    type=["csv", "xlsx", "xls"],
    accept_multiple_files=True,
)

if "confirmed_types" not in st.session_state:
    st.session_state.confirmed_types = {}

if uploaded_files:
    st.divider()
    to_save = []
    type_options = list(TYPE_LABELS.keys())
    for uf in uploaded_files:
        file_bytes = uf.getvalue()
        result, df = detect_file(file_bytes, uf.name)

        with st.container(border=True):
            st.markdown(f"**{uf.name}**")
            st.caption(f"{len(file_bytes):,} bytes")

            preselect_cols = st.columns([3, 3])
            preselected = preselect_cols[0].selectbox(
                "If you already know the data type, pick it here (optional)",
                [NOT_SURE] + type_options,
                index=0,
                format_func=lambda t: "Not sure -- let the system detect" if t == NOT_SURE else TYPE_LABELS[t],
                key=f"preselect_{uf.name}",
            )

            detect_box = preselect_cols[1]
            if preselected == NOT_SURE:
                if result.needs_confirmation:
                    detect_box.warning(result.message, icon="❓")
                else:
                    detect_box.success(result.message, icon="✅")
            else:
                # Staff told us what it is -- still show what the columns
                # look like as a sanity check, so a wrong manual pick gets
                # caught before it's saved rather than silently corrupting
                # the archive.
                feeds = TYPE_FEEDS.get(preselected, "")
                detect_box.info(
                    f"You selected: {TYPE_LABELS[preselected]}."
                    + (f" This updates: {feeds}" if feeds else ""),
                    icon="✅",
                )
                if (
                    not result.needs_confirmation
                    and result.detected_type is not None
                    and result.detected_type != preselected
                ):
                    detect_box.warning(
                        f"Heads up: based on its column headers, this file actually "
                        f"looks like **{TYPE_LABELS[result.detected_type]}** "
                        f"(confidence {result.confidence:.0%}), not "
                        f"**{TYPE_LABELS[preselected]}**. Double-check which one is "
                        f"right before saving.",
                        icon="⚠️",
                    )

            default_type = preselected if preselected != NOT_SURE else result.detected_type
            default_index = (
                type_options.index(default_type)
                if default_type in type_options
                else 0
            )
            chosen_type = st.selectbox(
                "Confirm data type to save as",
                type_options,
                index=default_index,
                format_func=lambda t: TYPE_LABELS[t],
                key=f"type_{uf.name}",
            )

            if df is not None:
                with st.expander("Preview first 5 rows"):
                    st.dataframe(df.head(5), use_container_width=True)

            to_save.append((uf.name, file_bytes, chosen_type))

    st.divider()
    if st.button("💾 Save to archive", type="primary", disabled=not sync_configured()):
        saved_paths = []
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        for name, file_bytes, chosen_type in to_save:
            safe_name = f"{timestamp}__{name}"
            dest_dir = os.path.join("data_archive", chosen_type)
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, safe_name)
            with open(dest_path, "wb") as f:
                f.write(file_bytes)
            saved_paths.append(dest_path)

        try:
            names = ", ".join(os.path.basename(p) for p in saved_paths)
            pushed = commit_and_push(
                saved_paths,
                message=f"Archive upload: {names}",
            )
            st.success(
                f"Saved {len(saved_paths)} file(s) to the archive and backed "
                f"them up to permanent storage. Go to **Run Analysis** to "
                f"include them in the KPIs.",
                icon="✅",
            )
            st.balloons()
        except GitSyncError as e:
            st.error(
                f"The file(s) were saved on this server but could NOT be "
                f"backed up to permanent storage: {e}",
                icon="🚫",
            )
