"""Bounded official hh/VK APIs and INN-scoped public Fedresurs discovery.

These are contextual observations, never proof of insolvency or a contract opportunity.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from radar.web import stoplist
from radar.web.chats import redact
from radar.web.configuration import configuration
from radar.web.operations import reserve_call
from radar.web.sources import ArticleParser
from radar.web.store import now
from radar.web.workflow import cached, query_documents


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hh_employer: str = Field(default="", pattern=r"^\d{0,12}$")
    vk_group: str = Field(default="", pattern=r"^[A-Za-z0-9_]{0,80}$")
    inn: str = Field(default="", pattern=r"^(?:\d{10}|\d{12})?$")
    legal_name: str = Field(default="", max_length=250)
    confirmed: bool = False
    automatic: bool = False

    @field_validator("inn")
    @classmethod
    def valid_inn(cls, v):
        if not v:
            return v
        digits = list(map(int, v))

        def check(weights):
            return sum(a * b for a, b in zip(digits, weights, strict=False)) % 11 % 10

        if len(v) == 10:
            valid = digits[9] == check([2, 4, 10, 3, 5, 9, 4, 6, 8])
        else:
            valid = digits[10] == check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) and digits[11] == check(
                [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
            )
        if not valid:
            raise ValueError("Контрольные цифры ИНН не совпадают")
        return v


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: str
    title: str = Field(max_length=500)
    text: str = Field(max_length=16000)
    url: str
    published_at: str | None = None
    trust: str


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS extra_bindings(
            record_id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS extra_results(record_id TEXT PRIMARY KEY,
            payload TEXT,updated_at TEXT);""")


def status(store, ident):
    initialize(store)
    store.get(ident)
    with store.connect() as db:
        binding = db.execute(
            "SELECT payload FROM extra_bindings WHERE record_id=?", (ident,)
        ).fetchone()
        result = db.execute(
            "SELECT payload FROM extra_results WHERE record_id=?", (ident,)
        ).fetchone()
    return {
        "binding": json.loads(binding[0]) if binding else Binding().model_dump(),
        "result": json.loads(result[0]) if result else None,
    }


def bind(store, ident, value):
    initialize(store)
    store.get(ident)
    if value.inn and not value.legal_name.strip():
        raise ValueError("Для ИНН укажите проверенное наименование юридического лица")
    if value.automatic and not value.confirmed:
        raise ValueError("Сначала подтвердите связь работодателя, группы и юрлица с объектом")
    with store.connect() as db:
        db.execute(
            "INSERT INTO extra_bindings VALUES(?,?) ON CONFLICT(record_id) "
            "DO UPDATE SET payload=excluded.payload",
            (ident, value.model_dump_json()),
        )
        # Old observations must not survive a binding change as current input to author.
        db.execute("DELETE FROM extra_results WHERE record_id=?", (ident,))
    return status(store, ident)


def get_json(client, url, *, params=None, data=None, headers=None):
    try:
        response = (
            client.post(url, data=data, headers=headers)
            if data is not None
            else client.get(url, params=params, headers=headers)
        )
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("Ответ источника превышает лимит")
        value = response.json()
        if not isinstance(value, dict) or value.get("error"):
            raise ValueError("Источник отклонил запрос")
        return value
    except (httpx.HTTPError, ValueError):
        raise ValueError(
            "Источник не отдал допустимый ответ. Проверьте доступ и лимиты API."
        ) from None


def plain(value):
    parser = ArticleParser()
    parser.feed(value or "")
    return redact(" ".join(parser.text), [])


def observation(kind, title, text, url, stamp, trust):
    return Observation(
        id=hashlib.sha256(url.encode()).hexdigest(),
        kind=kind,
        title=redact(title, [])[:500],
        text=redact(text, [])[:16000],
        url=url,
        published_at=stamp,
        trust=trust,
    ).model_dump()


