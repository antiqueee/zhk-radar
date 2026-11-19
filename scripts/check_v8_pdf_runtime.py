"""Free, isolated V8 export smoke. No Store, database, search or model calls.

Run inside the application container, using its installed package and Chromium:
  docker exec -i megapolis-radar-1 python - < scripts/check_v8_pdf_runtime.py

Only a fixed packaged gold fixture is used. The in-memory passed flag exercises
the guarded export branch; no brief or qualification is created or persisted.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

from radar.reports.render import ROOT, page_count
from radar.reports.schema_v8 import ReportV8
from radar.reports.v8 import check
from radar.web.pdf import render_pdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", choices=("nametkin-tower", "stantsiya-l"), default="nametkin-tower"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/megapolis-v8-pdf-smoke"))
    parser.add_argument(
        "--check-only", action="store_true", help="Validate fixture/guards without rendering"
    )
    args = parser.parse_args()
    fixture = ROOT / "gold/v8" / (args.fixture + ".json")
    document = ReportV8.model_validate_json(fixture.read_text()).model_dump()
    issues = check(document)
    if issues:
        raise ValueError(
            "Packaged V8 fixture failed checks: " + json.dumps(issues, ensure_ascii=False)
        )

    # These are ephemeral test inputs, not saved real briefs. Blocked states must
    # fail before the renderer, regardless of the presence of a valid document.
    blocked = []
    for status in ("watch", "hold", "manual"):
        payload = {
            "dataset": "demo",
            "report_v2": document,
            "report_pipeline": {"passed": False, "status": status},
            "report_qualification": {"verdict": status},
        }
        try:
            render_pdf(payload)
        except ValueError:
            blocked.append(status)
        else:
            raise ValueError("Export unexpectedly allowed blocked fixture state: " + status)
    payload = {
        "dataset": "demo",
        "report_v2": document,
        "report_pipeline": {"passed": True, "status": "passed"},
        "report_qualification": {"verdict": "report_full"},
    }
    result = {
        "check": "v8_pdf_runtime",
        "fixture_only": True,
        "fixture": args.fixture,
        "schema_version": document["meta"]["schema_version"],
        "export": "radar.web.pdf.render_pdf",
        "blocked_states": blocked,
        "ai_or_search_calls": 0,
    }
    if args.check_only:
        print(json.dumps({**result, "rendered": False}, ensure_ascii=False), flush=True)
        return

    binary = render_pdf(payload)  # Normal export → Pydantic → pdf_bytes → Chromium.
    # pdf_bytes rejects overflow, and renderer checks the actual HTML page count.
    # The Chromium PDF page dictionaries remain explicit, so no dev-only pypdf
    # dependency is needed in the production image.
    pages = len(re.findall(rb"/Type\s*/Page\b", binary))
    if not binary.startswith(b"%PDF-") or b"%%EOF" not in binary[-100:]:
        raise ValueError("Export did not produce a complete PDF")
    if pages != page_count(document):
        raise ValueError(f"PDF page count mismatch: {pages} != {page_count(document)}")
    if b"/FontFile" not in binary or b"/Subtype /Image" not in binary:
        raise ValueError("PDF is missing embedded font/image resources")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / ("fixture-v8-" + args.fixture + ".pdf")
    target.write_bytes(binary)
    print(
        json.dumps(
            {
                **result,
                "rendered": True,
                "pages": pages,
                "bytes": len(binary),
                "sha256": hashlib.sha256(binary).hexdigest(),
                "overflow": [],
                "output": str(target),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
