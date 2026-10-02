"""
File-type auto-detection for the Felbry College KPI system.

Every file a staff member uploads gets scored against the column signature of
each known dataset type. This is intentionally conservative: if the top two
candidate types are too close to call, or nothing scores highly enough, we
tell the staff member we're not sure rather than silently guessing wrong --
a wrong guess here would quietly corrupt every KPI downstream.

Known dataset types, as observed in the school's actual exports: enrollment
rosters, withdrawal extracts, the gradebook, attendance logs, LMS (Canvas)
engagement exports (2026-09-26 batch), plus five more added in the
2026-10-01 batch -- absenteeism hotspots, academic performance (course
pass/fail by grade), midterm-withdrawal flags, GPA-by-cohort trend data,
and the PN-AAS-BSN ladder-progression extract. Several of the newer types
have column sets that are a strict subset or near-subset of an older type's
(e.g. "withdrawals before midterm" is a 4-column slice of what's already in
gradebook/academic performance, and the ladder-rate extract is a 6-column
slice of withdrawal). Each such pair gets an explicit `required_any_of` /
`exclude_any_of` gate below so the single column that's actually unique to
one side of the pair decides it, rather than relying on raw overlap counts.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

# Each type's signature: columns that, together, distinguish it from every
# other type. We normalize column names (strip whitespace, lowercase) before
# comparing, so minor header formatting differences between export batches
# don't break detection.
SIGNATURES = {
    "enrollment": {
        # Present in enrollment rosters but NOT in withdrawal extracts --
        # this is what tells the two apart, since withdrawal is otherwise a
        # near-subset of enrollment's columns.
        "required_any_of": [
            {"student first name", "student last name", "campus", "method of delivery", "enroll type"}
        ],
        "core": {"student id number", "enrolled semester", "program", "registration status", "start date"},
    },
    "withdrawal": {
        "core": {"student id number", "enrolled semester", "program", "registration status",
                 "grad./ withdraw date", "withdrawal reason", "start date"},
        # Must NOT look like an enrollment file (see enrollment's required_any_of).
        "exclude_any_of": [
            {"student first name", "campus", "method of delivery", "enroll type"}
        ],
        # Must actually have a reason column -- the ladder-rate extract is a
        # same-shaped subset of this file that's missing exactly this one
        # column, so without this gate the two are nearly indistinguishable.
        "required_any_of": [{"withdrawal reason"}],
    },
    "gradebook": {
        "core": {"student id number", "grade title", "score", "course number", "course title",
                 "class start date"},
        # "Academic Performance Rate" exports carry every gradebook column
        # except the numeric score, so require it explicitly.
        "required_any_of": [{"score"}],
    },
    "attendance": {
        "core": {"student id number", "attendance type", "attendance date"},
        # Must NOT also have a class name -- that's the absenteeism-hotspots
        # export, which is this file plus one extra column.
        "exclude_any_of": [{"class name"}],
    },
    "engagement": {
        "core": {"name", "sis user id", "participations count", "pageviews count",
                 "most recent access date"},
    },
    "absenteeism_hotspots": {
        "core": {"student id number", "attendance type", "attendance date", "class name"},
        "required_any_of": [{"class name"}],
    },
    "academic_performance": {
        "core": {"student id number", "course number", "course title", "class name",
                 "class start date", "class end date", "grade title"},
        # Must NOT have a numeric score -- that's the gradebook file, which
        # is this file plus the score column.
        "exclude_any_of": [{"score"}],
    },
    "midterm_withdrawals": {
        "core": {"grade title", "course number", "class start date", "class end date"},
        # This file's 4 columns are a literal subset of gradebook's and of
        # academic performance's -- but uniquely, it has no student ID at
        # all (it's a course-withdrawal-event list, not a per-student roster).
        "exclude_any_of": [{"student id number"}],
    },
    "gpa_trend": {
        "core": {"student id number", "course number", "course title", "credit", "grade point",
                 "grade title", "class start date", "enrolled semester"},
    },
    "ladder_rate": {
        "core": {"student id number", "program", "registration status", "start date",
                 "grad./ withdraw date", "enrolled semester"},
        # Must NOT have a withdrawal reason -- that's a real withdrawal
        # extract, which is this file plus that one column.
        "exclude_any_of": [{"withdrawal reason"}],
    },
}

TYPE_LABELS = {
    "enrollment": "Enrollment roster",
    "withdrawal": "Withdrawal extract",
    "gradebook": "Gradebook / course grades",
    "attendance": "Attendance log",
    "engagement": "LMS (Canvas) engagement export",
    "absenteeism_hotspots": "Absenteeism hotspots (attendance by class)",
    "academic_performance": "Academic performance rate data",
    "midterm_withdrawals": "Withdrawals before midterm",
    "gpa_trend": "GPA trend by cohort",
    "ladder_rate": "PN-AAS-BSN ladder progression",
}

# What each type feeds, shown to staff after a confident detection so they
# know the upload actually did something besides "save a file".
TYPE_FEEDS = {
    "enrollment": "program enrollment counts, retention rate, and the entry-cohort roster used by several other KPIs.",
    "withdrawal": "withdrawal rate and withdrawal reasons by term.",
    "gradebook": "course pass rate, highest-failure course, and course pass rate ranked (Score-based metrics, kept for future use).",
    "attendance": "attendance rate by term.",
    "engagement": "LMS engagement / at-risk participation indicators.",
    "absenteeism_hotspots": "the Absenteeism Hotspots breakdown by class and program stage.",
    "academic_performance": "course pass rate, highest-failure course, and course pass rate ranked -- this is now the authoritative source for those three KPIs.",
    "midterm_withdrawals": "the Withdrawals Before Midterm KPI (course-withdrawal events split by before/after the midterm point).",
    "gpa_trend": "the GPA Trend by Entry Cohort chart.",
    "ladder_rate": "the PN -> AAS/ASN -> BSN Ladder Progression Rate KPI.",
}

CONFIDENT_THRESHOLD = 0.7   # fraction of "core" columns that must be present
MARGIN_REQUIRED = 0.15      # top score must beat the runner-up by this much


@dataclass
class DetectionResult:
    detected_type: str | None
    confidence: float
    label: str
    all_scores: dict[str, float] = field(default_factory=dict)
    columns_found: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    message: str = ""


def _normalize_columns(columns) -> set[str]:
    return {str(c).strip().lower() for c in columns if not str(c).startswith("Unnamed")}


def _score_type(norm_cols: set[str], sig: dict) -> float:
    core = sig["core"]
    if not core:
        return 0.0
    present = len(core & norm_cols)
    score = present / len(core)

    # A group needs at least 2 matching columns to count UNLESS the group
    # itself has fewer than 2 columns in it (a single-column disambiguator,
    # e.g. {"score"}), in which case just that one column matching is enough.
    required_any_of = sig.get("required_any_of")
    if required_any_of:
        if not any(len(group & norm_cols) >= min(2, len(group)) for group in required_any_of):
            score *= 0.3  # heavily penalize -- looks like the sibling type instead

    exclude_any_of = sig.get("exclude_any_of")
    if exclude_any_of:
        if any(len(group & norm_cols) >= min(2, len(group)) for group in exclude_any_of):
            score *= 0.3

    return round(score, 4)


def detect_columns(columns) -> DetectionResult:
    """Score a set of column names against every known type signature."""
    norm_cols = _normalize_columns(columns)
    scores = {t: _score_type(norm_cols, sig) for t, sig in SIGNATURES.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = ranked[0]
    runner_type, runner_score = ranked[1] if len(ranked) > 1 else (None, 0.0)

    if top_score < CONFIDENT_THRESHOLD:
        return DetectionResult(
            detected_type=None, confidence=top_score, label="Unrecognized",
            all_scores=scores, columns_found=sorted(norm_cols), needs_confirmation=True,
            message=(
                "This file's columns don't clearly match any known data type "
                "(enrollment, withdrawal, gradebook, attendance, LMS engagement, "
                "absenteeism hotspots, academic performance, withdrawals before "
                "midterm, GPA trend by cohort, or ladder progression). Please "
                "double-check the file before uploading, or tell us which type "
                "it is using the dropdown above."
            ),
        )

    if (top_score - runner_score) < MARGIN_REQUIRED:
        return DetectionResult(
            detected_type=top_type, confidence=top_score, label=TYPE_LABELS[top_type],
            all_scores=scores, columns_found=sorted(norm_cols), needs_confirmation=True,
            message=(
                f"This looks like it could be either '{TYPE_LABELS[top_type]}' or "
                f"'{TYPE_LABELS.get(runner_type, runner_type)}' -- please confirm "
                "which one before we add it to the archive."
            ),
        )

    feeds = TYPE_FEEDS.get(top_type, "")
    return DetectionResult(
        detected_type=top_type, confidence=top_score, label=TYPE_LABELS[top_type],
        all_scores=scores, columns_found=sorted(norm_cols), needs_confirmation=False,
        message=(
            f"Detected as: {TYPE_LABELS[top_type]} (confidence {top_score:.0%}). "
            f"This updates: {feeds}" if feeds else
            f"Detected as: {TYPE_LABELS[top_type]} (confidence {top_score:.0%})."
        ),
    )


def read_any(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Read a CSV or Excel upload into a DataFrame of strings, robust to the
    quirks we've already hit in this school's exports: Excel's ="value"
    CSV-quoting artifact, and non-UTF-8 (cp1252) encoded CSVs."""
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
    else:
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, encoding="utf-8")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, encoding="cp1252")
        # Undo Excel's ="12345" quoting artifact wherever it appears.
        for c in df.columns:
            df[c] = df[c].astype(str).str.replace(r'^="(.*)"$', r"\1", regex=True)
            df[c] = df[c].replace("nan", pd.NA)
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    df.columns = [str(c).strip() for c in df.columns]
    return df


def detect_file(file_bytes: bytes, filename: str) -> tuple[DetectionResult, pd.DataFrame | None]:
    """Full detection pass on an uploaded file: read it, then score its columns.
    Returns (result, dataframe) -- dataframe is None if the file couldn't be read."""
    try:
        df = read_any(file_bytes, filename)
    except Exception as e:
        return (
            DetectionResult(
                detected_type=None, confidence=0.0, label="Could not read file",
                needs_confirmation=True,
                message=f"Couldn't read this file as a spreadsheet: {e}",
            ),
            None,
        )
    return detect_columns(df.columns), df
