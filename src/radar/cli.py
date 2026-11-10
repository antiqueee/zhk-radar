import argparse
import json
from datetime import date
from pathlib import Path

import yaml

from radar.domain import Evidence, House, Sales, Snapshot
from radar.pipeline.scoring import assess


def main() -> None:
    parser = argparse.ArgumentParser(description="Радар — локальное расчётное ядро")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Синтетический пример, без сети и расходов")
    demo.add_argument("--input", type=Path, default=Path("tests/fixtures/scenario.json"))
    demo.add_argument("--config", type=Path, default=Path("config/scoring.yaml"))
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    result = assess(
        House.model_validate(payload["house"]),
        [Evidence.model_validate(s) for s in payload["signals"]],
        Sales.model_validate(payload["sales"]),
        [Snapshot.model_validate(s) for s in payload.get("history", [])],
        date.fromisoformat(payload["today"]),
        yaml.safe_load(args.config.read_text()),
    )
    print(
        json.dumps(
            {
                "mode": "offline-demo",
                "synthetic": True,
                "object": payload["house"]["label"],
                **result.model_dump(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
