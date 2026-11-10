"""Small opt-in integration check of the reader using two supplied public source URLs."""

import json
from pathlib import Path

from radar.web.sources import fetch_page

urls = [
    "https://nsk.rbc.ru/nsk/21/10/2026/6ad831fe9a79476e2871f6f3",
    "https://www.rbc.ru/ekb/freenews/6a61cc9e9a794723b7be2d6c",
]
result = []
for url in urls:
    try:
        page = fetch_page(url)
        result.append(
            dict(
                url=url,
                status=page.status,
                characters=len(page.text),
                published_at=page.published_at,
                content_hash=page.content_hash,
                title=page.title,
                sample=page.text[:160],
            )
        )
    except OSError:
        raise  # Let sandbox/DNS failures surface for the required permission retry.
    except ValueError as error:
        result.append(dict(url=url, status="unavailable", reason=str(error)))
Path("data/qa").mkdir(parents=True, exist_ok=True)
Path("data/qa/public-source-check.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2)
)
print(json.dumps(result, ensure_ascii=False, indent=2))
