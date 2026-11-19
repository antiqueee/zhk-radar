"""Ground selected author assertions that can misrepresent the underlying evidence."""

import re

from radar.reports.fact_numbers import MONTH, date_literals, numeric_values
from radar.reports.factual_copy import (
    _PERCENT,
    _READINESS,
    _data,
    _fact_clauses,
    _facts,
    _named_scopes,
    _without_dates,
    validate_copy_numbers,
)
from radar.reports.serious_claims import _authority_claims, _filing_roles

_DDU = re.compile(r"\b(?:дду|договор\w*\s+долев\w*\s+участи\w*)\b", re.I)
_MULTIPLE_DDU = re.compile(
    r"\b(?:часть|нескольк\w*|дв(?:а|ух)|три|тр[её]х|четыр[её]\w*|[2-9]|[1-9]\d+)\s+"
    r"(?:дду|договор\w*\s+долев\w*\s+участи\w*)\b", re.I,
)
_BUYER_NOUN = r"(?:дольщик|собственник|покупател)\w*"
_MULTIPLE_BUYERS = re.compile(
    rf"\b(?:часть|групп(?:а|е|ы|у|ой)|нескольк\w*)\s+{_BUYER_NOUN}\b", re.I,
)
_PLURAL_CONTRACTS = re.compile(r"\bдоговор(?:ах|ы|ами|ов|ам)\b", re.I)
_CONTRACT_DATE_ANCHOR = re.compile(
    r"\b(?:договорн\w+\s+(?:срок\w*|дат(?:а|ы|е|ой|у))|дду|договор(?:ах|ы|ами|ов|ам))\b",
    re.I,
)
_SEPARATE_DEADLINE = re.compile(
    r"\b(?:нов\w+|опубликованн\w+|перенес[ёе]нн\w+|официальн\w+)\s+"
    r"(?:срок\w*|дат(?:а|ы|е|ой|у))\b", re.I,
)
_CONTRACT_DATE_LINK = re.compile(r"\b(?:срок\w*|стои\w*|стоял\w*)\b", re.I)
_CONTRACT_DATE_NEGATED = re.compile(r"\bне\s+(?:сто[ия]\w*|указан\w*|содержал\w*)\b", re.I)
_NEW_TERMS = re.compile(
    r"\b(?:(?:измен[её]нн\w*|нов\w*|скорректированн\w*)\s+услови\w*|"
    r"(?:дополнительн\w*\s+соглашени\w*|допсоглашени\w*))\b", re.I,
)
_TERMS_ACTION = {
    "ddu_received": re.compile(r"\b(?:получил[аио]?|получен[аыо]?)\b", re.I),
    "ddu_sent": re.compile(r"\b(?:разослал[аио]?|направил[аио]?|направлен[аыо]?)\b", re.I),
    "ddu_signed": re.compile(r"\b(?:подписал[аио]?|подписан[аыо]?)\b", re.I),
}
_BUYERS = re.compile(
    r"\b(?:дольщик(?:и|ов|ам|ами|ах)|собственник(?:и|ов|ам|ами|ах)|покупател[ие]\w*)\b", re.I,
)
_SAW_READINESS = re.compile(r"\b(?:видят|увидели|узнали|прочитали|обсуждают|обсудили)\b", re.I)
_REACTED = re.compile(
    r"\b(?:сомнева\w*|усомнил\w*|оспарива\w*|отреагирова\w*|реагиру\w*|не\s+вер\w*)\b", re.I,
)
_REACTION_BASIS = re.compile(r"\b(?:при|из-за|после|в\s+ответ\s+на|в\s+связи\s+с)\b", re.I)
_NOT_REACTION = re.compile(r"\bне\s+$", re.I)
_CONSTRUCTION_PACE = re.compile(
    r"\b(?:(низк\w*|медленн\w*|недостаточн\w*)\s+)?темп\w*\s+"
    r"(?:стройк\w*|строительств\w*|работ\w*)\b", re.I,
)
_CAUSAL_DOUBT = re.compile(
    r"\b(?:усилива(?:ет|ют)|усилил[аио]?|вызыва(?:ет|ют)|вызвал[аио]?)\s+"
    rf"(?:сомнени\w*(?:\s+{_BUYER_NOUN})?|{_BUYER_NOUN}\s+сомнени\w*)\b", re.I,
)
_DEVELOPER_CONTACT = re.compile(
    r"\b(обратились|обращались|обращаются)\s+(?:(?:напрямую|непосредственно)\s+)?"
    r"(?:к\s+(?:застройщик|девелопер)\w*|в\s+офис\s+продаж)\b", re.I,
)
_CONTACT_UNCERTAIN = re.compile(
    r"\b(?:если|обраща\w*\s+ли|уточним|проверим|выясним|не\s+обра(?:тил|ща)\w*)\b", re.I,
)
_OBSERVATION_SPLIT = re.compile(r"\s+[—–]\s+|;\s+")
_OBSERVATION_HEAD = re.compile(
    r"\b(?:ключ\w*|готовност\w*|объект\w*\s+готов)\b", re.I,
)
_OBSERVATION_TAILS = (
    re.compile(rf"\b{_BUYER_NOUN}\s+требуют\s+(?:объяснени|разъяснени)\w*\s+"
               r"(?:напрямую|непосредственно)\b", re.I),
    re.compile(rf"\b{_BUYER_NOUN}\s+обращаются\s+(?:напрямую\s+)?к\s+"
               r"(?:застройщик|девелопер)\w*(?:\s+напрямую)?\b", re.I),
    re.compile(r"\bсомнения(?:\s+дольщиков)?\s+в\s+(?:реалистичности|выполнимости)\s+"
               r"(?:нового\s+)?срока\s+(?:усиливаются|нарастают|растут)\b", re.I),
)
_READINESS_CAUSAL_TAIL = re.compile(
    r",\s+что\s+(?:усиливает|усилило)\s+сомнения\s+в\s+"
    r"(?:(?:новом\s+)?сроке|(?:реалистичности|выполнимости|соблюдении)\s+(?:нового\s+)?срока)"
    r"\.?$", re.I,
)
_READINESS_EARLIER_TAIL = re.compile(
    rf"(?<=\.)\s+ранее,\s+\d{{1,2}}\s+{MONTH}\s+20\d{{2}}(?:\s+года)?,\s+"
    rf"(?:люди|дольщики|покупатели)\s+(?:выражали|высказывали)\s+сомнения\s+в\s+"
    rf"соблюдении\s+(?:нового\s+)?срока"
    rf"(?:\s+[—–]\s+на\s+тот\s+момент\s+на\s+площадке\s+находился\s+только\s+фундамент)?\.?$",
    re.I,
)
_NEGATED_READINESS = re.compile(
    r"\bне\s+(?:достига\w*|достиг\w*|состав\w*)\b", re.I,
)
_INDIVIDUAL = re.compile(
    r"\b(?:один|одна|одного|отдельный|отдельная)\s+(?:из\s+)?(?:дольщик|собственник|покупател)\w*",
    re.I,
)
_TERMS_UNCERTAIN = re.compile(
    r"\b(?:получили\s+ли|направили\s+ли|подписали\s+ли|уточн\w*|провер\w*|"
    r"не\s+(?:(?:был[аио]?|были)\s+)?(?:получ\w*|направ\w*|подпис\w*))\b", re.I,
)
_GROUP = re.compile(
    r"\b(?:инициативн\w+\s+групп\w*|(?:избранн\w+|уполномоченн\w+)\s+"
    r"представител\w*\s+(?:дольщик|собственник)\w*)\b", re.I,
)
_GROUP_ACTUAL = re.compile(
    r"\b(?:сформирова(?:л[аио]?|н[аыо]?)|созда(?:л[аио]?|н[аыо]?)|избра(?:л[аио]?|н[аыо]?)|"
    r"назнач(?:ил[аио]?|ен[аыо]?)|действу(?:ет|ют)|существу(?:ет|ют)|"
    r"представля(?:ет|ют|л[аио]?)|сообщил[аио]?|направил[аио]?|выступил[аио]?|провел[аио]?|есть)\b",
    re.I,
)
_GROUP_CONTACT = re.compile(
    r"\b(?:(?:переговор\w*|встреч\w*)\s+с|(?:выход\w*|выйд\w*|выш\w*)\s+на)\s+"
    r"(?:инициативн\w+\s+групп\w*|(?:избранн\w+|уполномоченн\w+)\s+представител\w*)\b",
    re.I,
)
_GROUP_UNCERTAIN = re.compile(
    r"\b(?:если|при\s+наличии|в\s+случае|(?:при|после)\s+подтверждени\w*|"
    r"поиск\w*|найти|найд\w*|провер\w*\s+наличие|наличи\w*\s+и\s+состав|"
    r"не\s+(?:подтвержд[её]н\w*|сформирован\w*|создан\w*|избран\w*|существу\w*|действу\w*))\b",
    re.I,
)
_CONDITIONAL = re.compile(r"^\s*(?:если\b|в\s+случае\b|при\s+наличии\b)", re.I)
_INDEPENDENT_WORKS = re.compile(
    r"(?:,\s*(?:и\s+)?|\s+и\s+)(?=(?:в|на|по)\s+[^.;!?]{1,65}\s+"
    r"(?:вед[её]тся|ведутся|ид[её]т|идут|продолжа\w*|выполня\w*)\b)", re.I,
)
_COMMUNICATION_GAPS = {
    "channel_gap": re.compile(
        r"\b(?:не\s+име(?:ет|ют)|нет|отсутству(?:ет|ют))\s+един\w+\s+канал\w*\b", re.I,
    ),
    "negotiation_gap": re.compile(
        r"\bтребован\w*(?:\s+пока)?\s+не\s+перевед[её]н\w*\s+в\s+"
        r"(?:(?:предметн\w*|содержательн\w*)\s+)?(?:переговор\w*|диалог\w*)\b", re.I,
    ),
    "position_document_gap": re.compile(
        r"\bпозици\w*[^.;!?]{0,100}\bне\s+сведен\w*\s+в\s+един\w+\s+документ\w*\b", re.I,
    ),
    "reply_gap": re.compile(
        r"\b(?:требован\w*[^.;!?]{0,70}\b(?:оста[её]тся|остаются|остал\w*)\s+без\s+"
        r"(?:предметн\w+|содержательн\w+)\s+ответ\w*|"
        r"(?:(?:(?:един\w+|предметн\w+|содержательн\w+|публичн\w+)\s+){1,3}ответ\w*|"
        r"ответ\w*\s+по\s+существу)[^.;!?]{0,95}"
        r"\bне\s+(?:зафиксирован\w*|найден\w*|опубликован\w*))\b", re.I,
    ),
    "brief_gap": re.compile(
        r"\b(?:опережающ\w+\s+)?справк\w*\s+(?:для|в)\s+(?:ведомств\w*|надзор\w*)"
        r"[^.;!?]{0,70}\bне\s+(?:зафиксирован\w*|найден\w*|опубликован\w*|подготовлен\w*)\b",
        re.I,
    ),
    "authority_position_gap": re.compile(
        r"\bпозици\w*\s+(?:застройщик|девелопер)\w*\s+для\s+ответ\w*\s+на\s+"
        r"(?:(?:возможн|потенциальн)\w*\s+)?запрос\w*\s+"
        r"(?:ведомств|надзор|регулятор)\w*(?:\s+пока)?\s+не\s+подготовлен\w*\b", re.I,
    ),
}
_VERIFY_GAP = re.compile(r"\b(?:провер\w*|уточн\w*|выясн\w*|есть\s+ли)\b", re.I)
_GENERATED = {
    "name", "service", "service_ids", "method_ids", "move", "refs", "pain_refs",
    "url", "meta", "quotes", "stages", "exit", "risk_titles",
}
_REASONS = {
    "ddu": (
        "Нет связанного факта о получении или подписании изменённых условий ДДУ дольщиками. "
        "Публичный перенос срока и сообщение одного владельца не подтверждают изменение "
        "договоров у группы. Оставь опубликованный срок или точно атрибутируй отдельный случай."
    ),
    "ddu_deadline": (
        "Срок из одного договора нельзя приписывать части ДДУ. "
        "Укажи источник как сообщение одного дольщика или сохрани общий опубликованный срок."
    ),
    "filing_scope": (
        "Сообщение об обращении не связывает подавших его дольщиков с указанными корпусами "
        "или очередью. Убери номера из этого утверждения и сохрани атрибуцию обращения."
    ),
    "group": (
        "Связанный источник не подтверждает существующую инициативную группу или её "
        "представителей. Коллективное обращение этого не доказывает. Опиши поиск добровольных "
        "представителей и переговоры при их наличии."
    ),
    "channel_gap": (
        "Связанный источник не подтверждает отсутствие единого канала ответов. "
        "Убери утверждение об отсутствии и предложи согласовать один канал для вопросов."
    ),
    "negotiation_gap": (
        "Связанный источник не подтверждает, что требования остались без переговоров. "
        "Сохрани зафиксированные вопросы и опиши предложение организовать обсуждение."
    ),
    "position_document_gap": (
        "По источникам нельзя установить, сведена ли позиция застройщика в единый документ. "
        "Сохрани известные факты и предложи агентству подготовить справку."
    ),
    "reply_gap": (
        "Связанный источник не подтверждает отсутствие ответа на коллективные требования. "
        "Отдельный запрос не описывает всю переписку. Опиши, какой ответ подготовим."
    ),
    "buyer_readiness": (
        "Связанный источник не подтверждает, что дольщики увидели этот процент готовности "
        "или отреагировали на него. Укажи дату стройотчёта и отдельно передай более ранние "
        "сомнения с их собственной датой и атрибуцией."
    ),
    "construction_pace_doubt": (
        "Источник не подтверждает, что такой темп стройки вызвал или усилил сомнения. "
        "Оставь датированные сведения о ходе работ; сомнения передай отдельно со своим "
        "источником или опиши риск как условие."
    ),
    "developer_contact": (
        "Связанный источник не подтверждает, что дольщики обращаются к застройщику напрямую. "
        "Оставь сообщение о том, куда они обратились по источнику, или опиши предлагаемый канал."
    ),
    "brief_gap": (
        "Источники не показывают, подготовлена ли отдельная справка для ведомств. "
        "Опиши подготовку справки как работу агентства без заявления о её отсутствии."
    ),
    "authority_position_gap": (
        "Связанный источник не подтверждает, что позиция застройщика для ответа ведомствам "
        "не подготовлена. Сохрани атрибутированное сообщение об обращениях и предложи "
        "согласовать ответ на случай запроса."
    ),
}


