"""
File-type auto-detection for the Felbry College KPI system.

Every file a staff member uploads gets scored against the column signature of
each known dataset type. This is intentionally conservative: if the top two
candidate types are too close to call, or nothing scores highly enough, we
tell the staff member we're not sure rather than silently guessing wrong --
a wrong guess here would quietly corrupt every KPI downstream.

Known dataset types, as observed in the school's actual exports (2026-09-26
batch): enrollment rosters, withdrawal extracts, the gradebook, attendance
logs, and LMS (Canvas) engagement exports.
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
    },
    "gradebook": {
        "core": {"student id number", "grade title", "score", "course number", "course title",
                 "class start date"},
    },
    "attendance": {
        "core": {"student id number", "attendance type", "attendance date"},
    },
    "engagement": {
        "core": {"name", "sis user id", "participations count", "pageviews count",
                 "most recent access date"},
    },
}

TYPE_LABELS = {
    "enrollment": "Enrollment roster",
    "withdrawal": "Withdrawal extract",
    "gradebook": "Gradebook / course grades",
    "attendance": "Attendance log",
    "engagement": "LMS (Canvas) engagement export",
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

    required_any_of = sig.get("required_any_of")
    if required_any_of:
        if not any(len(group & norm_cols) >= 2 for group in required_any_of):
            score *= 0.3  # heavily penalize -- looks like the sibling type instead

    exclude_any_of = sig.get("exclude_any_of")
    if exclude_any_of:
        if any(len(group & norm_cols) >= 2 for group in exclude_any_of):
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
                "(enrollment, withdrawal, gradebook, attendance, or LMS engagement). "
                "Please double-check the file before uploading, or tell us which "
                "type it is."
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

    return DetectionResult(
        detected_type=top_type, confidence=top_score, label=TYPE_LABELS[top_type],
        all_scores=scores, columns_found=sorted(norm_cols), needs_confirmation=False,
        message=f"Detected as: {TYPE_LABELS[top_type]} (confidence {top_score:.0%}).",
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
