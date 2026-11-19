"""Match legal claim terms without mistaking ordinary chronology for proceedings."""

import re
from urllib.parse import urlsplit

_INVESTIGATION = re.compile(r"\bследств[а-яё]*\b", re.I)
_INVESTIGATION_COMMITTEE = re.compile(
    r"\bследственн(?:ый|ого|ому|ым|ом)\s+комитет(?:а|у|ом|е)?\b", re.I
)
_COURT = re.compile(
    r"\bсуд(?:[аеуы]|ом|ов|ам|ами|ах|ебн[а-яё]*|им[а-яё]*|ил[а-яё]*|ит[а-яё]*|"
    r"опроизводств[а-яё]*|ья|ьи|ье|ью|ей|ьям|ьями|ьях)?\b", re.I
)


def serious_terms(text, terms):
    text = str(text or "").casefold()
    return {
        term for term in terms
        if (_INVESTIGATION.search(text) if term == "следств" else
            _COURT.search(text) if term == "суд" else term in text)
    }


_AUTHORITY_NAMES = {
    "prosecution": re.compile(r"\bпрокуратур\w*|\bпрокурор\w*", re.I),
    "investigation": re.compile(r"\bследственн\w*\s+комитет\w*|\bСК\b", re.I),
    "building": re.compile(r"\b(?:мосгос|гос)?стройнадзор\w*", re.I),
    "court": _COURT,
    "police": re.compile(r"\bполици\w*|\bМВД\b", re.I),
    "housing": re.compile(r"\b(?:гос)?жилинспекци\w*", re.I),
    "consumer": re.compile(r"\bроспотребнадзор\w*", re.I),
}
_AUTHORITY_GENERIC = re.compile(
    r"\b(?:ведомств\w*|надзор(?:а|у|ом|е)?|надзорн\w+\s+орган\w*|"
    r"государственн\w+\s+орган\w*|орган\w*\s+власт\w*|власт(?:и|ям|ями))\b",
    re.I,
)
_COMPLAINT = re.compile(r"\b(?:жалоб|обращен|заявлен)\w*", re.I)
_FILED = re.compile(
    r"\b(?:(?:обратил|обращал)(?:ась|ись|ось|ся)|"
    r"направил[аио]?|направлен[аыо]?|подал[аио]?|подан[аыо]?|"
    r"отправил[аио]?|отправлен[аыо]?)\b", re.I
)
_RECEIVED = re.compile(
    r"\b(?:получа(?:ет|ют)|получил[аио]?|получен[аыо]?|поступа(?:ет|ют)|"
    r"поступил[аио]?|зарегистрировал[аио]?|зарегистрирован[аыо]?|принял[аио]?|принят[аыо]?)\b",
    re.I,
)
_RECEIVED_OTHER = re.compile(
    r"\b(?:получа\w*|получил\w*)\s+(?:позиц|ответ|комментар|материал|справк)\w*", re.I
)
_ACTION = re.compile(
    r"\b(?:начат[аоы]?|начал[аио]?|провод(?:ит|ят|ится)|назначен[аоы]?|назначил[аио]?|"
    r"возбудил[аио]?|возбужд[её]н[аоы]?|рассмотрел[аио]?|рассмотрен[аоы]?)\b"
    r".{0,45}\b(?:проверк|дел|обращен|жалоб)\w*|\bвзя(?:т[аоы]?|л[аи]?)\b.{0,25}"
    r"\b(?:под|на)\s+контрол\w*|\b(?:на\s+контроле\s+у\s+ведомств|"
    r"расследу(?:ет|ют|ется)|проверя(?:ет|ют|ется))\b", re.I
)
_POSITION = re.compile(r"\b(?:позиц|ответ|комментар|разъяснен)\w*", re.I)
_GAP = re.compile(
    r"\b(?:отсутств\w*|не\s+(?:представ[ил]|предостав[ил]|направ[ил]|опубликов|получ|"
    r"поступ|сообщ|был|дал[аио]?\b)\w*|нет\b|без\s+ответа\b)", re.I
)
_AUTHORITY_CHANNEL = re.compile(
    r"\b(?:в\s+(?:этих|тех\s+же)\s+каналах|в\s+их\s+адрес|им\s+не|"
    r"перед\s+ними|туда)\b", re.I
)
_DEVELOPER_RECIPIENT = re.compile(
    r"\b(?:застройщику|девелоперу|(?:в\s+адрес|к)\s+(?:застройщик\w*|девелопер\w*|"
    r"компани\w*)|в\s+офис\s+продаж)\b", re.I
)
_NEGATED = re.compile(r"\bне\s+(?:(?:был[аио]?|были)\s+)?$", re.I)
_REPORTED = re.compile(
    r"\b(?:(?:на|в)\s+форуме\s+(?:сообщ|напис|опубликов|пересказ)\w*|"
    r"(?:по\s+(?:словам|сообщению|данным|версии)|со\s+слов)\s+"
    r"(?:дольщик\w*|жильц\w*|собственник\w*|покупател\w*|форум\w*|"
    r"пользовател\w*|сми\b|издани\w*|публикаци\w*)|"
    r"(?:дольщик\w*|жильц\w*|собственник\w*|покупател\w*|форум\w*|"
    r"сми\b|издани\w*|readovka\b|публикаци\w*)\s+"
    r"(?:сообщ|рассказ|напис|утвержда|заявля|пересказыва)\w*)", re.I
)
_CHANNEL_ATTRIBUTION = re.compile(
    r"\b(?:(?:на\s+форуме|в\s+(?:форуме|чат\w*|телеграм[- ]канал\w*|соцсет\w*))"
    r"(?:\s+«?[\w-]+(?:\.[\w-]+)+»?)?"
    r"(?:[^,;:.!?]|(?:(?<=\d\s)|(?<=\d))г\.(?=\s+(?-i:[а-яё]))){0,40}"
    r"\b(?:сообщ|напис|пиш|указ|рассказ|пересказ|упомян)\w*|"
    r"в\s+(?:посте|сообщении|пересказе)\s+(?:на\s+форуме|в\s+чат\w*)|"
    r"по\s+(?:словам|сообщению|данным|пересказу)\s+(?:форум\w*|чат\w*|репост\w*))\b",
    re.I,
)
_DOMAIN_ATTRIBUTION = re.compile(
    r"\b(?:по\s+(?:сообщению|данным|пересказу)|согласно)\s+«?[\w-]+(?:\.[\w-]+)+\b", re.I
)
_NEUTRAL_REPORT_HEADING = re.compile(r"^\s*(?:сообщения|сведения)\s+(?:о|об)\s", re.I)
_FIRST_PERSON = re.compile(r"\b(?:мы|я)\b", re.I)
_REPORTED_FILING = re.compile(
    r"\b(?!сообщени\w*\b)"
    r"(?:сообщ|напис|пиш|указ|рассказ|пересказ|упомян)\w*\s+"
    r"(?:о|об|про)\s+(?:коллективн\w*\s+)?(?:обращени|жалоб)\w*", re.I
)
_FILING_ROLES = {
    "initiative": re.compile(r"\bинициативн\w+\s+групп\w*\b", re.I),
    "representative": re.compile(
        r"\bпредставител(?:ь|я|и|ей|ем|ями)\s+"
        r"(?:дольщик\w*|собственник\w*|жильц\w*|покупател\w*|инициативн\w+\s+групп\w*)",
        re.I,
    ),
}
_ROLE_BREAK = re.compile(
    r"[,;:.!?]|\b(?:что|чтобы|ищ\w*|искать|най\w*|планир\w*|отдельно|при\s+участии)\b",
    re.I,
)
_PROJECTED = re.compile(r"^\s*(?:если\b|в\s+случае\b|при\s+поступлении\b)", re.I)
_CLAUSE_BREAK = re.compile(
    r"(?<=[.!?])(?:(?<!\d\sг\.)(?<!\dг\.)\s+|\s+(?=(?-i:[А-ЯЁA-Z«])))"
    r"|[;\n]+|\s+[—–]\s+|,\s+(?=(?:но|однако|а|при этом)\s)", re.I
)
_KINDS = {
    "filed": "подаче обращения в ведомство",
    "received": "получении или регистрации обращения ведомством",
    "action": "действии ведомства",
    "position_gap": "непредставлении позиции или ответа в ведомстве",
}
_PRECEDES_PAST_COMPLAINT = re.compile(
    r"\b(?:до(?:\s+или\s+одновременно\s+с)?\s+жалоб\w*|"
    r"раньше\s*,?\s*чем\s+из\s+жалоб\w*)\b", re.I
)
_PRECEDES_AGENCY_REVIEW = re.compile(
    r"\b(?:до|к\s+началу)\s+(?:начала\s+)?(?:рассмотрени|регистраци|проверк)\w*\s+"
    r"(?:(?:эт\w+|нов\w+|поданн\w+|коллективн\w+)\s+)?(?:жалоб|обращени)\w*\b",
    re.I,
)
_OUTCOME_DELIVERY = re.compile(
    r"\b(?:получ(?:ат|ает|ают|ит|ение)|переда(?:ст|дим|дут|ча)|"
    r"поступ(?:ит|ят|ает)|направ(?:им|ят|ляется|ление))\b", re.I
)
_BEFORE_UNSPECIFIED_FILING = re.compile(
    r"\bдо\s+(?:отправк|подач|направлени)\w*\s+в\s+"
    r"(?:надзор|ведомств|прокуратур)\w*\b", re.I,
)
_NO_PROOF, _NAME_CHANNEL, _SUPPORTED = range(3)
_FORUM_REPORT_ACTORS = {
    "собственники": "собственников",
    "дольщики": "дольщиков",
    "покупатели": "покупателей",
    "жильцы": "жильцов",
}
_FORUM_REPORT_VOICE = re.compile(
    r"\b(?P<forum>на\s+форуме\s+(?P<host>[\w-]+(?:\.[\w-]+)+)\s+)"
    rf"(?P<actor>{'|'.join(_FORUM_REPORT_ACTORS)})\s+"
    r"(?P<report>сообщили\s+(?:о|об)\s+(?:коллективн\w*\s+)?обращени(?:и|ях))(?=\s+в\s)",
    re.I,
)


