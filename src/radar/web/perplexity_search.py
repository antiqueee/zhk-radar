"""Perplexity adapters for finding source pages."""

from datetime import UTC, datetime, timedelta

from radar.web.money import PERPLEXITY_REQUEST_USD
from radar.web.sources import canonical_url

MAX_RESULTS = 10


def search_perplexity(client, cfg, query, period):
    from radar.web.discovery import call_json

    filters = {}
    if period == "fortnight":
        cutoff = datetime.now(UTC).date() - timedelta(days=14)
        filters["search_after_date_filter"] = cutoff.strftime("%m/%d/%Y")
    elif period == "month":
        filters["search_recency_filter"] = "month"
    elif period != "all":
        raise ValueError("Неизвестный период поиска Perplexity")
    data = call_json(
        client,
        "https://api.perplexity.ai/search",
        {"Authorization": "Bearer " + cfg["PERPLEXITY_API_KEY"]},
        {"query": query, "max_results": MAX_RESULTS, "search_language_filter": ["ru"],
         "country": "RU", **filters},
        "Perplexity Search",
        fixed_cost=PERPLEXITY_REQUEST_USD,
        timeout=8.0,
    )
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Perplexity вернул выдачу в неожиданном формате")
    return _search_documents(rows[:MAX_RESULTS], query)


def search_perplexity_via_openrouter(client, cfg, query, period, *, model):
    from radar.web.discovery import call_json

    date_filter = ""
    if period in {"fortnight", "month"}:
        days = 14 if period == "fortnight" else 30
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        date_filter = f"\nПубликации с {cutoff.isoformat()} включительно."
    elif period != "all":
        raise ValueError("Неизвестный период поиска Perplexity")
    data = call_json(
        client,
        "https://openrouter.ai/api/v1/chat/completions",
        {"Authorization": "Bearer " + cfg["OPENROUTER_API_KEY"]},
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Найди русскоязычные источники по поисковому запросу пользователя. "
                        "Ответь кратко со ссылками на найденные страницы."
                    ),
                },
                {"role": "user", "content": query + date_filter},
            ],
            "max_tokens": 80,
            "web_search_options": {"search_context_size": "low"},
        },
        "Perplexity через OpenRouter",
        timeout=20.0,
    )
    return _search_documents(_openrouter_results(data), query)


def _openrouter_results(data):
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Perplexity через OpenRouter вернул ответ в неожиданном формате")
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(choice, dict) and (choice.get("error") or choice.get("finish_reason") == "error"):
        raise ValueError("Perplexity через OpenRouter не завершил поиск")

    results, citations = data.get("search_results"), data.get("citations")
    if not isinstance(message, dict) and not any(
        isinstance(rows, list) for rows in (results, citations)
    ):
        raise ValueError("Perplexity через OpenRouter вернул ответ в неожиданном формате")

    # OpenRouter normalizes source citations into annotations. The answer text is model output.
    annotations = message.get("annotations") if isinstance(message, dict) else None
    if isinstance(annotations, list):
        for annotation in annotations[:MAX_RESULTS]:
            if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                continue
            source = annotation.get("url_citation")
            if isinstance(source, dict):
                yield {
                    "url": source.get("url"), "title": source.get("title"),
                    "snippet": source.get("content"),
                }

    # Some compatible responses expose the provider's search records or URL-only citations.
    if isinstance(results, list):
        yield from results[:MAX_RESULTS]
    if isinstance(citations, list):
        yield from ({"url": url} for url in citations[:MAX_RESULTS])


def _search_documents(rows, query):
    from radar.web.discovery import SearchDocument

    documents = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        try:
            url = canonical_url(row["url"])
            title = row.get("title")
            text = row.get("snippet") or row.get("description")
            doc = SearchDocument(
                url=url,
                title=title[:1000] if isinstance(title, str) else "",
                text=text[:16000] if isinstance(text, str) else "",
                query=query,
            )
        except (ValueError, UnicodeError):
            continue
        documents.setdefault(url, doc.model_dump(mode="json"))
        if len(documents) == MAX_RESULTS:
            break
    return list(documents.values())