def hh(client, binding, cfg):
    if not cfg.get("HH_USER_AGENT"):
        raise ValueError("Добавьте HH_USER_AGENT с названием приложения и рабочим email")
    value = get_json(
        client,
        "https://api.hh.ru/vacancies",
        params={
            "employer_id": binding.hh_employer,
            "period": 30,
            "per_page": 100,
            "page": 0,
            "text": '"клиентский сервис" OR "связи с общественностью" OR "работа с жителями" OR PR',
        },
        headers={"User-Agent": cfg["HH_USER_AGENT"]},
    )
    rows = []
    for item in value.get("items", [])[:100]:
        if str((item.get("employer") or {}).get("id")) != binding.hh_employer:
            continue
        ident = str(item.get("id", ""))
        if not ident.isdigit():
            continue
        snippet = item.get("snippet") or {}
        rows.append(
            observation(
                "vacancy",
                item.get("name", "Вакансия"),
                plain(snippet.get("requirement", ""))
                + " "
                + plain(snippet.get("responsibility", "")),
                "https://hh.ru/vacancy/" + ident,
                item.get("published_at"),
                "B",
            )
        )
    return {
        "items": rows,
        "partial": value.get("pages", 0) > 1,
        "note": "Вакансии — контекст спроса у работодателя, не доказательство проблемы ЖК.",
    }


def vk(client, binding, cfg, reserve=lambda: None):
    token = cfg.get("VK_SERVICE_TOKEN")
    if not token:
        raise ValueError("Добавьте VK_SERVICE_TOKEN с правами чтения выбранных методов")
    first = True

    def request(method, **parameters):
        nonlocal first
        if not first:
            reserve()
        first = False
        return get_json(
            client,
            "https://api.vk.com/method/" + method,
            data={**parameters, "v": "5.199", "access_token": token},
        )["response"]

    metadata = request("groups.getById", group_id=binding.vk_group)
    groups = metadata.get("groups", []) if isinstance(metadata, dict) else metadata
    if len(groups) != 1 or groups[0].get("is_closed") != 0:
        raise ValueError("Допускаются только открытые сообщества VK")
    group = groups[0]
    wall = request("wall.get", domain=binding.vk_group, count=20, filter="all")
    rows, truncated = [], wall.get("count", 0) > 20
    today = datetime.now(UTC)
    comments_read = 0
    for post in wall.get("items", [])[:20]:
        stamp = datetime.fromtimestamp(post["date"], UTC)
        if not 0 <= (today - stamp).days <= 30 or post.get("owner_id") != -int(group["id"]):
            continue
        url = f"https://vk.com/wall{post['owner_id']}_{int(post['id'])}"
        rows.append(
            observation(
                "vk_post",
                group.get("name", "Открытая группа"),
                post.get("text", ""),
                url,
                stamp.isoformat(),
                "C",
            )
        )
        if post.get("comments", {}).get("count", 0) and comments_read < 2:
            comments = request(
                "wall.getComments",
                owner_id=post["owner_id"],
                post_id=post["id"],
                count=50,
                sort="desc",
                thread_items_count=0,
            )
            comments_read += 1
            truncated = truncated or comments.get("count", 0) > 50
            for c in comments.get("items", [])[:50]:
                if not c.get("text"):
                    continue
                rows.append(
                    observation(
                        "vk_comment",
                        "Комментарий в открытой группе",
                        c["text"],
                        url + "?reply=" + str(int(c["id"])),
                        datetime.fromtimestamp(c["date"], UTC).isoformat(),
                        "C",
                    )
                )
        elif post.get("comments", {}).get("count", 0):
            truncated = True
    return {
        "items": rows,
        "partial": truncated,
        "note": "Последние 20 постов за 30 дней и до 50 комментариев к двум постам. "
        "Ответы в ветках не загружены. Принадлежность каждого сообщения ЖК требует проверки.",
    }