def _confirmed_group(text):
    return bool(_GROUP.search(text) and not _GROUP_UNCERTAIN.search(text) and (
        _GROUP_ACTUAL.search(text) or _filing_roles(text)
    ))


def _collective_contract_dates(clause):
    """An original contractual date is independent of a later publicly announced deadline."""
    if _INDIVIDUAL.search(clause) or _CONTRACT_DATE_NEGATED.search(clause):
        return frozenset()
    if not (_MULTIPLE_DDU.search(clause) or _MULTIPLE_BUYERS.search(clause)
            or (_BUYERS.search(clause) and _PLURAL_CONTRACTS.search(clause))):
        return frozenset()
    dates = date_literals(clause)
    result = set()
    for anchor in _CONTRACT_DATE_ANCHOR.finditer(clause):
        for start, _, date in dates:
            if start >= anchor.end():
                prefix = clause[anchor.end():start]
                if (_CONTRACT_DATE_LINK.search(clause[anchor.start():start])
                        and not _SEPARATE_DEADLINE.search(prefix)):
                    result.add(date)
                break
    return frozenset(result)


def _buyer_readiness_claims(clause):
    """Require explicit reaction or awareness, not adjacent independent observations."""
    if not _READINESS.search(clause):
        return
    buyers = _BUYERS.search(clause)
    reacted = _REACTED.search(clause) if buyers else None
    for percent in _PERCENT.finditer(clause):
        if not _READINESS.search(clause[max(0, percent.start() - 35):percent.end() + 35]):
            continue
        before = clause[:percent.start()]
        saw = _SAW_READINESS.search(before)
        actions = set()
        if (saw and buyers and buyers.start() < saw.start()
                and not _NOT_REACTION.search(before[:saw.start()])
                and not re.search(r"[,;]", before[saw.end():])):
            actions.add("saw")
        basis = _REACTION_BASIS.search(re.split(r"[,;]", before)[-1][-55:])
        explanation = reacted and ":" in before[reacted.end():]
        if (reacted and not _NOT_REACTION.search(clause[:reacted.start()])
                and (actions or basis or explanation)):
            actions.add("reacted")
        causal_doubt = _CAUSAL_DOUBT.search(clause, percent.end())
        if causal_doubt and not _NOT_REACTION.search(clause[:causal_doubt.start()]):
            actions.add("reacted")
        if actions:
            for value in numeric_values(percent[1]):
                yield value, frozenset(actions)


