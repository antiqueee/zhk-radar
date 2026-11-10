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
    intake = sub.add_parser("import-workbooks", help="Импорт в непроверенные кандидаты без сети")
    intake.add_argument("files", type=Path, nargs="+")
    intake.add_argument("--output", type=Path, default=Path("data/intake/staging.json"))
    serve = sub.add_parser("serve", help="Открыть локальное приложение")
    serve.add_argument("--port", type=int, default=8765)
    sub.add_parser("backup", help="Создать согласованную локальную копию SQLite")
    args = parser.parse_args()
    if args.command == "backup":
        import sqlite3
        from datetime import UTC, datetime

        source = Path("data/local/workspace.sqlite3")
        if not source.exists():
            parser.error("Рабочая база ещё не создана. Сначала запустите приложение.")
        target = Path("data/backups") / (
            datetime.now(UTC).strftime("radar-%Y%m%dT%H%M%S%fZ") + ".sqlite3"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as original, sqlite3.connect(target) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Проверка резервной копии не прошла")
        target.chmod(0o600)
        print(target.resolve())
        return
    if args.command == "serve":
        import uvicorn

        from radar.web.app import create_app

        uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
        return
    if args.command == "import-workbooks":
        from radar.intake import import_workbooks

        print(json.dumps(import_workbooks(args.files, args.output), ensure_ascii=False, indent=2))
        return
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
