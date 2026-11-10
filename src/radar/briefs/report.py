"""Deterministic, evidence-linked service design. No generated factual claims."""

# Each playbook: work sequence, tangible result, measurement, dependency.
import json
import re
from pathlib import Path

PLAYBOOKS = json.loads(Path(__file__).with_name("playbooks.json").read_text())


def build_report(brief, library):
    """Keep supplied facts distinct from proposed work and conditional products."""
    topics = set(brief.get("topics", []))
    hints = {
        "перенос_сроков": r"перенос|срыв.{0,15}срок|задержк.{0,15}(сдач|передач)",
        "выдача_ключей": r"выдач.{0,10}ключ|передач.{0,12}квартир",
        "протечки": r"протеч|затоплен",
        "качество_ук": r"управляющ.{0,10}компан|\bУК\b",
        "суд": r"\bсуд\b|судебн",
        "неустойка": r"неустойк",
        "разные_ответы": r"разн.{0,10}ответ|противоречив.{0,10}информац",
        "инициативная_группа": r"инициативн.{0,10}групп",
        "жалобы_в_органы": r"прокуратур|жалоб.{0,15}(орган|надзор)",
        "смена_собственника": r"смен.{0,5}собственник",
    }
    inferred = {
        topic
        for topic, pattern in hints.items()
        if re.search(pattern, brief.get("summary", ""), re.I)
    } - topics
    topics |= inferred
    chosen = list(brief.get("offers", []))
    codes = {o["code"] for o in chosen}
    covered = {t for o in chosen for t in o["pain_topics"] if t in topics}
    # Additional products are explicit hypotheses, not silently assigned conflict stages.
    ranked = sorted(library.offers, key=lambda o: (-len(topics & set(o.pain_topics)), o.code))
    for offer in ranked:
        if len(chosen) >= 3:
            break
        if brief.get("dataset") == "demo" and (
            brief.get("stage", 0) not in offer.stages
            or brief.get("situation", "сроки") not in offer.situation_types
        ):
            continue
        if offer.code not in codes and (topics.intersection(offer.pain_topics) - covered):
            chosen.append({**offer.model_dump(), "conditional": True})
            codes.add(offer.code)
            covered.update(topics.intersection(offer.pain_topics))
    explicit_building = re.search(r"(?:корпус|дом)\s+[\w./-]+", brief["name"], re.I)
    scope = brief["name"]
    if brief.get("building"):
        if not explicit_building or explicit_building[0] != brief["building"]:
            scope += " · " + brief["building"]
    elif not explicit_building:
        scope += " · корпус уточняется"
    products = []
    for offer in chosen:
        tasks, result, metric, dependency = PLAYBOOKS[offer["code"]]
        matching = sorted(topics.intersection(offer["pain_topics"]))
        signals = [
            s
            for s in brief.get("dossier", {}).get("signals", [])
            if set(s.get("topics", [])) & set(matching)
        ]
        chat_examples = [
            example
            for example in (brief.get("chat_stats") or {}).get("examples", [])
            if example["topic"] in matching
        ]
        trigger = (
            signals[0]["quote"]
            if signals
            else chat_examples[0]["text"]
            if chat_examples
            else brief.get("summary", "")
        )
        products.append(
            {
                **offer,
                "matching_topics": matching,
                "keyword_topics": sorted(inferred & set(matching)),
                "case_trigger": trigger,
                "case_origin": "Публикация"
                if signals
                else "Сообщение из чата"
                if chat_examples
                else "Исходное описание карточки",
                "tasks": tasks,
                "result": result,
                "measurement": metric,
                "dependency": dependency,
                "evidence_urls": list(dict.fromkeys(s["source_url"] for s in signals)),
                "scope": scope,
                "starting_question": brief.get("check", "Подтвердить факты и границы проекта"),
            }
        )
    return {"schema_version": 6, "products": products}
