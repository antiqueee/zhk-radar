"""A fully offline scan using the same pipeline; every source and object is synthetic."""

import hashlib

from radar.web.discovery import Findings, discover
from radar.web.sources import Page
from radar.web.store import now

EXAMPLES = [
    {
        "url": "https://demo.example/keys",
        "name": "ЖК Учебный берег",
        "title": "Учебная публикация о передаче квартир",
        "region": "Москва",
        "quote": (
            "10 ноября 2026 года Москва: жители ЖК Учебный берег, корпус 3, "
            "сообщили о переносе передачи квартир."
        ),
        "building": "корпус 3",
        "event_type": "handover_delay",
        "event_date": "2026-11-10",
        "topics": ["перенос_сроков", "выдача_ключей"],
    },
    {
        "url": "https://demo.example/answers",
        "name": "ЖК Учебный берег",
        "title": "Учебная публикация об ответах жителям",
        "region": "Москва",
        "quote": (
            "9 ноября 2026 года Москва: жители ЖК Учебный берег, корпус 3, "
            "сообщили о разных ответах сотрудников по срокам."
        ),
        "building": "корпус 3",
        "event_type": "complaint",
        "event_date": "2026-11-09",
        "topics": ["разные_ответы"],
    },
    {
        "url": "https://demo.example/building-four",
        "name": "ЖК Учебный берег",
        "title": "Учебная публикация о другом корпусе",
        "region": "Москва",
        "quote": (
            "10 ноября 2026 года Москва: в ЖК Учебный берег, корпус 4, "
            "жители сообщают о протечках после заселения."
        ),
        "building": "корпус 4",
        "event_type": "defects",
        "event_date": "2026-11-10",
        "topics": ["протечки"],
    },
]


def run_demo(store, request, progress, cancelled):
    pages = {item["url"]: item for item in EXAMPLES}
    documents = [
        dict(url=x["url"], title=x["title"], text=x["quote"], query="Учебный поиск")
        for x in EXAMPLES
    ]

    def reader(url):
        item = pages[url]
        text = item["quote"] + "\nЭто вымышленная публикация для проверки программы, не новость."
        return Page(
            url,
            url,
            "loaded",
            title=item["title"],
            text=text,
            fetched_at=now(),
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )

    def extract(batch):
        objects = []
        for i, doc in enumerate(batch):
            item = pages[doc["url"]]
            objects.append(
                {
                    key: item[key]
                    for key in [
                        "name",
                        "region",
                        "quote",
                        "building",
                        "event_type",
                        "event_date",
                        "topics",
                    ]
                }
                | {
                    "source_index": i,
                    "developer": "Учебный девелопер",
                    "summary": "В учебной публикации: " + item["quote"],
                }
            )
        return Findings.model_validate({"objects": objects})

    return discover(
        store,
        request,
        reader=reader,
        progress=progress,
        cancelled=cancelled,
        dataset="demo",
        prepared={"documents": documents, "extract": extract},
    )
