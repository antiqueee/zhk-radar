"""Registry snapshots with explicit mappings, conservative binding and deterministic scoring."""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from radar.domain import Evidence, House, Sales, Snapshot
from radar.pipeline.scoring import assess
from radar.web.sources import public_get
from radar.web.stoplist import blocked, normalized
from radar.web.store import now

DOM = "https://xn--80az8a.xn--d1aqf.xn--p1ai"
API = DOM + "/%D1%81%D0%B5%D1%80%D0%B2%D0%B8%D1%81%D1%8B/api/kn/object/"
ERZ_KEYS = {
    "moskva": "143443001",
    "moskovskaya-oblast": "143449001",
    "sankt-peterburg": "144781001",
    "leningradskaya-oblast": "143275001",
}


class RegistrySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    house_id: str = Field(pattern=r"^[0-9]{1,12}$")
    name: str = Field(min_length=2, max_length=250)
    address: str = Field(default="", max_length=500)
    developer: str = Field(default="", max_length=250)
    commissioning: date | None = None
    transfer: date | None = None
    rve: date | None = None
    rve_checked: bool = False
    readiness: float | None = Field(default=None, ge=0, le=100)
    flats: int | None = Field(default=None, ge=0)
    observed_at: AwareDatetime
    source_url: HttpUrl
    imported: bool = True

    @model_validator(mode="after")
    def no_future_snapshot(self):
        if self.observed_at > datetime.now(UTC):
            raise ValueError("Дата снимка не может быть в будущем")
        return self


class RegistryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    house_id: str = Field(pattern=r"^[0-9]{1,12}$")
    confirmed: bool = False


