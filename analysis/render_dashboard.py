"""
Embeds a freshly computed KPI JSON into the dashboard HTML template for
display inside the Streamlit app.

The dashboard's design (dashboard_template.html) is unchanged from the
standalone prototype delivered earlier -- same charts, same status dots,
same Documentation page. The ONLY thing this module does is swap out the
hardcoded `const REAL_DATA = {...};` block near the top of its <script>
with whatever is currently in data_archive/kpi_output/REAL_DATA.json, so
the embedded dashboard always reflects the latest completed "Run Analysis"
pass -- never a live recomputation, per the system's core design rule that
the dashboard only ever reads pre-computed numbers.
"""
from __future__ import annotations

import json
import os
import re

_REAL_DATA_BLOCK = re.compile(r"const REAL_DATA = \{.*?\n\};", re.DOTALL)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMPLATE = os.path.join(REPO_ROOT, "dashboard_template.html")
DEFAULT_KPI_PATH = os.path.join(REPO_ROOT, "data_archive", "kpi_output", "REAL_DATA.json")


class RenderError(Exception):
    """Plain-language error for the Dashboard page to show directly."""


def render_dashboard_html(
    kpi_path: str = DEFAULT_KPI_PATH,
    template_path: str = DEFAULT_TEMPLATE,
) -> str:
    if not os.path.exists(kpi_path):
        raise RenderError(
            "No KPI data has been computed yet. Go to the 'Run Analysis' page "
            "and click Run Analysis at least once before viewing the dashboard."
        )
    if not os.path.exists(template_path):
        raise RenderError(f"Dashboard template file is missing: {template_path}")

    with open(kpi_path) as f:
        kpi_data = json.load(f)
    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    replacement = "const REAL_DATA = " + json.dumps(kpi_data, indent=2) + ";"
    new_html, n = _REAL_DATA_BLOCK.subn(replacement, html, count=1)
    if n == 0:
        raise RenderError(
            "Could not find the REAL_DATA block in dashboard_template.html to "
            "update -- the template may have been edited in a way this script "
            "doesn't expect. (Looked for a line starting with "
            "'const REAL_DATA = {' followed by a line that is just '};'.)"
        )
    return new_html