def _recipients(text):
    return {name for name, pattern in _AUTHORITY_NAMES.items() if pattern.search(text)}


def _agency_context(text):
    return bool(_recipients(text) or _AUTHORITY_GENERIC.search(text))


def _affirmed(pattern, text):
    return any(not _NEGATED.search(text[:match.start()]) for match in pattern.finditer(text))


def _authority_claims(text, context):
    direct = _agency_context(text)
    if not direct and (not context or _DEVELOPER_RECIPIENT.search(text)):
        return set()
    has_complaint = bool(_COMPLAINT.search(text))
    claims = set()
    is_filing = _affirmed(_FILED, text) and (
        has_complaint or direct and re.search(r"\bобра(?:тил|щал)\w*", text, re.I)
    )
    if is_filing or direct and _affirmed(_REPORTED_FILING, text):
        claims.add("filed")
    if direct and _affirmed(_RECEIVED, text) and has_complaint and not _RECEIVED_OTHER.search(text):
        claims.add("received")
    if direct and _affirmed(_ACTION, text):
        claims.add("action")
    if (direct or _AUTHORITY_CHANNEL.search(text)) and _POSITION.search(text) and _GAP.search(text):
        claims.add("position_gap")
    return claims


def without_reported_filing_authority_names(text):
    """Mask the committee name only where it is the recipient of an attributed filing."""
    clauses = re.split(f"({_CLAUSE_BREAK.pattern})", str(text or ""), flags=_CLAUSE_BREAK.flags)
    for index in range(0, len(clauses), 2):
        clause = clauses[index]
        if (
            _INVESTIGATION_COMMITTEE.search(clause)
            and _CHANNEL_ATTRIBUTION.search(clause)
            and _authority_claims(clause, False) == {"filed"}
        ):
            clauses[index] = _INVESTIGATION_COMMITTEE.sub(
                lambda match: " " * len(match[0]), clause
            )
    return "".join(clauses)