class SalesUpdate(Sales):
    # Human-entered business signals must have an explanatory note.
    note: str = Field(min_length=10, max_length=2000)


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS registry_snapshots(
            id TEXT PRIMARY KEY, house_id TEXT NOT NULL, observed_at TEXT NOT NULL,
            payload TEXT NOT NULL, raw TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS registry_house ON registry_snapshots(house_id,observed_at);
        CREATE TABLE IF NOT EXISTS registry_bindings(
            record_id TEXT PRIMARY KEY,house_id TEXT,confirmed INTEGER);
        CREATE TABLE IF NOT EXISTS sales_assessments(
            record_id TEXT PRIMARY KEY,payload TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS registry_runs(
            id INTEGER PRIMARY KEY,kind TEXT,status TEXT,detail TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS erz_snapshots(
            id TEXT PRIMARY KEY,region TEXT,developer TEXT,payload TEXT,observed_at TEXT);
        """)


def parse_dom(raw, house_id):
    if not isinstance(raw, dict):
        raise ValueError("ЕИСЖС: неизвестная схема ответа")
    data = raw.get("data", raw)
    if not isinstance(data, dict) or str(data.get("objId", data.get("id", ""))) != str(house_id):
        raise ValueError("ЕИСЖС вернул другую карточку или неизвестную схему")
    # Only fields whose meanings are explicit in the catalogue contract. No inferred RVE.
    planned = data.get("objReady100PercDt")
    if planned:
        planned = date.fromisoformat(str(planned)[:10])
    dev = data.get("developer") or {}
    if not isinstance(dev, dict):
        raise ValueError("ЕИСЖС: неизвестная схема застройщика")
    return RegistrySnapshot(
        house_id=str(house_id),
        name=data.get("objCommercNm") or data.get("objAddr") or str(house_id),
        address=data.get("objAddr", ""),
        developer=dev.get("devShortCleanNm") or dev.get("shortName", ""),
        commissioning=planned,
        flats=data.get("objElemLivingCnt"),
        observed_at=datetime.now(UTC),
        source_url=DOM
        + "/%D1%81%D0%B5%D1%80%D0%B2%D0%B8%D1%81%D1%8B/"
        + "%D0%BA%D0%B0%D1%82%D0%B0%D0%BB%D0%BE%D0%B3-"
        + "%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D1%80%D0%BE%D0%B5%D0%BA/"
        + str(house_id),
        imported=False,
    )


def save_snapshot(store, snapshot, raw=None):
    initialize(store)
    payload = snapshot.model_dump(mode="json")
    payload["observed_at"] = snapshot.observed_at.astimezone(UTC).isoformat()
    ident = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with store.connect() as db:
        added = db.execute(
            "INSERT OR IGNORE INTO registry_snapshots VALUES(?,?,?,?,?)",
            (
                ident,
                snapshot.house_id,
                payload["observed_at"],
                json.dumps(payload),
                json.dumps(raw or payload),
            ),
        ).rowcount
    return {"id": ident, "added": bool(added), "snapshot": payload}


def fetch_house(store, house_id, transport=public_get):
    initialize(store)
    if not re.fullmatch(r"[0-9]{1,12}", house_id):
        raise ValueError("Нужен числовой ID дома в ЕИСЖС")
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM registry_snapshots WHERE house_id=? AND observed_at>=? "
            "AND json_extract(payload,'$.imported')=0 ORDER BY observed_at DESC LIMIT 1",
            (house_id, now()[:10]),
        ).fetchone()
    if row:
        return {
            "id": row["id"],
            "added": False,
            "cached": True,
            "snapshot": json.loads(row["payload"]),
        }
    try:
        status, headers, body = transport(API + house_id)
        if status != 200:
            raise ValueError(
                f"ЕИСЖС недоступна: HTTP {status}. Можно импортировать официальный снимок"
            )
        if len(body) > 2_000_000:
            raise ValueError("Слишком большая карточка ЕИСЖС")
        raw = json.loads(body)
        result = save_snapshot(store, parse_dom(raw, house_id), raw)
    except (ValueError, OSError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
            else "Не удалось прочитать ЕИСЖС"
        )
        with store.connect() as db:
            db.execute(
                (
                    "INSERT INTO registry_runs(kind,status,detail,created_at) "
                    "VALUES('eisgs','failed',?,?)"
                ),
                (detail, now()),
            )
        raise ValueError(detail) from None
    return result


def bind(store, record_id, data):
    initialize(store)
    store.get(record_id)
    with store.connect() as db:
        db.execute(
            "INSERT INTO registry_bindings VALUES(?,?,?) ON CONFLICT(record_id) "
            "DO UPDATE SET house_id=excluded.house_id,confirmed=excluded.confirmed",
            (record_id, data.house_id, data.confirmed),
        )
    return dossier(store, record_id)


def save_sales(store, record_id, data):
    initialize(store)
    store.get(record_id)
    with store.connect() as db:
        db.execute(
            "INSERT INTO sales_assessments VALUES(?,?,?) ON CONFLICT(record_id) "
            "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
            (record_id, data.model_dump_json(), now()),
        )
    return dossier(store, record_id)


def dossier(store, record_id):
    initialize(store)
    record = store.get(record_id)
    with store.connect() as db:
        binding = db.execute(
            "SELECT * FROM registry_bindings WHERE record_id=?", (record_id,)
        ).fetchone()
        sales_row = db.execute(
            "SELECT payload FROM sales_assessments WHERE record_id=?", (record_id,)
        ).fetchone()
        rows = (
            list(
                db.execute(
                    "SELECT * FROM registry_snapshots WHERE house_id=? ORDER BY observed_at,id",
                    (binding["house_id"],),
                )
            )
            if binding
            else []
        )
    snapshots = [RegistrySnapshot.model_validate_json(r["payload"]) for r in rows]
    changes = []
    for previous, current in zip(snapshots, snapshots[1:], strict=False):
        for field in ("commissioning", "transfer", "readiness", "rve"):
            before, after = getattr(previous, field), getattr(current, field)
            if before is not None and after is not None and before != after:
                changes.append(
                    {
                        "field": field,
                        "before": str(before),
                        "after": str(after),
                        "observed_at": current.observed_at.isoformat(),
                        "source_url": str(current.source_url),
                    }
                )
    result = {
        "binding": dict(binding) if binding else None,
        "snapshots": [s.model_dump(mode="json") for s in snapshots],
        "changes": changes,
        "assessment": None,
        "sales": json.loads(sales_row[0]) if sales_row else None,
        "limitations": [
            "Привязку корпуса подтверждает оператор; импорт не сертифицирует подлинность документа."
        ],
    }
    if not binding or not binding["confirmed"] or not snapshots:
        return result
    first, last = snapshots[0], snapshots[-1]
    house = House(
        eisgs_id=last.house_id,
        label=last.name,
        flats=last.flats,
        commissioning_initial=first.commissioning,
        commissioning=last.commissioning,
        transfer_initial=first.transfer,
        transfer=last.transfer,
        readiness=last.readiness,
        rve=last.rve,
        rve_checked=last.rve_checked,
        commissioning_shifts=sum(
            c["field"] == "commissioning" and c["after"] > c["before"] for c in changes
        ),
        transfer_shifts=sum(c["field"] == "transfer" and c["after"] > c["before"] for c in changes),
    )
    sales = Sales.model_validate({k: v for k, v in (result["sales"] or {}).items() if k != "note"})
    sales = sales.model_copy(update={"stoplisted": bool(blocked(store, record))})
    # A registry observation supplies provenance, not a fabricated conflict classification.
    evidence = [
        Evidence(
            kind="registry",
            occurred_at=last.observed_at.date(),
            trust="C" if last.imported else "A",
            origin="eisgs:" + last.house_id,
            quote=rows[-1]["payload"],
            document_id=rows[-1]["id"],
        )
    ]
    history = [Snapshot(on=s.observed_at.date(), readiness=s.readiness) for s in snapshots]
    score = assess(
        house,
        evidence,
        sales,
        history,
        datetime.now(ZoneInfo("Europe/Moscow")).date(),
        yaml.safe_load((store.root / "config/scoring.yaml").read_text()),
    ).model_dump()
    score["limitations"].extend(
        [
            "Первоначальный срок — первый сохранённый снимок, не первый ДДУ.",
            (
                "Скоринг пока учитывает реестр и введённые бизнес-признаки; публикации "
                "требуют отдельной проверки."
            ),
        ]
    )
    if last.imported:
        score["limitations"].append(
            "Снимок загружен оператором, источник не проверен автоматически"
        )
    if (datetime.now(UTC) - last.observed_at).days > 30:
        score["eligible"] = False
        score["limitations"].append("Снимок старше 30 дней: приоритет требует обновления")
    result["assessment"] = score
    return result


class ErzParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.stack = [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "app-developer-item":
            self.row = {"cells": {}, "name": "", "url": ""}
        col = next(
            (
                int(c.rsplit("-", 1)[1])
                for c in attrs.get("class", "").split()
                if re.fullmatch(r"developer-td-\d+", c)
            ),
            None,
        )
        self.stack.append((tag, col, "developer-td__title" in attrs.get("class", "")))
        if (
            self.row is not None
            and tag == "a"
            and "/zastroyschiki/" in attrs.get("href", "")
            and not self.row["url"]
        ):
            self.row["url"] = urljoin("https://erzrf.ru", attrs["href"])

    def handle_endtag(self, tag):
        if tag == "app-developer-item" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.row is None or any(x[2] for x in self.stack):
            return
        col = next((x[1] for x in reversed(self.stack) if x[1] is not None), None)
        if col:
            self.row["cells"][col] = self.row["cells"].get(col, "") + data
            if col == 3 and any(x[0] == "a" for x in self.stack):
                self.row["name"] += data


def parse_erz(html):
    parser = ErzParser()
    parser.feed(html)
    rows = []
    for row in parser.rows:

        def number(col, row=row):
            value = re.sub(r"[\s\u00a0%]", "", row["cells"].get(col, "")).replace(",", ".")
            return float(value) if value and re.fullmatch(r"\d+(?:\.\d+)?", value) else None

        name = row["name"].strip()
        if name:
            share = number(6)
            if share is not None and not 0 <= share <= 100:
                raise ValueError("ЕРЗ: доля вне диапазона, проверьте схему")
            rows.append(
                {
                    "developer": name,
                    "building_area": number(4),
                    "delayed_area": number(5),
                    "delayed_share_pct": share,
                    "shift_months": number(7),
                    "url": row["url"],
                }
            )
    if not rows:
        raise ValueError("ЕРЗ: таблица не найдена. Формат страницы мог измениться")
    return rows


def fetch_erz(store, region="moskva", transport=public_get):
    initialize(store)
    if region not in ERZ_KEYS:
        raise ValueError("Неизвестный регион ЕРЗ")
    url = (
        "https://erzrf.ru/top-zastroyshchikov/"
        + region
        + f"?region={region}&regionKey={ERZ_KEYS[region]}&topType=0"
    )
    status, headers, body = transport(url)
    if status != 200:
        raise ValueError(f"ЕРЗ недоступен: HTTP {status}")
    if len(body) > 2_000_000:
        raise ValueError("ЕРЗ: страница превышает лимит")
    rows = parse_erz(body.decode("utf-8"))
    # The slug alone silently returns Russia-wide values. Require the regional
    # key in the actual portfolio links rendered by ERZ, not just our request.
    if any(parse_qs(urlsplit(r["url"]).query).get("regionKey") != [ERZ_KEYS[region]] for r in rows):
        raise ValueError("ЕРЗ не подтвердил регион таблицы; показатели не сохранены")
    stamp = now()
    rows = [{**r, "source_url": url, "observed_at": stamp, "scope_verified": True} for r in rows]
    with store.connect() as db:
        for row in rows:
            payload = {**row, "source_url": url, "observed_at": stamp}
            ident = hashlib.sha256(
                (
                    region
                    + normalized(row["developer"])
                    + stamp[:7]
                    + json.dumps(row, sort_keys=True)
                ).encode()
            ).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO erz_snapshots VALUES(?,?,?,?,?)",
                (ident, region, row["developer"], json.dumps(payload), stamp),
            )
        db.execute(
            (
                "INSERT INTO registry_runs(kind,status,detail,created_at) "
                "VALUES('erz','completed',?,?)"
            ),
            (f"{region}: {len(rows)} строк. Первая страница рейтинга; охват ограничен.", stamp),
        )
    return {
        "region": region,
        "rows": rows,
        "partial": True,
        "limitation": (
            "Первая страница рейтинга; показатели портфеля не переносятся на конкретный корпус"
        ),
    }
