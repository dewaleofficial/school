# -*- coding: utf-8 -*-
"""
Felbry College KPI pipeline -- the generalized, ever-growing-archive version of
the one-off `build_real_data.py` script used for the first (2026-09-26) data
batch.

WHAT CHANGED vs. the one-off script, and why it's safe:

  1. File discovery is now glob-based over `data_archive/<type>/*`, not a fixed
     dict of 6 named files. Any number of files of a type can be present.

  2. TERM_ORDER is now INFERRED from the data itself (distinct `Enrolled
     Semester` values across every enrollment file found, ordered by the mode
     of each term's `Start Date`) instead of a hardcoded 6-term list. This is
     what lets a new term's enrollment file "just work" once uploaded.

  3. Each enrollment row's own `Enrolled Semester` column is read directly as
     that row's entry-cohort term -- confirmed safe by inspecting the first
     batch: every enrollment file's `Enrolled Semester` column is uniform
     within the file and matches the term the file was named for (e.g. every
     row in Enrolled_fall_2024_.csv reads "FALL 2024"). This removes the need
     to associate a whole FILE with a term via its filename, which matters
     once files accumulate in a folder without that naming convention being
     enforced.

  4. Each withdrawal row is bucketed into a term via ITS OWN `Grad./ Withdraw
     Date` against the inferred term calendar, instead of assuming one
     withdrawal FILE = one term. Confirmed safe on the first batch: 19/19 rows
     in Withdrawn_Rate_-_FALL_2024.csv fall inside the inferred Fall 2024
     window. This is required because new withdrawal extracts may not arrive
     one-term-per-file going forward.

  5. Every dataset is DEDUPED before use, keeping the most-recently-uploaded
     copy of any row that appears more than once (a staff member re-uploading
     the same export, or an export that overlaps a previous one, must not
     double-count students). "Most recent" is by file modification time on
     disk, which is set to the upload time when a file is saved into the
     archive.

Every other computation, threshold, and the output JSON schema are carried
over UNCHANGED from the validated first-batch script so the dashboard's
existing chart code keeps working without modification.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import timedelta

import numpy as np
import pandas as pd

from detectors import read_any

pd.set_option('future.no_silent_downcasting', True)

# ---------------------------------------------------------------------------
# Thresholds (unchanged from build_real_data.py -- see that file's comments
# and the dashboard's Documentation page for the rationale behind each one).
# ---------------------------------------------------------------------------
MIN_N_FOR_PROGRAM_RETENTION = 15
MIN_N_FOR_HIGHEST_FAILURE = 15
MIN_N_FOR_GPA_CELL = 3

NEVER_STARTED = {'No Start', 'New Applicant', 'Accepted'}
PROGRAM_SHORT = {
    'Practical Nursing': 'PN',
    'Associate of Applied Science in Nursing One + One': 'ADN (1+1)',
    'Associate of Applied Science in Nursing': 'ADN',
    'Bachelor of Science in Nursing': 'BSN',
    'RN to BSN Completion Program': 'RN-to-BSN',
}
PASS_GRADES = {'A', 'B', 'C', 'S'}
FAIL_GRADES = {'F', 'WF', 'U'}
GPA_MAP = {'A': 4.0, 'B': 3.0, 'C': 2.0, 'F': 0.0}
PRESENT_TYPES = {'Present', 'Tardy/Late', 'Tardy/Late excused'}
EXCLUDE_ATTENDANCE_TYPES = {'No class'}


class PipelineError(Exception):
    """Raised for a data problem a non-technical staff member needs to see
    explained in plain language (missing files, unreadable dates, etc.),
    as opposed to a bug. The Streamlit app should catch this and show
    `str(e)` directly rather than a traceback."""


def to_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors='coerce')


def _normalize_term_label(raw: str) -> str:
    """'FALL 2024' / 'fall 2024' / 'Fall 2024' -> 'Fall 2024'."""
    parts = str(raw).strip().split()
    if len(parts) != 2:
        return str(raw).strip().title()
    season, year = parts
    return f"{season.title()} {year}"


def _archive_files(archive_root: str, type_name: str) -> list[str]:
    folder = os.path.join(archive_root, type_name)
    files = sorted(
        glob.glob(os.path.join(folder, "*")),
        key=lambda p: os.path.getmtime(p),
    )
    # Ignore dotfiles / non-data artifacts that might land in the folder.
    return [f for f in files if not os.path.basename(f).startswith('.')]


def _load_and_concat(archive_root: str, type_name: str) -> pd.DataFrame:
    """Read every file under data_archive/<type_name>/ and concatenate them,
    tagging each row with its source file's mtime so later dedup steps can
    keep the most-recently-uploaded copy of a duplicated row."""
    files = _archive_files(archive_root, type_name)
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        df = read_any_path(f)
        df['_source_file'] = os.path.basename(f)
        df['_uploaded_at'] = os.path.getmtime(f)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def read_any_path(path: str) -> pd.DataFrame:
    with open(path, 'rb') as fh:
        return read_any(fh.read(), os.path.basename(path))


def _dedupe_keep_latest(df: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """Drop duplicate rows (by key_cols), keeping the copy from the
    most-recently-uploaded file. A staff member re-uploading an export they
    already archived, or uploading a file that overlaps a previous one,
    should not double-count students."""
    if df.empty:
        return df
    df = df.sort_values('_uploaded_at')
    return df.drop_duplicates(subset=key_cols, keep='last').reset_index(drop=True)


def dround_or_none(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_pipeline(archive_root: str = "data_archive") -> dict:
    """Runs the full KPI computation over everything currently in the
    archive and returns the output dict (the same schema the dashboard's
    REAL_DATA.json already expects). Raises PipelineError with a plain-
    language message if the archive doesn't have enough to work with yet."""

    # -----------------------------------------------------------------
    # 1. Enrollment -> master roster
    # -----------------------------------------------------------------
    roster = _load_and_concat(archive_root, 'enrollment')
    if roster.empty:
        raise PipelineError(
            "No enrollment files found in the archive yet. Upload at least "
            "one enrollment roster before running analysis."
        )

    roster = roster.rename(columns={
        'Student Id Number': 'student_id',
        'Enrolled Semester': 'entry_term_raw',
        'Program': 'program',
        'Registration Status': 'status',
        'Campus': 'campus',
        'Method of Delivery': 'delivery_mode',
        'Start Date': 'start_date',
        'Grad./ Withdraw Date': 'end_date',
        'Withdrawal Reason': 'withdrawal_reason',
        'Enroll Type': 'enroll_type',
    })
    missing_cols = {'student_id', 'entry_term_raw', 'start_date', 'status'} - set(roster.columns)
    if missing_cols:
        raise PipelineError(
            f"Enrollment data is missing expected column(s): {', '.join(sorted(missing_cols))}. "
            "Please check the file(s) in the enrollment archive."
        )

    roster['entry_term'] = roster['entry_term_raw'].map(_normalize_term_label)
    roster['start_date'] = to_date(roster['start_date'])
    roster['end_date'] = to_date(roster['end_date'])
    roster = _dedupe_keep_latest(roster, ['student_id', 'entry_term', 'start_date'])
    roster['actually_started'] = ~roster['status'].isin(NEVER_STARTED)
    roster['program_short'] = roster['program'].map(PROGRAM_SHORT).fillna(roster['program'])

    # -----------------------------------------------------------------
    # 2. Term order + calendar, inferred from the enrollment data itself.
    # -----------------------------------------------------------------
    term_start = (
        roster.dropna(subset=['start_date'])
        .groupby('entry_term')['start_date']
        .agg(lambda s: s.mode().iloc[0])
        .to_dict()
    )
    if not term_start:
        raise PipelineError("Could not determine term start dates from the enrollment data's Start Date column.")

    TERM_ORDER = sorted(term_start.keys(), key=lambda t: term_start[t])

    # Gradebook is loaded now (rather than in step 7) because it's needed to
    # infer the end date of the most recent term.
    gradebook = _load_and_concat(archive_root, 'gradebook')
    next_term_start = None
    if not gradebook.empty and 'Class Start Date' in gradebook.columns:
        gb_dates = to_date(gradebook['Class Start Date'])
        last_term_start = term_start[TERM_ORDER[-1]]
        candidates = gb_dates[gb_dates > last_term_start + timedelta(days=90)]
        if len(candidates):
            next_term_start = candidates.min()

    term_calendar = {}
    for i, term in enumerate(TERM_ORDER):
        start = term_start[term]
        if i + 1 < len(TERM_ORDER):
            end = term_start[TERM_ORDER[i + 1]] - timedelta(days=1)
        elif next_term_start is not None:
            end = next_term_start - timedelta(days=1)
        else:
            end = start + timedelta(days=119)  # fallback: assume a ~17-week term
        term_calendar[term] = {'start': start, 'end': end}

    term_idx_map = {t: i for i, t in enumerate(TERM_ORDER)}

    # -----------------------------------------------------------------
    # 3. Active-headcount reconstruction (unchanged logic).
    # -----------------------------------------------------------------
    def active_in_term(row, term):
        b = term_calendar[term]
        if not row['actually_started']:
            return False
        if pd.isna(row['start_date']) or row['start_date'] > b['end']:
            return False
        if pd.notna(row['end_date']) and row['end_date'] < b['start']:
            return False
        return True

    for term in TERM_ORDER:
        roster[f'active_{term}'] = roster.apply(lambda r: active_in_term(r, term), axis=1)

    active_counts = {term: int(roster.loc[roster[f'active_{term}'], 'student_id'].nunique()) for term in TERM_ORDER}

    # -----------------------------------------------------------------
    # 4. Enrollment by program per term.
    # -----------------------------------------------------------------
    enrollment_by_program_term = {}
    for term in TERM_ORDER:
        sub = roster[roster[f'active_{term}']].drop_duplicates('student_id')
        enrollment_by_program_term[term] = sub['program_short'].value_counts().to_dict()

    # -----------------------------------------------------------------
    # 5. Term-to-term retention (overall, by enroll type, by program).
    # -----------------------------------------------------------------
    retention = []
    for i in range(len(TERM_ORDER) - 1):
        n, n1 = TERM_ORDER[i], TERM_ORDER[i + 1]
        ids_n = set(roster.loc[roster[f'active_{n}'], 'student_id'])
        ids_n1 = set(roster.loc[roster[f'active_{n1}'], 'student_id'])
        rate = (len(ids_n & ids_n1) / len(ids_n)) if ids_n else None
        retention.append({'from': n, 'to': n1, 'denominator': len(ids_n), 'retained': len(ids_n & ids_n1), 'rate': rate})

    def pooled_retention_by(group_col):
        out = {}
        for i in range(len(TERM_ORDER) - 1):
            n, n1 = TERM_ORDER[i], TERM_ORDER[i + 1]
            sub_n = roster[roster[f'active_{n}']].drop_duplicates('student_id')
            ids_n1 = set(roster.loc[roster[f'active_{n1}'], 'student_id'])
            for val, grp in sub_n.groupby(group_col):
                ids_n = set(grp['student_id'])
                out.setdefault(val, {'denom': 0, 'num': 0})
                out[val]['denom'] += len(ids_n)
                out[val]['num'] += len(ids_n & ids_n1)
        for val in out:
            d = out[val]
            d['rate'] = d['num'] / d['denom'] if d['denom'] else None
        return out

    retention_by_enroll_type = pooled_retention_by('enroll_type') if 'enroll_type' in roster.columns else {}

    retention_by_program = []
    for i in range(len(TERM_ORDER) - 1):
        n, n1 = TERM_ORDER[i], TERM_ORDER[i + 1]
        sub_n = roster[roster[f'active_{n}']].drop_duplicates('student_id')
        ids_n1 = set(roster.loc[roster[f'active_{n1}'], 'student_id'])
        for prog, grp in sub_n.groupby('program_short'):
            ids_n = set(grp['student_id'])
            if len(ids_n) < MIN_N_FOR_PROGRAM_RETENTION:
                continue
            retention_by_program.append({
                'from': n, 'to': n1, 'program': prog,
                'denominator': len(ids_n), 'retained': len(ids_n & ids_n1),
                'rate': len(ids_n & ids_n1) / len(ids_n)
            })

    # -----------------------------------------------------------------
    # 6. Withdrawal rate & reasons -- bucketed by each ROW's own
    #    Grad./Withdraw Date against the inferred term calendar (not by
    #    which file it came from).
    # -----------------------------------------------------------------
    withdrawal_by_term = {t: {'count': 0, 'denominator': active_counts[t], 'rate': None} for t in TERM_ORDER}
    reason_counter: dict[str, int] = {}
    reasons_by_term = {t: {} for t in TERM_ORDER}

    withdrawals = _load_and_concat(archive_root, 'withdrawal')
    if not withdrawals.empty:
        withdrawals = withdrawals.rename(columns={
            'Student Id Number': 'student_id',
            'Grad./ Withdraw Date': 'end_date',
            'Withdrawal Reason': 'withdrawal_reason',
        })
        withdrawals['end_date'] = to_date(withdrawals['end_date'])
        withdrawals = _dedupe_keep_latest(withdrawals, ['student_id', 'end_date'])

        def _term_for_withdrawal(d):
            if pd.isna(d):
                return None
            for t in TERM_ORDER:
                b = term_calendar[t]
                if b['start'] <= d <= b['end']:
                    return t
            return None

        withdrawals['term'] = withdrawals['end_date'].map(_term_for_withdrawal)
        withdrawals['withdrawal_reason'] = (
            withdrawals.get('withdrawal_reason', pd.Series(dtype=str))
            .fillna('Not stated').astype(str).str.strip().replace('', 'Not stated')
        )
        for term, grp in withdrawals.dropna(subset=['term']).groupby('term'):
            n = len(grp)
            denom = active_counts.get(term, 0)
            rate = n / denom if denom else None
            withdrawal_by_term[term] = {'count': n, 'denominator': denom, 'rate': rate}
            term_counts = {}
            for reason, cnt in grp['withdrawal_reason'].value_counts().items():
                reason_counter[reason] = reason_counter.get(reason, 0) + int(cnt)
                term_counts[reason] = int(cnt)
            reasons_by_term[term] = term_counts

    # -----------------------------------------------------------------
    # 7. Course pass/fail (gradebook).
    # -----------------------------------------------------------------
    overall_pass_rate = None
    graded_n = 0
    by_course_records = []
    highest_failure = None
    gpa_trend_records = []

    if not gradebook.empty:
        gb = gradebook.rename(columns={
            'Student Id Number': 'student_id', 'Grade Title': 'grade', 'Score': 'score',
            'Course Number': 'course_no', 'Course Title': 'course_title',
            'Class Name': 'class_name', 'Class Start Date': 'class_start', 'Class End Date': 'class_end',
        })
        key_cols = [c for c in ['student_id', 'course_no', 'class_start'] if c in gb.columns]
        if key_cols:
            gb = _dedupe_keep_latest(gb, key_cols)
        gb['class_start'] = to_date(gb['class_start']) if 'class_start' in gb.columns else pd.NaT

        gb['outcome'] = np.where(gb['grade'].isin(PASS_GRADES), 'Pass',
                          np.where(gb['grade'].isin(FAIL_GRADES), 'Fail', 'Excluded'))
        graded = gb[gb['outcome'] != 'Excluded']
        graded_n = int(len(graded))
        if graded_n:
            overall_pass_rate = (graded['outcome'] == 'Pass').mean()

            by_course = graded.groupby(['course_no', 'course_title']).agg(
                n=('outcome', 'size'),
                passes=('outcome', lambda s: (s == 'Pass').sum())
            ).reset_index()
            by_course['pass_rate'] = by_course['passes'] / by_course['n']
            by_course['fail_rate'] = 1 - by_course['pass_rate']
            by_course = by_course.sort_values('fail_rate', ascending=False)
            by_course_records = by_course.to_dict('records')

            eligible = by_course[by_course['n'] >= MIN_N_FOR_HIGHEST_FAILURE]
            highest_failure = (eligible if len(eligible) else by_course).iloc[0].to_dict()

        # GPA trend by entry cohort & term.
        gb['gpa_points'] = gb['grade'].map(GPA_MAP)

        def _term_for_class(d):
            if pd.isna(d):
                return None
            for t in TERM_ORDER:
                b = term_calendar[t]
                if b['start'] <= d <= b['end']:
                    return t
            return None

        gb['term'] = gb['class_start'].map(_term_for_class)
        entry_cohort = (
            roster.sort_values('start_date').drop_duplicates('student_id', keep='first')
            .set_index('student_id')['entry_term']
        )
        gb['entry_cohort'] = gb['student_id'].map(entry_cohort)

        gpa_gradeable = gb.dropna(subset=['gpa_points', 'term', 'entry_cohort'])
        gpa_gradeable = gpa_gradeable[
            gpa_gradeable['term'].map(term_idx_map) >= gpa_gradeable['entry_cohort'].map(term_idx_map)
        ]
        if len(gpa_gradeable):
            gpa_trend = gpa_gradeable.groupby(['entry_cohort', 'term'])['gpa_points'].agg(['mean', 'count']).reset_index()
            gpa_trend = gpa_trend.rename(columns={'mean': 'gpa_points'})
            gpa_trend = gpa_trend[gpa_trend['count'] >= MIN_N_FOR_GPA_CELL]
            gpa_trend_records = gpa_trend.to_dict('records')

    # -----------------------------------------------------------------
    # 8. Attendance.
    # -----------------------------------------------------------------
    attendance_by_term = {}
    attendance_by_stage = {}
    attendance = _load_and_concat(archive_root, 'attendance')
    if not attendance.empty:
        attendance = attendance.rename(columns={'Student Id Number': 'student_id'})
        key_cols = [c for c in ['student_id', 'Attendance Date', 'Attendance Type'] if c in attendance.columns]
        if key_cols:
            attendance = _dedupe_keep_latest(attendance, key_cols)
        attendance['att_date'] = to_date(attendance.get('Attendance Date', pd.Series(dtype=str)))

        def _term_for_attendance(d):
            if pd.isna(d):
                return None
            for t in TERM_ORDER:
                b = term_calendar[t]
                if b['start'] <= d <= b['end']:
                    return t
            return None

        attendance['term'] = attendance['att_date'].map(_term_for_attendance)
        for term, grp in attendance.dropna(subset=['term']).groupby('term'):
            valid = grp[~grp['Attendance Type'].isin(EXCLUDE_ATTENDANCE_TYPES)]
            present = int(valid['Attendance Type'].isin(PRESENT_TYPES).sum())
            rate = present / len(valid) if len(valid) else None
            attendance_by_term[term] = {'present': present, 'total_valid': int(len(valid)), 'rate': rate}

        entry_idx = (
            roster.sort_values('start_date').drop_duplicates('student_id', keep='first')
            .set_index('student_id')['entry_term'].map(term_idx_map)
        )
        attendance['term_idx'] = attendance['term'].map(term_idx_map)
        attendance['entry_idx'] = attendance['student_id'].map(entry_idx)
        attendance['stage'] = attendance['term_idx'] - attendance['entry_idx'] + 1
        att_valid = attendance[~attendance['Attendance Type'].isin(EXCLUDE_ATTENDANCE_TYPES)]
        att_valid = att_valid.dropna(subset=['stage'])
        att_valid = att_valid[att_valid['stage'].between(1, 4)]
        if len(att_valid):
            stage_group = att_valid.groupby('stage')['Attendance Type'].apply(lambda s: s.isin(PRESENT_TYPES).mean())
            attendance_by_stage = {int(k): float(v) for k, v in stage_group.items()}

    # -----------------------------------------------------------------
    # 9. Completion proxy by enroll type.
    # -----------------------------------------------------------------
    completion_proxy = {}
    if 'enroll_type' in roster.columns:
        resolved = roster[roster['actually_started'] & roster['status'].isin(['Graduate', 'Withdrawn', 'Transfer', 'Inactive'])]
        if len(resolved):
            completion_proxy = resolved.groupby('enroll_type')['status'].apply(lambda s: (s == 'Graduate').mean()).to_dict()

    # -----------------------------------------------------------------
    # 10. Engagement (LMS) -- archived and deduped for future use, not yet
    #     scored into a KPI (mirrors the first-batch script, which also
    #     collected but did not score this data).
    # -----------------------------------------------------------------
    engagement = _load_and_concat(archive_root, 'engagement')
    engagement_rows_archived = 0
    if not engagement.empty:
        key_cols = [c for c in ['SIS User ID', 'Name'] if c in engagement.columns]
        if key_cols:
            engagement = _dedupe_keep_latest(engagement, key_cols)
        engagement_rows_archived = int(len(engagement))

    # -----------------------------------------------------------------
    # Assemble output (schema unchanged from build_real_data.py).
    # -----------------------------------------------------------------
    output = {
        "generated_at": pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "term_order": TERM_ORDER,
        "term_calendar": {t: {"start": b["start"].strftime("%Y-%m-%d"), "end": b["end"].strftime("%Y-%m-%d")} for t, b in term_calendar.items()},
        "active_headcount_by_term": active_counts,
        "enrollment_by_program_term": {t: {k: int(v) for k, v in c.items()} for t, c in enrollment_by_program_term.items()},
        "retention": [
            {"from": r["from"], "to": r["to"], "denominator": r["denominator"], "retained": r["retained"], "rate": dround_or_none(r["rate"])}
            for r in retention
        ],
        "retention_by_enroll_type": {
            k: {"denominator": v["denom"], "retained": v["num"], "rate": dround_or_none(v["rate"])}
            for k, v in retention_by_enroll_type.items()
        },
        "retention_by_program": [
            {"from": r["from"], "to": r["to"], "program": r["program"], "denominator": r["denominator"], "retained": r["retained"], "rate": dround_or_none(r["rate"])}
            for r in retention_by_program
        ],
        "withdrawal_by_term": {
            t: {"count": v["count"], "denominator": v["denominator"], "rate": dround_or_none(v["rate"])}
            for t, v in withdrawal_by_term.items()
        },
        "withdrawal_reasons": reason_counter,
        "withdrawal_reasons_by_term": reasons_by_term,
        "course_pass_fail": {
            "overall_pass_rate": dround_or_none(overall_pass_rate),
            "graded_n": graded_n,
            "by_course": [
                {
                    "course_no": row["course_no"], "course_title": row["course_title"],
                    "n": int(row["n"]), "passes": int(row["passes"]),
                    "pass_rate": dround_or_none(row["pass_rate"]), "fail_rate": dround_or_none(row["fail_rate"])
                }
                for row in by_course_records
            ],
            "highest_failure_course": (
                {
                    "course_no": highest_failure["course_no"], "course_title": highest_failure["course_title"],
                    "fail_rate": dround_or_none(highest_failure["fail_rate"]), "n": int(highest_failure["n"])
                }
                if highest_failure is not None else None
            ),
        },
        "gpa_trend_by_cohort": [
            {"entry_cohort": row["entry_cohort"], "term": row["term"], "avg_gpa_points": dround_or_none(row["gpa_points"]), "n": int(row["count"])}
            for row in gpa_trend_records
        ],
        "attendance_by_term": {
            t: {"present": v["present"], "total_valid": v["total_valid"], "rate": dround_or_none(v["rate"])}
            for t, v in attendance_by_term.items()
        },
        "attendance_by_program_stage": {str(k): dround_or_none(v) for k, v in attendance_by_stage.items()},
        "completion_proxy_by_enroll_type": {k: dround_or_none(v) for k, v in completion_proxy.items()},
        "_archive_summary": {
            "enrollment_files": len(_archive_files(archive_root, 'enrollment')),
            "withdrawal_files": len(_archive_files(archive_root, 'withdrawal')),
            "gradebook_files": len(_archive_files(archive_root, 'gradebook')),
            "attendance_files": len(_archive_files(archive_root, 'attendance')),
            "engagement_files": len(_archive_files(archive_root, 'engagement')),
            "engagement_rows_archived": engagement_rows_archived,
            "roster_rows": int(len(roster)),
            "roster_unique_students": int(roster['student_id'].nunique()),
        },
    }
    return output


def write_kpi_output(archive_root: str = "data_archive", out_path: str | None = None) -> dict:
    """Runs the pipeline and writes the result to
    data_archive/kpi_output/REAL_DATA.json (the exact path/filename the
    dashboard already expects)."""
    output = run_pipeline(archive_root)
    out_path = out_path or os.path.join(archive_root, "kpi_output", "REAL_DATA.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    return output


if __name__ == "__main__":
    result = write_kpi_output()
    print(json.dumps(result["_archive_summary"], indent=2))