def is_attributed_filing(text):
    """Identify explicit attribution; the caller still verifies its cited evidence."""
    return any(
        (_CHANNEL_ATTRIBUTION.search(clause) or _DOMAIN_ATTRIBUTION.search(clause))
        and not _PROJECTED.search(clause)
        and "filed" in _authority_claims(clause, False)
        for clause in _CLAUSE_BREAK.split(str(text or ""))
    )


def asserts_filing_heading(text):
    """Recognize an unqualified filing assertion in a heading with authority context."""
    text = str(text or "")
    if _NEUTRAL_REPORT_HEADING.search(text) or is_attributed_filing(text):
        return False
    return any(
        not _PROJECTED.search(clause) and "filed" in _authority_claims(clause, True)
        for clause in _CLAUSE_BREAK.split(text)
    )


def _filing_roles(text):
    """Recognize explicit actor roles tied to the filing, including nominal phrasing."""
    verbs = list(_FILED.finditer(text))
    reports = list(_REPORTED_FILING.finditer(text))

    def linked(actor, verb):
        gap = text[min(actor.end(), verb.end()):max(actor.start(), verb.start())]
        return len(gap) <= 40 and not _ROLE_BREAK.search(gap)

    return {
        role
        for role, pattern in _FILING_ROLES.items()
        for actor in pattern.finditer(text)
        if any(linked(actor, verb) for verb in verbs) or any(
            actor.start() >= report.end() and not text[report.end():actor.start()].strip()
            for report in reports
        )
    }


