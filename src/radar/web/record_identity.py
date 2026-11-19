"""Conservative, transaction-local resolution of new findings to existing records."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_BUILDING_PREFIX = r"\b(?:корпус|корп|дом|строение|стр)(?:\.\s*|\s+)(?:№|no|#)?\s*"
_BUILDING_NUMBER = r"\d+(?:[./-]\d+)*[а-яa-z]?|[ivxlcdm]{1,5}"
_BUILDING_END = r"(?!\w|[./-]\d)"
_BUILDING_REFERENCE = re.compile(_BUILDING_PREFIX + f"({_BUILDING_NUMBER})" + _BUILDING_END)
_BUILDING_LABEL = re.compile(_BUILDING_PREFIX + f"({_BUILDING_NUMBER}|[а-яa-z])" + _BUILDING_END)
_PHASE = re.compile(r"\b(v|iv|iii|ii|i|\d+)\s+(?:я\s+)?очеред\w*")
_REGIONAL_SCOPE = re.compile(r"\b(?:област\w*|край|республик\w*|федерац\w*|подмосковье)\b")


def _fold(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")


def _normal(value):
    return re.sub(r"[^\w]+", " ", _fold(value)).strip()


def _name(value):
    text = re.sub(r"^(?:жк|жилой комплекс)\s+", "", _normal(value))
    return re.sub(r"\s+(?:жилые кварталы|жилой комплекс)$", "", text)


def _known(value):
    return bool(value and value not in {"не установлен", "не указан", "неизвестно", "россия"})


def _developer(value):
    return re.sub(
        r"^(?:(?:гк|группа компаний|концерн|ооо|ао|пао|зао|оао|сз)\s+)+", "", _normal(value)
    )


def _developers(item):
    values = [item.get(k) for k in ("developer", "developer_brand", "developer_legal")]
    values.extend(item.get("developer_aliases") or [])
    return {v for value in values if _known(v := _developer(value))}


def project_name(value):
    """Normalize the title of a project while ignoring a trailing building or phase label."""
    raw = _fold(value)
    for match in _BUILDING_LABEL.finditer(raw):
        if not _normal(raw[match.end():]):
            raw = raw[:match.start()]
            break
    name = _name(raw)
    for match in _PHASE.finditer(name):
        if match.end() == len(name):
            return name[:match.start()].rstrip()
    return name


def _project_names(item):
    values = [item.get("name"), item.get("subject"), *(item.get("aliases") or [])]
    return {name for value in values if (name := project_name(value))}


def _project_locality(item):
    for key in ("project_locality", "locality", "city", "region"):
        for part in reversed(str(item.get(key) or "").split(",")):
            locality = _normal(part)
            if _known(locality) and not _REGIONAL_SCOPE.search(locality):
                return locality
    return None


def _project_anchor(item):
    # A curated project identifier is separate from a physical house identifier.
    key = item.get("project_identity")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _scope(item, *, incoming=False):
    scope = item.get("report_scope") or {}
    labels = [item.get("name"), item.get("building"), item.get("phase"), scope.get("phase")]
    quote = _fold(item.get("quote")) if incoming else ""
    phase_text = " ".join(_normal(value) for value in [*labels, quote] if value)
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
    phases = {roman.get(match[1], match[1]) for match in _PHASE.finditer(phase_text)}
    name = _fold(item.get("name"))
    buildings = set(_BUILDING_REFERENCE.findall(name))
    for match in _BUILDING_LABEL.finditer(name):
        if not _normal(name[match.end():]):
            buildings.add(match[1])
    for declared in (item.get("building"), scope.get("building")):
        field = _fold(declared).strip()
        if re.fullmatch(_BUILDING_NUMBER + r"|[а-яa-z]", field):
            buildings.add(field)
        else:
            buildings.update(_BUILDING_LABEL.findall(field))
    if incoming:
        buildings.update(_BUILDING_REFERENCE.findall(quote))
    stored = item.get("identity_scope") or {}
    phases.update(stored.get("phases") or [])
    # Old records also stored a truncated `3` derived from `корпус 3.1`.
    legacy_prefixes = {
        re.split(r"[./-]", code, maxsplit=1)[0]
        for code in buildings
        if re.search(r"[./-]", code)
    }
    buildings.update(code for code in stored.get("buildings") or [] if code not in legacy_prefixes)
    return phases, buildings


def identity_scope(item):
    phases, buildings = _scope(item, incoming=True)
    return {"phases": sorted(phases), "buildings": sorted(buildings)}


def unresolved_id(base, incoming, source_url):
    """An ambiguous publication remains a stable separate lead, never a guessed house."""
    value = [base, source_url, identity_scope(incoming), sorted(_developers(incoming))]
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]
    return ("demo-web-" if base.startswith("demo-web-") else "web-") + digest


def eis_url_id(value):
    """Only an exact official object-card/API URL is an identity, never an article number."""
    try:
        url = urlsplit(str(value or ""))
        host = (url.hostname or "").encode("idna").decode().removeprefix("www.")
    except (ValueError, UnicodeError):
        return None
    if url.scheme not in {"http", "https"} or host != "xn--80az8a.xn--d1aqf.xn--p1ai":
        return None
    match = re.search(r"/(?:объект|object)/(\d{1,12})/?$", unquote(url.path), re.I)
    return match[1] if match else None


@dataclass(frozen=True)
class Resolution:
    record_id: str | None = None
    reason: str = "new_identity"
    candidates: tuple[str, ...] = ()


class RecordResolver:
    """Read an existing transaction; never mutate records or merge their user data."""

    def __init__(self, db, dataset="web"):
        self.rows = {}
        for row in db.execute("SELECT id,dataset,payload FROM records"):
            if (dataset == "demo") != (row["dataset"] == "demo"):
                continue
            self.rows[row["id"]] = {**json.loads(row["payload"]), "id": row["id"]}
        self.bound = {}
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='registry_bindings'"
        ).fetchone():
            for row in db.execute(
                "SELECT record_id,house_id FROM registry_bindings WHERE confirmed=1"
            ):
                self.bound.setdefault(row["record_id"], set()).add(str(row["house_id"]))

    def add(self, ident, payload):
        self.rows[ident] = {**payload, "id": ident}

    def canonical(self, ident):
        seen = set()
        while ident in self.rows and ident not in seen:
            seen.add(ident)
            parent = self.rows[ident].get("merged_into")
            if not parent:
                return ident
            ident = parent
        return None

    def houses(self, item, source_url=None):
        result = set(self.bound.get(item.get("id"), set()))
        binding = (item.get("registry") or {}).get("binding") or {}
        if binding.get("confirmed") and binding.get("house_id"):
            result.add(str(binding["house_id"]))
        if result or item.get("granularity") == "project":
            return result
        for url in [source_url, item.get("official_card_url"), *(item.get("urls") or [])]:
            if ident := eis_url_id(url):
                result.add(ident)
        return result

    def compatible(self, incoming, target, *, strong=False, merged_name=False):
        incoming_houses = self.houses(incoming)
        target_houses = self.houses(target)
        incoming_hints = incoming_houses | set(incoming.get("eisgs_id_hints") or [])
        target_hints = target_houses | set(target.get("eisgs_id_hints") or [])
        if incoming_hints and target_hints and not incoming_hints & target_hints:
            return False
        a_phase, a_building = _scope(incoming, incoming=True)
        b_phase, b_building = _scope(target)
        if target.get("granularity") == "project" and (a_phase or a_building or incoming_hints):
            return False
        if (a_phase and b_phase and a_phase != b_phase) or (
            a_building and b_building and a_building != b_building
        ):
            return False
        if strong:
            return True
        regions = [_normal(item.get("region")) for item in (incoming, target)]
        if not all(_known(region) for region in regions) or regions[0] != regions[1]:
            return False
        a_developer, b_developer = _developers(incoming), _developers(target)
        if a_developer and b_developer and not a_developer & b_developer:
            return False
        if not merged_name and (
            (bool(a_phase) != bool(b_phase)) or (bool(a_building) != bool(b_building))
        ):
            return False
        # A generic complex name is not proof of one physical house/phase.
        if target_hints and not (a_phase or a_building or merged_name):
            return False
        return True

    def resolve_project(self, incoming, *, exclude=()):
        """Find a single proven parent project without treating its buildings as duplicates."""
        names, place = _project_names(incoming), _project_locality(incoming)
        developers, anchor = _developers(incoming), _project_anchor(incoming)
        skipped, confirmed, possible = set(exclude), [], []
        for ident, item in self.rows.items():
            if ident in skipped or ident == incoming.get("id") or self.canonical(ident) != ident:
                continue
            if item.get("granularity") not in {None, "complex", "project"} or any(_scope(item)):
                continue
            if self.houses(item) or item.get("eisgs_id_hints") or (
                item.get("report_scope") or {}
            ).get("house_id"):
                continue
            other_anchor = _project_anchor(item)
            if anchor and other_anchor and anchor != other_anchor:
                continue
            same_anchor = bool(anchor and anchor == other_anchor)
            if not same_anchor and not names & _project_names(item):
                continue
            other_place, other_developers = _project_locality(item), _developers(item)
            if (place and other_place and place != other_place) or (
                developers and other_developers and not developers & other_developers
            ):
                continue
            possible.append(ident)
            if same_anchor or (place and place == other_place and developers & other_developers):
                confirmed.append((ident, "verified_project" if same_anchor else "project_identity"))
        anchored = [ident for ident, reason in confirmed if reason == "verified_project"]
        if len(anchored) == 1:
            return Resolution(anchored[0], "verified_project")
        if len(confirmed) == 1:
            return Resolution(*confirmed[0])
        return Resolution(None, "ambiguous_project", tuple(sorted(possible)))

    def resolve(self, incoming, source_url=None, *, origin_record_id=None, fallback_id=None):
        if origin_record_id in self.rows:
            if canonical := self.canonical(origin_record_id):
                return Resolution(canonical, "stored_origin")
        candidate = {**incoming, "urls": [*(incoming.get("urls") or []), source_url]}
        houses = self.houses(candidate)
        strong, possible = set(), set()
        for ident, item in self.rows.items():
            canonical = self.canonical(ident)
            item_houses = self.houses(item)
            if canonical and houses & item_houses:
                possible.add(canonical)
                if (
                    len(houses) == len(item_houses) == 1
                    and self.compatible(candidate, self.rows[canonical], strong=True)
                ):
                    strong.add(canonical)
        if len(strong) == 1:
            return Resolution(next(iter(strong)), "confirmed_house")
        if possible:
            return Resolution(None, "ambiguous_or_conflicting_house", tuple(sorted(possible)))
        # Preserve the previous deterministic identity on exact-scope replays,
        # including unknown-region keys that already contain the source URL.
        canonical = self.canonical(fallback_id)
        if canonical:
            target = self.rows[canonical]
            a_developer, b_developer = _developers(incoming), _developers(target)
            candidate = {**incoming, "urls": [*(incoming.get("urls") or []), source_url]}
            if (
                _scope(incoming, incoming=True) == _scope(target)
                and not (a_developer and b_developer and not a_developer & b_developer)
                and self.compatible(candidate, target, strong=True)
            ):
                return Resolution(canonical, "stable_existing_key")
        name = _name(incoming.get("name"))
        matched = set()
        for ident, item in self.rows.items():
            names = {_name(item.get("name")), _name(item.get("subject"))}
            names.update(_name(v) for v in item.get("aliases") or [])
            if not name or name not in names:
                continue
            canonical = self.canonical(ident)
            if not canonical:
                continue
            target = self.rows[canonical]
            # Region-incompatible names are distinct projects, not duplicate candidates.
            region, target_region = _normal(incoming.get("region")), _normal(target.get("region"))
            if _known(region) and _known(target_region) and region != target_region:
                continue
            possible.add(canonical)
            merged_name = bool(item.get("merged_into") and name == _name(item.get("name")))
            candidate = {**incoming, "urls": [*(incoming.get("urls") or []), source_url]}
            if self.compatible(candidate, target, merged_name=merged_name):
                matched.add(canonical)
        if len(matched) == 1 and len(possible) == 1:
            return Resolution(next(iter(matched)), "explicit_stored_alias")
        return Resolution(None, "ambiguous_or_conflicting_alias", tuple(sorted(possible)))
