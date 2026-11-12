import io
import json
import shutil
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader

from radar.briefs.offers import load_offers
from radar.briefs.report import build_report
from radar.web.benchmark import (
    BenchmarkRequest,
    BenchmarkRunner,
    Review,
    evaluate_findings,
    search_provider,
)
from radar.web.configuration import write_secrets
from radar.web.discovery import Findings
from radar.web.operations import usage
from radar.web.store import Store, now


@pytest.fixture
def store(tmp_path):
    shutil.copytree(Path("config"), tmp_path / "config")
    return Store(tmp_path)


def execute(runner, request):
    # Submit normally to exercise durable job state; no external calls in demo.
    job = runner.submit(request)
    runner.thread.join(5)
    assert not runner.thread.is_alive()
    return runner.get(job["id"])


def test_demo_comparison_is_labelled_offline_and_idempotent(store):
    runner = BenchmarkRunner(store)
    request = BenchmarkRequest(mode="demo", queries=["ЖК Москва жалобы"])
    result = execute(runner, request)
    assert result["status"] == "completed"
    assert result["result"]["external_requests"] == 0
    assert result["result"]["mode"] == "demo"
    assert runner.submit(request)["id"] == result["id"]
    assert usage(store)["calls"] == 0


def test_same_query_comparison_reviews_and_cache(store):
    write_secrets(
        store.root,
        {"YANDEX_SEARCH_API_KEY": "test", "YANDEX_FOLDER_ID": "test", "PERPLEXITY_API_KEY": "test"},
    )
    seen = []

    def search(client, cfg, provider, query):
        seen.append((provider, query))
        return [
            {
                "url": "https://example.org/news",
                "title": "ЖК",
                "text": "Сведения из публикации",
                "query": query,
            }
        ]

    runner = BenchmarkRunner(store, search=search)
    request = BenchmarkRequest(queries=["ЖК Москва жалобы"])
    result = execute(runner, request)
    assert seen == [("yandex", request.queries[0]), ("perplexity", request.queries[0])]
    assert usage(store)["calls"] == 2
    reviewed = runner.review(
        result["id"],
        Review(query=request.queries[0], url="https://example.org/news", relevant=True),
    )
    assert reviewed["reviews"][0]["relevant"] == 1
    with pytest.raises(ValueError):
        runner.review(
            result["id"], Review(query=request.queries[0], url="https://other.org", relevant=True)
        )
    # Simulate restart after paid steps completed, before final job commit.
    with store.connect() as db:
        db.execute("UPDATE benchmark_runs SET status='interrupted'")
    again = execute(runner, request)
    assert again["result"]["cache_hits"] == 2 and len(seen) == 2
    assert len(again["reviews"]) == 1


def test_search_adapters_have_same_locale_and_no_raw_credentials():
    requests = []

    def transport(request):
        requests.append((str(request.url), json.loads(request.content)))
        if "perplexity" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [{"url": "https://example.org/a", "title": "ЖК", "snippet": "Текст"}]
                },
            )
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"url": "https://example.org/a", "title": "ЖК", "description": "Текст"}
                    ]
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        cfg = {"PERPLEXITY_API_KEY": "secret", "BRAVE_SEARCH_API_KEY": "secret"}
        for provider in ("perplexity", "brave"):
            assert search_provider(client, cfg, provider, "ЖК Москва")[0]["text"] == "Текст"
    assert all(body["country"] == "RU" for url, body in requests)
    assert requests[0][1]["query"] == requests[1][1]["q"]
    assert all("secret" not in url for url, body in requests)


def test_model_quote_and_object_grounding():
    doc = {"text": "ЖК Северный: жители сообщают о переносе передачи ключей."}
    finding = dict(
        name="ЖК Северный",
        region="Москва",
        summary="В публикации говорится о переносе.",
        quote="жители сообщают о переносе передачи ключей.",
        source_index=0,
    )
    result = Findings.model_validate({"objects": [finding, {**finding, "name": "ЖК Выдуманный"}]})
    evaluation = evaluate_findings(result, [doc])
    assert evaluation["quote_match_rate"] == 0.5 and evaluation["rejected"] == 1


def test_case_products_and_unbounded_pdf_preserve_every_quote(store):
    library = load_offers(store.root / "config/offers.yaml")
    brief = {
        **store.get("demo-3"),
        "building": "корпус 2",
        "topics": ["протечки", "качество_ук"],
        "offers": [library.offers[0].model_dump()],
        "dossier": {"signals": []},
        "chat_stats": None,
        "limitation": "Учебный сценарий",
        "created_at": now(),
        "version": 1,
    }
    brief["evidence"] = [
        {
            "url": f"https://example.org/{i}",
            "title": f"Источник {i}",
            "quote": f"Уникальная цитата номер {i}. " + "Длинное описание обстоятельств. " * 12,
        }
        for i in range(12)
    ]
    brief["urls"] = [e["url"] for e in brief["evidence"]]
    brief["report"] = build_report(brief, library)
    products = brief["report"]["products"]
    engineering = next(p for p in products if p["code"] == "O7")
    assert engineering["conditional"] and "корпус 2" in engineering["scope"]
    assert "УК" in engineering["tasks"][0]
    assert not any(p["code"] == "O4" for p in products)
    from radar.web.pdf import render_detailed_pdf

    reader = PdfReader(io.BytesIO(render_detailed_pdf(brief)))
    text = "".join(p.extract_text() for p in reader.pages)
    assert len(reader.pages) > 7
    assert all(e["quote"].split(". ")[0] in text for e in brief["evidence"])
    assert "Пилот на 30 дней" in text and "МЕГАПОЛИС" in text
    assert sum(len(page.get("/Annots", [])) for page in reader.pages) >= 12


def test_compared_models_receive_identical_balanced_documents(store):
    from radar.web.benchmark import BenchmarkModels, save_models

    write_secrets(
        store.root,
        {
            "YANDEX_SEARCH_API_KEY": "test",
            "YANDEX_FOLDER_ID": "test",
            "PERPLEXITY_API_KEY": "test",
            "OPENROUTER_API_KEY": "test",
        },
    )
    save_models(store.root, BenchmarkModels(benchmark_a="test/a", benchmark_b="test/b"))

    def search(client, cfg, provider, query):
        return [
            {
                "url": "https://" + provider + ".example.org/news",
                "title": "ЖК Северный",
                "text": "ЖК Северный: жители сообщают о переносе передачи ключей.",
                "query": query,
            }
        ]

    seen = []

    def extractor(client, cfg, model, docs):
        seen.append((model["model"], json.dumps(docs, sort_keys=True)))
        return Findings.model_validate(
            {
                "objects": [
                    {
                        "name": "ЖК Северный" if model["model"] == "test/a" else "ЖК Выдуманный",
                        "region": "Москва",
                        "summary": "В публикации сообщают о переносе.",
                        "source_index": 0,
                        "quote": "жители сообщают о переносе передачи ключей.",
                    }
                ]
            }
        )

    runner = BenchmarkRunner(store, search=search, extractor=extractor)
    run = execute(
        runner, BenchmarkRequest(queries=["ЖК перенос"], model_roles=["benchmark_a", "benchmark_b"])
    )
    assert run["status"] == "completed"
    assert [model for model, docs in seen] == ["test/a", "test/b"]
    assert seen[0][1] == seen[1][1]
    assert len(run["result"]["documents"]) == 2
    assert [m["quote_match_rate"] for m in run["result"]["models"]] == [1, 0]
    assert usage(store)["calls"] == 4