def _matched_claim_clauses(claim, recipients, actors, atom):
    for sentence in _CLAUSE_BREAK.split(atom.get("verbatim") or ""):
        if claim in _authority_claims(sentence, False) and recipients <= _recipients(sentence):
            if not actors or actors <= _filing_roles(sentence):
                yield sentence


def _first_person_filing(sentence):
    return not _REPORTED.search(sentence) and any(
        _FIRST_PERSON.search(sentence[:verb.start()]) for verb in _FILED.finditer(sentence)
    )


def _recorded_atoms(facts):
    if hasattr(facts, "model_dump"):
        facts = facts.model_dump()
    atoms = facts["facts"] if isinstance(facts, dict) else facts
    return [atom.model_dump() if hasattr(atom, "model_dump") else atom for atom in atoms]


def normalize_forum_report_voice(text, facts, sources, refs=None):
    """Attribute an owner filing report to the same forum that actually carried it."""
    if not _FORUM_REPORT_VOICE.search(text):
        return text
    allowed = set(refs) if refs is not None else None
    by_n = {source["n"]: source for source in sources}
    atoms = [atom for atom in _recorded_atoms(facts)
             if allowed is None or atom["source_n"] in allowed]
    clauses = re.split(f"({_CLAUSE_BREAK.pattern})", text, flags=_CLAUSE_BREAK.flags)
    for index in range(0, len(clauses), 2):
        clause = clauses[index]
        match = _FORUM_REPORT_VOICE.search(clause)
        if not match or _PROJECTED.search(clause) or _authority_claims(clause, False) != {"filed"}:
            continue
        host = match["host"].casefold().removeprefix("www.")
        actor = match["actor"].casefold()
        actor_subject = re.compile(rf"\b{actor}(?:\s+[а-яё-]+){{0,3}}\s*$", re.I)
        recipients = _recipients(clause)
        got_report, got_direct = False, False
        for atom in atoms:
            source = by_n.get(atom["source_n"], {})
            kinds = {source.get("kind"), source.get("source_kind")}
            source_host = urlsplit(source.get("url") or "").hostname or source.get("publisher", "")
            if host != str(source_host).casefold().removeprefix("www."):
                continue
            if kinds & {"snippet", "search_snippet"} or not (
                "repost" in kinds or source.get("contour") in {"reviews", "communities"}
            ):
                continue
            for sentence in _matched_claim_clauses("filed", recipients, set(), atom):
                if "repost" not in kinds and _first_person_filing(sentence):
                    got_direct = True
                if any(
                    actor_subject.search(sentence[:verb.start()])
                    and not _NEGATED.search(sentence[:verb.start()])
                    for verb in _FILED.finditer(sentence)
                ):
                    got_report = True
        if got_report and not got_direct:
            replacement = f'{match["forum"]}{match["report"]} {_FORUM_REPORT_ACTORS[actor]}'
            clauses[index] = clause[:match.start()] + replacement + clause[match.end():]
    return "".join(clauses)