def fedresurs(store, client, binding, run):
    documents, _ = query_documents(
        store, '"' + binding.inn + '" site:bankrot.fedresurs.ru', run, client, max_pages=4
    )
    rows = []
    for doc in documents:
        host = urlsplit(doc["url"]).hostname or ""
        if (
            host != "bankrot.fedresurs.ru"
            or doc["source_kind"] != "page"
            or not re.search(r"(?<!\d)" + binding.inn + r"(?!\d)", doc["text"])
        ):
            continue
        rows.append(
            observation(
                "registry_mention",
                "Упоминание ИНН: " + binding.legal_name,
                doc["text"],
                doc["url"],
                doc.get("published_at"),
                "A",
            )
        )
    return {
        "items": rows,
        "partial": True,
        "note": "Поиск по открытым индексируемым страницам ЕФРСБ, не полный реестр. "
        "Совпадение ИНН требует проверки роли юрлица в документе. "
        "Юридический статус автоматически не устанавливается; "
        "отсутствие находок ничего не доказывает.",
    }


def collect(store, ident, run, client=None, collectors=None, scheduled=False):
    record = store.get(ident)
    if record["dataset"] == "demo" or stoplist.blocked(store, record):
        raise ValueError("Внешние источники отключены для демо и объектов стоп-листа")
    previous = status(store, ident)
    binding = Binding.model_validate(previous["binding"])
    if not binding.confirmed:
        raise ValueError("Подтвердите привязку источников к объекту")
    cfg = configuration(store.root)
    client, owns = client or httpx.Client(timeout=40), client is None
    collectors = collectors or {"hh": hh, "vk": vk}
    results = {}
    try:
        for kind, field in [
            ("hh", binding.hh_employer),
            ("vk", binding.vk_group),
            ("fedresurs", binding.inn),
        ]:
            if not field:
                continue
            old = ((previous["result"] or {}).get("sources") or {}).get(kind)
            if scheduled and old and old.get("observed_at") and old["status"] != "failed":
                age = (datetime.now(UTC) - datetime.fromisoformat(old["observed_at"])).days
                if age < (30 if kind == "fedresurs" else 7):
                    results[kind] = old
                    continue
            try:
                # Credentials are excluded from content cache and output.
                if kind == "fedresurs":
                    if not all(cfg.get(k) for k in ("YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID")):
                        raise ValueError("Для поиска ЕФРСБ нужны ключи Yandex Search")

                    def fn():
                        return fedresurs(store, client, binding, run)
                elif kind == "vk" and collectors[kind] is vk:

                    def fn():
                        return vk(
                            client,
                            binding,
                            cfg,
                            reserve=lambda: reserve_call(store, run, "source-vk"),
                        )
                else:

                    def fn(kind=kind):
                        return collectors[kind](client, binding, cfg)

                # Fedresurs reserves calls in query_documents; this wrapper reserves one external
                # operation for the other APIs as well, keeping all network work bounded.
                key = ["extra-v1", kind, binding.model_dump(), now()[:10]]
                if kind == "fedresurs":
                    value = fn()
                else:
                    value, _ = cached(store, key, fn, run, "source-" + kind)
                value["items"] = [
                    Observation.model_validate(i).model_dump() for i in value["items"]
                ]
                results[kind] = {
                    "status": "partial" if value["partial"] else "completed",
                    "observed_at": now(),
                    **value,
                }
            except (ValueError, KeyError, TypeError, OverflowError):
                results[kind] = {
                    "status": "failed",
                    "items": [],
                    "error": "Источник не обработан. Проверьте идентификатор, доступ и лимиты.",
                }
        value = {
            "sources": results,
            "observed_at": now(),
            "status": "partial"
            if any(r["status"] != "completed" for r in results.values())
            else "completed",
        }
        with store.connect() as db:
            db.execute(
                "INSERT INTO extra_results VALUES(?,?,?) ON CONFLICT(record_id) "
                "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (ident, json.dumps(value), now()),
            )
        return value
    finally:
        if owns:
            client.close()