def _readiness_percentages(text):
    return {
        value for percent in _PERCENT.finditer(text)
        if _READINESS.search(text[max(0, percent.start() - 55):percent.end() + 55])
        for value in numeric_values(percent[1])
    }


def _proven_readiness_observation(observation, tail, rows):
    if _CONDITIONAL.search(observation) or _NEGATED_READINESS.search(observation):
        return False
    dates = [date for _, _, date in date_literals(observation)]
    percentages = _readiness_percentages(observation)
    if (len(dates) != 1 or dates[0][1] in (None, "q") or not dates[0][2]
            or len(percentages) != 1
            or sum(bool(clause.strip()) for clause, _ in _fact_clauses(observation)) != 1):
        return False
    date = dates[0]
    scope = _named_scopes(_without_dates(observation))
    if tail.re is _READINESS_EARLIER_TAIL:
        tail_dates = [item for _, _, item in date_literals(tail[0])]
        if (len(tail_dates) != 1 or tail_dates[0][1] in (None, "q") or not tail_dates[0][2]
                or tail_dates[0] >= date):
            return False
    proven = False
    for row in rows:
        row_dates = {item for _, _, item in date_literals(row.get("date") or "")}
        for clause, masked in _fact_clauses(row.get("verbatim") or ""):
            if _named_scopes(masked) != scope:
                continue
            amounts = _readiness_percentages(clause)
            causal_doubt = _CAUSAL_DOUBT.search(clause)
            if percentages & amounts and (
                (causal_doubt and not _NOT_REACTION.search(clause[:causal_doubt.start()]))
                or any(value in percentages and "reacted" in actions
                       for value, actions in _buyer_readiness_claims(clause))
            ):
                return False
            if (row.get("kind") != "fact" or _NEGATED_READINESS.search(clause)
                    or amounts != percentages):
                continue
            if date not in row_dates and date not in {item for _, _, item in date_literals(clause)}:
                continue
            try:
                validate_copy_numbers(observation, [row])
            except ValueError:
                continue
            proven = True
    return proven