def _filing_proof(claim, recipients, actors, atom, source, attribution):
    proof = _NO_PROOF
    source_kinds = {source.get("kind"), source.get("source_kind")}
    full_original = not source_kinds & {"snippet", "search_snippet", "repost"}
    dated = bool(source.get("evidence_date") or atom.get("date"))
    publisher = str(source.get("publisher") or "").casefold()
    explicit_channel = bool(
        _CHANNEL_ATTRIBUTION.search(attribution)
        or "." in publisher and re.search(
            r"\b(?:по\s+(?:сообщению|данным|пересказу)|согласно)\s+" + re.escape(publisher) + r"\b",
            attribution, re.I,
        )
    )
    attributed = explicit_channel or bool(_REPORTED.search(attribution))
    forum_or_repost = (
        source.get("contour") in {"reviews", "communities"} or "repost" in source_kinds
    )
    for sentence in _matched_claim_clauses(claim, recipients, actors, atom):
        if attributed:
            direct_account = dated and full_original and _first_person_filing(sentence)
            if claim == "filed" and forum_or_repost and not (explicit_channel or direct_account):
                proof = _NAME_CHANNEL
                continue
            return _SUPPORTED
        if (
            source.get("contour") in {"official", "media", "authorities"}
            and full_original
            and dated
            and not _REPORTED.search(sentence)
        ):
            return _SUPPORTED
    return proof


def _field_issues(text, *, path, refs, atoms, sources, context):
    issues = set()
    actor_issue = False
    channel_issue = False
    for sentence in _CLAUSE_BREAK.split(text):
        if _PROJECTED.search(sentence):
            continue
        recipients = _recipients(sentence)
        for claim in _authority_claims(sentence, context):
            actors = _filing_roles(sentence) if claim == "filed" else set()
            proof = max((
                _filing_proof(
                    claim, recipients, actors, atom, sources.get(atom["source_n"], {}), sentence
                ) for atom in atoms if refs is None or atom["source_n"] in refs
            ), default=_NO_PROOF)
            if proof == _NAME_CHANNEL:
                channel_issue = True
            elif proof == _NO_PROOF:
                if actors:
                    actor_issue = True
                else:
                    issues.add(claim)
    if not (issues or actor_issue or channel_issue):
        return []
    reasons = []
    if issues:
        targets = ", ".join(_KINDS[claim] for claim in sorted(issues))
        reasons.append(
            f"Утверждение о {targets} не подтверждено связанным фактом. "
            "Датированное СМИ или официальный источник должны подтверждать именно это действие. "
            "Если есть только сообщение дольщиков или пересказ, укажи, кто сообщил, "
            "и убери неподтверждённый статус, ответ или отсутствие позиции."
        )
    if actor_issue:
        reasons.append(
            "Связанный факт должен подтверждать, кто обратился в ведомство. "
            "Если источник называет только собственников или дольщиков, сохрани эту атрибуцию "
            "и не приписывай подачу обращения инициативной группе или её представителю."
        )
    if channel_issue:
        reasons.append(
            "Связанный факт — сообщение на форуме, в чате или пересказ, без прямого "
            "сообщения собственников. Назови канал в той же фразе, например "
            "«На форуме сообщили об обращении». Номер источника не заменяет атрибуцию."
        )
    return [{
        "path": path,
        "refs": sorted(refs or []),
        "kind": "authority_claim",
        "reason": " ".join(reasons),
    }]


