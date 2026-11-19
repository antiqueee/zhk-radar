"""Render supplied fixture, or bounded paid acceptance on explicitly named local objects."""

import argparse
import json
from pathlib import Path

from radar.reports.pipeline_v5 import generate
from radar.reports.render import render
from radar.web.discovery import call_json
from radar.web.store import Store


def logged_call(client, url, headers, body, service):
    print(service, body["model"], flush=True)

    def inspect(response):
        if response.status_code >= 400:
            response.read()
            error = response.json().get("error", {})
            message = (
                str(error.get("message", "")) + " " + str(error.get("metadata", {}).get("raw", ""))
            )
            for secret in headers.values():
                message = message.replace(secret, "[hidden]").replace(
                    secret.removeprefix("Bearer "), "[hidden]"
                )
            print("Provider error:", message[:2000], flush=True)

    client.event_hooks["response"] = [inspect]
    return call_json(client, url, headers, body, service)


parser = argparse.ArgumentParser()
parser.add_argument("--record", action="append", default=[])
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = root / "data/qa/report-kit"
output.mkdir(parents=True, exist_ok=True)
if not args.record:
    for name in ("nametkin-tower", "stantsiya-l"):
        value = json.loads((root / f"src/radar/reports/gold/{name}.json").read_text())
        binary, overflow = render(value, output / f"{name}.pdf", png=True)
        print(json.dumps({"object": name, "bytes": len(binary), "overflow": overflow}), flush=True)
else:
    store = Store(root)
    for ident in args.record:
        print("Starting", ident, store.get(ident)["name"], flush=True)
        result = generate(store, ident, "report-kit-acceptance:" + ident, invoke=logged_call)
        payload = result["payload"]
        if payload.get("report_pipeline", {}).get("passed") is not True:
            print(
                json.dumps(
                    {
                        "id": result["id"],
                        "record_id": ident,
                        "qualification": payload.get("report_qualification"),
                        "issues": payload.get("report_pipeline", {}).get("issues", []),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue
        document = payload["report_v2"]
        target = output / (ident + ".pdf")
        binary, overflow = render(document, target, png=True)
        target.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "id": result["id"],
                    "record_id": ident,
                    "layout": document["meta"]["layout"],
                    "passed": payload["report_pipeline"]["passed"],
                    "overflow": overflow,
                    "usage": [
                        {
                            "stage": u["stage"],
                            "model": u["model"],
                            "cached": u["cached"],
                            "input": u.get("prompt_tokens"),
                            "output": u.get("completion_tokens"),
                            "cost": u.get("cost"),
                        }
                        for u in payload["report_pipeline"]["usage"]
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