def ground_observation_copy(text, facts, refs=None, *, repair_readiness=False):
    """Drop only a detachable unsupported demand/contact or trend after a valid observation."""
    if repair_readiness:
        tail = _READINESS_CAUSAL_TAIL.search(text) or _READINESS_EARLIER_TAIL.search(text)
        if tail:
            observation = text[:tail.start()].rstrip()
            rows = [row for row in _facts(facts) if refs is None or row["source_n"] in refs]
            if _proven_readiness_observation(observation, tail, rows):
                return observation if observation.endswith(".") else observation + "."
            return text
    separators = list(_OBSERVATION_SPLIT.finditer(text))
    if not separators:
        return text
    separator = separators[-1]
    observation = text[:separator.start()].rstrip()
    tail = text[separator.end():].strip()
    period = "." if tail.endswith(".") else ""
    tail = tail.removesuffix(".")
    pattern = next((pattern for pattern in _OBSERVATION_TAILS if pattern.fullmatch(tail)), None)
    if (pattern is None or _CONDITIONAL.search(observation)
            or not _OBSERVATION_HEAD.search(observation) or not date_literals(observation)):
        return text
    rows = [row for row in _facts(facts) if refs is None or row["source_n"] in refs]
    scope = _named_scopes(_without_dates(observation))
    for row in rows:
        for clause, masked in _fact_clauses(row.get("verbatim") or ""):
            if (pattern.search(clause) and not _CONTACT_UNCERTAIN.search(clause)
                    and _named_scopes(masked) == scope):
                return text
    try:
        validate_copy_numbers(observation, rows, refs)
    except ValueError:
        return text
    return observation if observation.endswith((".", "!", "?")) else observation + period