def authority_claim_issues(blocks, facts, sources):
    """Find ungrounded authored assertions about legal filings or agency responses."""
    atoms = _recorded_atoms(facts)
    by_n = {source["n"]: source for source in sources}
    issues = []

    def visit(node, path):
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}.{index}")
        if not isinstance(node, dict):
            return
        context = any(_agency_context(value) for value in node.values() if isinstance(value, str))
        for key, value in node.items():
            if key in {"quotes", "stages", "exit"} or (
                path == "scenario" and key == "levels"
            ):
                continue
            field = f"{path}.{key}"
            if isinstance(value, str) and key not in {"url", "publisher", "date", "date_iso"}:
                refs = node.get(f"{key}_refs", node.get("refs"))
                if refs is None and "n" in node and "publisher" in node:
                    refs = [node["n"]]
                issues.extend(_field_issues(
                    value, path=field, refs=refs, atoms=atoms, sources=by_n, context=context,
                ))
            else:
                visit(value, field)
        pain, outcome = node.get("pain"), node.get("outcome")
        if not path.startswith("offer.modules.") or not isinstance(outcome, str):
            return
        refs = node.get("pain_refs") or []
        solution = node.get("solution") or ""
        if _BEFORE_UNSPECIFIED_FILING.search(solution) and any(
            atom["source_n"] in refs
            and any(
                "filed" in _authority_claims(clause, False) and not _PROJECTED.search(clause)
                for clause in _CLAUSE_BREAK.split(atom.get("verbatim") or "")
            )
            for atom in atoms
        ):
            issues.append({
                "path": f"{path}.solution", "refs": sorted(refs), "kind": "authority_claim",
                "reason": (
                    "Источник уже сообщает о подаче обращения. Убери двусмысленное «до "
                    "отправки в надзор». Срок работы привяжи к будущим повторным обращениям "
                    "или выдаче подготовленного ответа его заявителям."
                ),
            })
        before_known_complaint = (
            isinstance(pain, str) and _PRECEDES_PAST_COMPLAINT.search(outcome)
            and any(
                "filed" in _authority_claims(sentence, False) and not _PROJECTED.search(sentence)
                for sentence in _CLAUSE_BREAK.split(pain)
            )
        )
        before_agency_review = any(
            _agency_context(clause) and _OUTCOME_DELIVERY.search(clause)
            and _PRECEDES_AGENCY_REVIEW.search(clause) and not _PROJECTED.search(clause)
            for clause in _CLAUSE_BREAK.split(outcome)
        )
        if before_known_complaint or before_agency_review:
            reason = (
                "Ещё неизвестно, когда ведомство рассмотрит жалобу. Не обещай получение "
                "позиции к этому моменту. Укажи управляемый результат агентства: "
                "согласованную справку для возможных запросов."
                if before_agency_review else
                "Нельзя обещать передать позицию до обращений, о которых уже сообщалось. "
                "Укажи конечный результат работы агентства для будущих запросов ведомств."
            )
            issues.append({
                "path": f"{path}.outcome",
                "refs": sorted(refs),
                "kind": "authority_claim",
                "reason": reason,
            })

    for block, value in blocks.items():
        visit(value.model_dump() if hasattr(value, "model_dump") else value, block)
    return issues
