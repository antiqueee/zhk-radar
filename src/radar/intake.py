"""Read-only import of analyst workbooks into an unverified staging dataset."""

import hashlib
import json
import re
import unicodedata
import warnings
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from pydantic import Field

from radar.domain import StrictModel


class Candidate(StrictModel):
    id: str
    file: str
    sha256: str
    sheet: str
    row: int
    developer: str
    subject: str
    granularity: str
    claims: dict[str, str]
    cells: dict[str, str]
    urls: list[str]
    verification: str = "unverified_import"
    eligible_for_digest: bool = False
    # IDs are hints, not registry matches. Never create an object from a guessed ID.
    eisgs_id_hints: list[str] = Field(default_factory=list)


# Explicit layouts: source classifications remain source text, never radar scores.
LAYOUTS = {
    ("Группа", "Объект / корпус"): ("Группа", "Объект / корпус", "mixed"),
    ("Девелопер", "ЖК / корпуса"): ("Девелопер", "ЖК / корпуса", "mixed"),
    ("Компания", "Объект", "Почему в резерве"): ("Компания", "Объект", "mixed"),
    ("Группа/СЗ", "Тип учёта"): ("Группа/СЗ", "Группа/СЗ", "developer"),
    ("Группа / юрлицо", "Дом / корпус"): ("Группа / юрлицо", "ЖК", "mixed"),
    ("Девелопер", "Корпус / ID ЕИСЖС"): ("Девелопер", "ЖК", "mixed"),
}


def registry_hints(values: list[str]) -> list[str]:
    ids = set()
    for value in values:
        ids.update(re.findall(r"(?:\bID|ЕИСЖС)\s*(?:№\s*)?(\d{4,7})\b", value, re.I))
        # Require the registry host for extracting numeric URL suffixes.
        for url in re.findall(r"https?://[^\s]+", value):
            if re.match(r"https?://(?:наш\.дом\.рф|xn--80az8a\.xn--d1aqf\.xn--p1ai)/", url):
                match = re.search(r"(?:/|[-_])(\d{4,7})/?(?:[?#].*)?$", url)
                if match:
                    ids.add(match[1])
    return sorted(ids)


def candidates_from_rows(file: str, sha: str, sheet: str, rows: list[dict]) -> list[Candidate]:
    result = []
    headers = None
    layout = None
    for row in rows:
        current = {
            c["coordinate"]: str(c["value"])
            for c in row["cells"]
            if c["value"] is not None and not c["formula"]
        }
        if headers is None:
            labels = set(current.values())
            layout = next((v for keys, v in LAYOUTS.items() if set(keys) <= labels), None)
            if layout:
                headers = {re.sub(r"\d+$", "", k): v for k, v in current.items()}
            continue
        claims = {
            headers[re.sub(r"\d+$", "", coord)]: value
            for coord, value in current.items()
            if re.sub(r"\d+$", "", coord) in headers
        }
        developer, subject, granularity = layout
        if not claims.get(developer) or not claims.get(subject):
            continue
        urls = sorted(
            {
                u
                for c in row["cells"]
                for u in ([c["url"]] if c["url"] else [])
                if u.startswith(("https://", "http://"))
            }
            | {u for value in claims.values() for u in re.findall(r"https?://[^\s]+", value)}
        )
        identity = hashlib.sha256(f"{sha}\0{sheet}\0{row['row']}".encode()).hexdigest()
        result.append(
            Candidate(
                id=identity,
                file=file,
                sha256=sha,
                sheet=sheet,
                row=row["row"],
                developer=claims[developer],
                subject=claims[subject],
                granularity=granularity,
                claims=claims,
                cells=current,
                urls=urls,
                eisgs_id_hints=registry_hints([*claims.values(), *urls]),
            )
        )
    return result


def read_workbook(path: Path) -> dict:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    filename = unicodedata.normalize("NFC", path.name)
    sheets = []
    candidates = []
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        workbook = load_workbook(path, data_only=False)
        cached = load_workbook(path, data_only=True)
        try:
            for sheet in workbook:
                rows = []
                for cells in sheet:
                    values = []
                    for cell in cells:
                        if cell.value is None and cell.hyperlink is None:
                            continue
                        value = cell.value
                        if isinstance(value, (date, datetime)):
                            value = value.isoformat()
                        formula = value if cell.data_type == "f" else None
                        cached_value = (
                            cached[sheet.title][cell.coordinate].value if formula else None
                        )
                        if isinstance(cached_value, (date, datetime)):
                            cached_value = cached_value.isoformat()
                        values.append(
                            {
                                "coordinate": cell.coordinate,
                                "value": value,
                                "formula": formula,
                                "cached_value": cached_value,
                                "url": cell.hyperlink.target if cell.hyperlink else None,
                            }
                        )
                    if values:
                        rows.append({"row": cells[0].row, "cells": values})
                sheets.append({"name": sheet.title, "rows": rows})
                candidates.extend(candidates_from_rows(filename, sha, sheet.title, rows))
        finally:
            workbook.close()
            cached.close()
    return {
        "file": filename,
        "sha256": sha,
        "sheets": sheets,
        "reader_warnings": sorted({str(w.message) for w in captured}),
        "candidates": [c.model_dump() for c in candidates],
    }


def import_workbooks(paths: list[Path], output: Path) -> dict:
    # Re-import produces the same dataset. Content duplicates are read only once.
    sources = {}
    for path in paths:
        book = read_workbook(path)
        sources.setdefault(book["sha256"], book)
    books = sorted(sources.values(), key=lambda b: (b["file"], b["sha256"]))
    payload = {"schema_version": 1, "verification": "unverified_import", "workbooks": books}
    if output.resolve() in {p.resolve() for p in paths}:
        raise ValueError("Нельзя перезаписывать исходный файл")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    return {
        "workbooks": len(books),
        "sheets": sum(len(b["sheets"]) for b in books),
        "candidate_rows": sum(len(b["candidates"]) for b in books),
        "verified_signals": 0,
        "output": str(output),
    }