def _claims(text, *, evidence):
    clauses = (
        part for clause, _ in _fact_clauses(text) for part in _INDEPENDENT_WORKS.split(clause)
    )
    for clause in clauses:
        if _CONDITIONAL.search(clause):
            continue
        scope = frozenset(_named_scopes(_without_dates(clause)))
        if (_DDU.search(clause) and _NEW_TERMS.search(clause) and _BUYERS.search(clause)
                and not (_INDIVIDUAL.search(clause) or _TERMS_UNCERTAIN.search(clause))):
            for status, pattern in _TERMS_ACTION.items():
                if pattern.search(clause):
                    yield status, scope, ()
        dates = _collective_contract_dates(clause)
        if dates:
            yield "ddu_deadline", scope, dates
        for claim in _buyer_readiness_claims(clause):
            yield "buyer_readiness", scope, claim
        pace = _CONSTRUCTION_PACE.search(clause)
        doubt = _CAUSAL_DOUBT.search(clause, pace.end()) if pace else None
        if doubt and not _NOT_REACTION.search(clause[:doubt.start()]):
            yield "construction_pace_doubt", scope, bool(pace[1])
        contact = _DEVELOPER_CONTACT.search(clause)
        if (_BUYERS.search(clause) and contact
                and not (_INDIVIDUAL.search(clause) or _CONTACT_UNCERTAIN.search(clause))):
            yield "developer_contact", scope, contact[1].casefold() == "обращаются"
        if scope and "filed" in _authority_claims(clause, False):
            yield "filing_scope", scope, ()
        if _confirmed_group(clause) or (
            not evidence and _GROUP.search(clause) and _GROUP_CONTACT.search(clause)
            and not _GROUP_UNCERTAIN.search(clause)
        ):
            yield "group", scope, ()
        if not _VERIFY_GAP.search(clause):
            for gap, pattern in _COMMUNICATION_GAPS.items():
                if pattern.search(clause):
                    yield gap, scope, ()


