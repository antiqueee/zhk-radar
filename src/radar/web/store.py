"""Durable local workspace. Independent from the production PostgreSQL registry."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from radar.domain import Evidence, House, Sales
from radar.pipeline.scoring import assess

STATUSES = {
    "new": "Новый",
    "review": "На проверке",
    "inwork": "В работе",
    "proposal": "КП отправлено",
    "meeting": "Встреча",
    "closed": "Закрыт",
    "later": "Отложен",
    "rejected": "Не интересно",
}


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "data/local/workspace.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS records (
              id TEXT PRIMARY KEY, dataset TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '',
              revision INTEGER NOT NULL DEFAULT 0, stage INTEGER NOT NULL DEFAULT 0,
              situation TEXT NOT NULL DEFAULT 'сроки', topics TEXT NOT NULL DEFAULT '[]',
              updated_at TEXT NOT NULL, snooze_until TEXT);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, action TEXT NOT NULL,
              detail TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (
              id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, title TEXT NOT NULL,
              url TEXT NOT NULL, quote TEXT NOT NULL, source_text TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_identity
              ON evidence(record_id,url,quote);
            CREATE TABLE IF NOT EXISTS briefs (
              id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, version INTEGER NOT NULL,
              fingerprint TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(record_id, fingerprint));
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, payload TEXT NOT NULL,
              created_at TEXT NOT NULL);
            """)
        self.seed()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, book: dict) -> int:
        count = 0
        with self.connect() as db:
            for candidate in book["candidates"]:
                claims = candidate["claims"]
                payload = {
                    **candidate,
                    "name": candidate["subject"],
                    "region": claims.get("Регион", "Москва и область"),
                    "summary": next(
                        (
                            claims[k]
                            for k in [
                                "Подтвержденные сигналы / ограничения",
                                "Подтверждённый сигнал",
                                "Суть ситуации",
                                "Сигнал для мониторинга",
                                "Почему в резерве",
                                "Тип учёта",
                            ]
                            if claims.get(k)
                        ),
                        "Нужна проверка источников",
                    ),
                    "check": next(
                        (
                            claims[k]
                            for k in [
                                "Что проверить перед КП",
                                "Что перепроверить",
                                "Следующая проверка",
                                "Что проверить далее",
                                "Что проверить",
                            ]
                            if claims.get(k)
                        ),
                        "Установить корпус, актуальные сроки и заказчика",
                    ),
                    "assessment": None,
                }
                count += db.execute(
                    "INSERT OR IGNORE INTO records(id,dataset,payload,updated_at) VALUES(?,?,?,?)",
                    (candidate["id"], "imported", json.dumps(payload, ensure_ascii=False), now()),
                ).rowcount
        return count

    def seed(self):
        staging = self.root / "data/intake/staging.json"
        if staging.exists():
            for book in json.loads(staging.read_text())["workbooks"]:
                self.ingest(book)
        demo = [
            (
                "demo-1",
                "Северный берег · корпус 3",
                "Демо Девелопмент",
                "Московская область",
                "Повторный перенос передачи. В тестовом сценарии жители получают "
                "разные ответы о сроках.",
                "Уточнить согласованный график передачи и ответственного за коммуникации.",
                2,
                ["разные_ответы", "выдача_ключей"],
                780,
                2,
                ["supervision", "regional_media"],
            ),
            (
                "demo-2",
                "Новые сады · корпус 1",
                "Демо Строй",
                "Санкт-Петербург",
                "Вымышленный пример раннего риска: до ввода два месяца, готовность 68%.",
                "Проверить готовность инженерии и актуальность планового ввода.",
                0,
                ["риск_повторного_переноса"],
                320,
                0,
                [],
            ),
            (
                "demo-3",
                "Речной квартал · дом 2",
                "Демо Девелопмент",
                "Москва",
                "Вымышленный пример после заселения: повторные сообщения о протечках и работе УК.",
                "Установить ответственного за устранение дефектов и сроки ответа жителям.",
                2,
                ["протечки", "качество_ук"],
                540,
                0,
                ["chat_conflict_25pct", "collective_complaint", "regional_media"],
            ),
            (
                "demo-4",
                "Лесной парк · корпус 4",
                "Демо Регион",
                "Татарстан",
                "Вымышленный пример: судебные претензии и длительный перенос, "
                "заказчик не установлен.",
                "Подтвердить полномочия заказчика и возможность оказать услугу.",
                3,
                ["массовые_претензии", "суд"],
                700,
                2,
                ["prosecutor", "federal_media"],
            ),
        ]
        cfg = yaml.safe_load((self.root / "config/scoring.yaml").read_text())
        today = date.today()
        with self.connect() as db:
            for (
                ident,
                name,
                developer,
                region,
                summary,
                check,
                stage,
                topics,
                flats,
                shifts,
                kinds,
            ) in demo:
                house = House(
                    eisgs_id=ident,
                    label=name,
                    flats=flats,
                    commissioning=today + timedelta(days=60)
                    if shifts == 0
                    else today - timedelta(days=40),
                    transfer_initial=today - timedelta(days=180) if shifts else None,
                    transfer=today + timedelta(days=90),
                    transfer_shifts=shifts,
                    readiness=68 if ident == "demo-2" else 92,
                    rve=today - timedelta(days=60) if ident == "demo-3" else None,
                )
                sales = Sales(
                    state="active" if ident != "demo-4" else "disputed",
                    solvent=ident != "demo-4",
                    buyer_known=ident != "demo-4",
                )
                signals = [
                    Evidence(
                        kind=k,
                        occurred_at=today - timedelta(days=3),
                        trust="B" if "media" in k else "A",
                        origin=k,
                        quote="Синтетическое доказательство",
                        document_id="demo",
                    )
                    for k in kinds
                ]
                score = assess(house, signals, sales, [], today, cfg).model_dump()
                payload = dict(
                    id=ident,
                    name=name,
                    developer=developer,
                    region=region,
                    summary=summary,
                    check=check,
                    assessment=score,
                    urls=[],
                    claims={},
                    file="Демонстрационный сценарий",
                    sheet="Демо",
                    row=1,
                    verification="synthetic",
                    eisgs_id_hints=[],
                    granularity="object",
                )
                db.execute(
                    "INSERT OR IGNORE INTO records"
                    "(id,dataset,payload,updated_at,stage,topics,situation) VALUES(?,?,?,?,?,?,?)",
                    (
                        ident,
                        "demo",
                        json.dumps(payload, ensure_ascii=False),
                        now(),
                        stage,
                        json.dumps(topics),
                        "после_заселения" if ident == "demo-3" else "сроки",
                    ),
                )

    def decode(self, row):
        d = dict(row)
        payload = json.loads(d.pop("payload"))
        return {**payload, **d, "topics": json.loads(d["topics"])}

    def records(self, dataset):
        with self.connect() as db:
            return [
                self.decode(r)
                for r in db.execute(
                    "SELECT * FROM records WHERE dataset=? ORDER BY rowid", (dataset,)
                )
            ]

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            return {
                **self.decode(row),
                "events": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM events WHERE record_id=? ORDER BY id DESC", (ident,)
                    )
                ],
                "evidence": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,title,url,quote,created_at FROM evidence WHERE record_id=?",
                        (ident,),
                    )
                ],
            }

    def update(self, ident, data):
        with self.connect() as db:
            until = (
                (date.today() + timedelta(days=28)).isoformat() if data.status == "later" else None
            )
            cursor = db.execute(
                """UPDATE records SET status=?,note=?,stage=?,situation=?,topics=?,
              revision=revision+1,updated_at=?,snooze_until=? WHERE id=? AND revision=?""",
                (
                    data.status,
                    data.note,
                    data.stage,
                    data.situation,
                    json.dumps(data.topics),
                    now(),
                    until,
                    ident,
                    data.revision,
                ),
            )
            if not cursor.rowcount:
                raise ValueError("Карточка изменилась. Обновите её и повторите действие.")
            db.execute(
                "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
                (ident, "decision", STATUSES[data.status], now()),
            )
        return self.get(ident)

    def add_evidence(self, ident, data):
        self.get(ident)
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM evidence WHERE record_id=? AND url=? AND quote=?",
                (ident, str(data.url), data.quote),
            ).fetchone()
            if not existing:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO evidence"
                    "(record_id,title,url,quote,source_text,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (ident, data.title, str(data.url), data.quote, data.source_text, now()),
                ).rowcount
                if not inserted:
                    return self.get(ident)
                db.execute(
                    "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
                    (
                        ident,
                        "evidence",
                        "Добавлена дословная цитата. Подлинность источника проверяет аналитик.",
                        now(),
                    ),
                )
        return self.get(ident)

    def save_brief(self, ident, payload):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM briefs WHERE record_id=? AND fingerprint=?", (ident, fingerprint)
            ).fetchone()
            if existing:
                return {**dict(existing), "payload": json.loads(existing["payload"])}
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM briefs WHERE record_id=?", (ident,)
            ).fetchone()[0]
            cur = db.execute(
                "INSERT INTO briefs(record_id,version,fingerprint,payload,created_at) "
                "VALUES(?,?,?,?,?)",
                (ident, version, fingerprint, json.dumps(payload, ensure_ascii=False), now()),
            )
            db.execute(
                "INSERT INTO events(record_id,action,detail,created_at) VALUES(?,?,?,?)",
                (ident, "brief", f"Собран черновик расклада v{version}", now()),
            )
            return {
                "id": cur.lastrowid,
                "version": version,
                "payload": payload,
                "created_at": now(),
            }
