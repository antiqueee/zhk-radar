"""Same-query search comparison; same-document extraction comparison. No invented winner."""

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from radar.web.configuration import (
    SETTINGS_LOCK,
    atomic_write,
    configuration,
    model_config,
    raw_model_config,
)
from radar.web.discovery import SearchDocument, call_json, extract, yandex
from radar.web.operations import BudgetExceeded, reserve_call
from radar.web.sources import canonical_url
from radar.web.store import now

PROVIDERS = {
    "yandex": ["YANDEX_SEARCH_API_KEY", "YANDEX_FOLDER_ID"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "brave": ["BRAVE_SEARCH_API_KEY"],
}


class BenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["live", "demo"] = "live"
    queries: list[str] = Field(min_length=1, max_length=4)
    providers: list[Literal["yandex", "perplexity", "brave"]] = Field(
        default=["yandex", "perplexity"], min_length=2, max_length=3
    )
    model_roles: list[Literal["benchmark_a", "benchmark_b"]] = Field(
        default_factory=list, max_length=2
    )

    @field_validator("queries")
    @classmethod
    def validate_queries(cls, values):
        if any(not q.strip() or len(q) > 500 or len(q.split()) > 75 for q in values):
            raise ValueError("Введите от 1 до 4 запросов, каждый до 500 символов и 75 слов")
        return list(dict.fromkeys(q.strip() for q in values))

    @field_validator("providers", "model_roles")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Выберите разные варианты")
        return values


class BenchmarkModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    benchmark_a: str = Field(default="", max_length=150, pattern=r"^[a-zA-Z0-9._:/-]*$")
    benchmark_b: str = Field(default="", max_length=150, pattern=r"^[a-zA-Z0-9._:/-]*$")


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(max_length=500)
    url: str = Field(max_length=3000)
    relevant: bool | None


def save_models(root, data):
    with SETTINGS_LOCK:
        cfg = raw_model_config(root)
        for role, model in data.model_dump().items():
            cfg[role] = {"model": model, "max_tokens": 4500, "temperature": 0}
        atomic_write(root / "config/models.yaml", yaml.safe_dump(cfg, allow_unicode=True), 0o644)


def search_provider(client, cfg, provider, query):
    if provider == "yandex":
        return yandex(client, cfg, query, "month")
    if provider == "perplexity":
        data = call_json(
            client,
            "https://api.perplexity.ai/search",
            {"Authorization": "Bearer " + cfg["PERPLEXITY_API_KEY"]},
            {
                "query": query,
                "max_results": 10,
                "search_language_filter": ["ru"],
                "country": "RU",
                "search_recency_filter": "month",
            },
            "Perplexity Search",
        )
        rows = data.get("results", [])
    else:
        data = call_json(
            client,
            "https://api.search.brave.com/res/v1/web/search",
            {"X-Subscription-Token": cfg["BRAVE_SEARCH_API_KEY"]},
            {"q": query, "count": 10, "country": "RU", "search_lang": "ru", "freshness": "pm"},
            "Brave Search",
        )
        rows = data.get("web", {}).get("results", [])
    if not isinstance(rows, list):
        raise ValueError("Поисковик вернул неподдерживаемый формат")
    documents = []
    for row in rows[:10]:
        try:
            doc = SearchDocument(
                url=canonical_url(row["url"]),
                title=row.get("title", "")[:1000],
                text=(row.get("snippet") or row.get("description") or "")[:16000],
                query=query,
            )
            documents.append(doc.model_dump(mode="json"))
        except (ValueError, KeyError, TypeError):
            continue
    return documents


def evaluate_findings(result, documents):
    valid, rejected = [], 0
    for finding in result.objects:
        if finding.source_index >= len(documents):
            rejected += 1
            continue
        source = documents[finding.source_index]
        if finding.quote not in source["text"] or finding.name not in source["text"]:
            rejected += 1
            continue
        valid.append(finding.model_dump(mode="json"))
    return {
        "accepted": valid,
        "rejected": rejected,
        "count": len(result.objects),
        "quote_match_rate": round(len(valid) / len(result.objects), 3) if result.objects else None,
    }


class BenchmarkRunner:
    def __init__(self, store, search=search_provider, extractor=extract):
        self.store, self.search, self.extractor = store, search, extractor
        self.lock = threading.Lock()
        self.thread = None
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS benchmark_runs(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL,
                result TEXT, error TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS benchmark_cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS benchmark_reviews(
                run_id TEXT, query TEXT, url TEXT, relevant INTEGER,
                PRIMARY KEY(run_id,query,url));
            """)

    def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE benchmark_runs SET status='interrupted',error='Приложение "
                "остановилось. Повторите сравнение с сохранённых шагов.' WHERE "
                "status='running'"
            )

    def close(self):
        if self.thread:
            self.thread.join(2)

    def config(self):
        cfg, models = configuration(self.store.root), model_config(self.store.root)
        return {
            "providers": {p: all(cfg.get(k) for k in keys) for p, keys in PROVIDERS.items()},
            "models": {
                role: models.get(role, {}).get("model", "")
                for role in ("benchmark_a", "benchmark_b")
            },
        }

    def get(self, ident=None):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM benchmark_runs WHERE id=?"
                if ident
                else "SELECT * FROM benchmark_runs ORDER BY created_at DESC LIMIT 1",
                (ident,) if ident else (),
            ).fetchone()
            if not row:
                if ident:
                    raise KeyError(ident)
                return None
            result = dict(row)
            result["request"] = json.loads(result["request"])
            result["result"] = json.loads(result["result"]) if result["result"] else None
            result["reviews"] = [
                dict(r)
                for r in db.execute(
                    "SELECT query,url,relevant FROM benchmark_reviews WHERE run_id=?", (row["id"],)
                )
            ]
        return result

    def submit(self, request):
        cfg = self.config()
        if request.mode == "live":
            if any(not cfg["providers"][p] for p in request.providers):
                raise ValueError("Сохраните ключи выбранных поисковиков")
            if len(request.model_roles) == 2 and (
                cfg["models"][request.model_roles[0]] == cfg["models"][request.model_roles[1]]
            ):
                raise ValueError("Для сравнения выберите две разные модели")
            if request.model_roles and (
                not configuration(self.store.root).get("OPENROUTER_API_KEY")
                or any(not cfg["models"][r] for r in request.model_roles)
            ):
                raise ValueError("Для сравнения моделей сохраните OpenRouter и обе модели")
        models = model_config(self.store.root)
        ident = hashlib.sha256(
            json.dumps(
                [
                    request.model_dump(),
                    datetime.now(UTC).date().isoformat(),
                    {r: models.get(r) for r in request.model_roles},
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.lock:
            if self.thread and self.thread.is_alive():
                current = self.get()
                if current and current["id"] == ident:
                    return current
                raise ValueError("Сравнение уже выполняется")
            with self.store.connect() as db:
                old = db.execute(
                    "SELECT status FROM benchmark_runs WHERE id=?", (ident,)
                ).fetchone()
                if old and old["status"] == "completed":
                    return self.get(ident)
                db.execute(
                    "INSERT INTO benchmark_runs VALUES(?,'running',?,NULL,NULL,?) "
                    "ON CONFLICT(id) DO UPDATE SET status='running',error=NULL,result=NULL",
                    (ident, request.model_dump_json(), now()),
                )
            self.thread = threading.Thread(
                target=self.execute, args=(ident, request), daemon=True, name="radar-benchmark"
            )
            self.thread.start()
        return self.get(ident)

    def cached(self, ident, parts, operation):
        key = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM benchmark_cache WHERE key=?", (key,)).fetchone()
        if row:
            return json.loads(row[0]), True
        reserve_call(self.store, ident, "benchmark:" + parts[0])
        value = operation()
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO benchmark_cache VALUES(?,?)", (key, json.dumps(value))
            )
        return value, False

    def execute(self, ident, request):
        results = {
            "search": [],
            "models": [],
            "documents": [],
            "mode": request.mode,
            "external_requests": 0,
            "cache_hits": 0,
            "limitations": (
                "Релевантность оценивает аналитик по ссылкам. Без эталона полнота неизвестна. "
                "Модели получают одинаковые поисковые фрагменты, не полный интернет."
            ),
        }
        cfg = configuration(self.store.root)
        models = model_config(self.store.root)
        try:
            with httpx.Client(timeout=httpx.Timeout(90, connect=15)) as client:
                for query in request.queries:
                    for provider in request.providers:
                        started = time.monotonic()
                        try:
                            if request.mode == "demo":
                                docs = [
                                    {
                                        "url": "https://example.org/demo-publication",
                                        "title": "Учебная публикация",
                                        "text": (
                                            "ЖК Учебный берег: в вымышленном примере "
                                            "жители обсуждают перенос "
                                            "ключей."
                                        ),
                                        "query": query,
                                    }
                                ]
                                hit = False
                            else:
                                docs, hit = self.cached(
                                    ident,
                                    [provider, now()[:10], query, "month-v1"],
                                    lambda p=provider, q=query: self.search(client, cfg, p, q),
                                )
                            # Canonicalized within each query to avoid rewarding duplicate URLs.
                            docs = list(
                                {
                                    canonical_url(d["url"]): {**d, "url": canonical_url(d["url"])}
                                    for d in docs
                                }.values()
                            )
                            results["external_requests"] += request.mode == "live" and not hit
                            results["cache_hits"] += hit
                            results["search"].append(
                                {
                                    "provider": provider,
                                    "query": query,
                                    "documents": docs,
                                    "domains": len({urlsplit(d["url"]).hostname for d in docs}),
                                    "seconds": round(time.monotonic() - started, 2),
                                    "cached": hit,
                                }
                            )
                        except BudgetExceeded:
                            raise
                        except ValueError as exc:
                            results["search"].append(
                                {
                                    "provider": provider,
                                    "query": query,
                                    "error": str(exc),
                                    "documents": [],
                                }
                            )
                # Interleave providers: no provider gets exclusive control of model input.
                pool = {}
                for rank in range(10):
                    for row in results["search"]:
                        if rank < len(row["documents"]):
                            d = row["documents"][rank]
                            pool.setdefault(
                                canonical_url(d["url"]),
                                {**d, "source_kind": "snippet", "text": d["text"][:2000]},
                            )
                documents = list(pool.values())[:8]
                results["documents"] = documents
                for role in request.model_roles:
                    started = time.monotonic()
                    try:
                        if not documents:
                            raise ValueError("Нет фрагментов для сравнения моделей")
                        if request.mode == "demo":
                            evaluation = {
                                "accepted": [],
                                "rejected": 0,
                                "count": 0,
                                "quote_match_rate": None,
                            }
                            hit = False
                        else:
                            evaluation, hit = self.cached(
                                ident,
                                ["model", "extract-v3", models[role], documents],
                                lambda r=role: evaluate_findings(
                                    self.extractor(client, cfg, models[r], documents), documents
                                ),
                            )
                        results["external_requests"] += request.mode == "live" and not hit
                        results["cache_hits"] += hit
                        results["models"].append(
                            {
                                "role": role,
                                "model": models.get(role, {}).get("model", ""),
                                **evaluation,
                                "seconds": round(time.monotonic() - started, 2),
                                "cached": hit,
                            }
                        )
                    except BudgetExceeded:
                        raise
                    except ValueError as exc:
                        results["models"].append({"role": role, "error": str(exc)})
            status = (
                "partial"
                if any(r.get("error") for r in results["search"] + results["models"])
                else "completed"
            )
            error = None
        except ValueError as exc:
            status, error = "failed", str(exc)
        except Exception:
            status, error = "failed", "Сравнение не завершено. Успешные шаги сохранены"
        with self.store.connect() as db:
            db.execute(
                "UPDATE benchmark_runs SET status=?,result=?,error=? WHERE id=?",
                (status, json.dumps(results), error, ident),
            )

    def review(self, ident, data):
        run = self.get(ident)
        if not any(
            row["query"] == data.query and any(d["url"] == data.url for d in row["documents"])
            for row in (run["result"] or {}).get("search", [])
        ):
            raise ValueError("Источник не найден в результатах этого сравнения")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO benchmark_reviews VALUES(?,?,?,?) ON CONFLICT(run_id,query,url) "
                "DO UPDATE SET relevant=excluded.relevant",
                (ident, data.query, data.url, data.relevant),
            )
        return self.get(ident)