def editorial_claim_issues(blocks, facts):
    """Return precise author-owned fields with unproven contract, filing or group claims."""
    proof = [
        (row["source_n"], set(_claims(row.get("verbatim") or "", evidence=True)))
        for row in _facts(facts)
    ]
    issues = []

    def inspect(node, path, refs=None):
        if isinstance(node, list):
            for index, item in enumerate(node):
                inspect(item, f"{path}.{index}", refs)
        elif isinstance(node, dict):
            shared = node.get("refs", node.get("pain_refs", refs))
            for key, value in node.items():
                if key in _GENERATED or (path == "scenario" and key == "levels"):
                    continue
                inspect(value, f"{path}.{key}", node.get(f"{key}_refs", shared))
        elif isinstance(node, str):
            claims = set(_claims(node, evidence=False))
            if claims:
                supported = set().union(*(seen for n, seen in proof if refs is None or n in refs))
                broken = sorted({
                    "ddu" if kind in _TERMS_ACTION else kind
                    for kind, scope, dates in claims if (kind, scope, dates) not in supported
                })
                if broken:
                    issues.append({
                        "path": path, "refs": sorted(refs or []), "kind": "editorial_claim",
                        "reason": " ".join(_REASONS[kind] for kind in broken),
                    })

    for section, value in _data(blocks).items():
        inspect(_data(value), section)
    return issues
