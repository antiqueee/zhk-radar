"""User-supplied A4 layout, StrictUndefined and observable Chromium overflow."""

import hashlib
import json
import logging
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from playwright.sync_api import Error, sync_playwright

ROOT = Path(__file__).parent
MAX_V8_PAGES = 6
LOGGER = logging.getLogger(__name__)
PDF_PAGE = re.compile(rb"/Type\s*/Page\b")
PDF_TRAILER = re.compile(rb"startxref\s+([0-9]{1,12})\s+%%EOF\s*\Z")
# One bounded alternative, scoped to marked V8 pages. Reclaim whitespace only:
# no smaller fonts/line heights, scaling, hidden content or changed tree geometry.
V8_FIT_CSS = """
.page.is-fit-compact[data-page] .body{gap:2mm;padding-top:3mm;padding-bottom:2mm}
.page.is-fit-compact[data-page] .hdr{padding-top:6mm;padding-bottom:1.5mm}
.page.is-fit-compact[data-page] .ftr{padding-top:1.5mm;padding-bottom:5mm}
.page.is-fit-compact[data-page] .block > h2{margin-bottom:1.1mm}
.page.is-fit-compact[data-page] .fact{padding-top:2.6mm;padding-bottom:2.6mm}
.page.is-fit-compact[data-page] .voice{padding-top:2.8mm;padding-bottom:2.8mm}
.page.is-fit-compact[data-page] .voice__h{margin-bottom:1.8mm}
.page.is-fit-compact[data-page] .voice__p{margin-bottom:1.3mm}
.page.is-fit-compact[data-page] .voice__topics{margin-bottom:1.3mm}
.page.is-fit-compact[data-page] .voice__topics li{margin-bottom:.8mm}
.page.is-fit-compact[data-page] .mp > div{padding-top:1.8mm;padding-bottom:1.8mm}
.page.is-fit-compact[data-page] .stage-ev > div{padding-top:1.9mm;padding-bottom:1.9mm}
.page.is-fit-compact[data-page] .lv--1 .risk{padding-top:.75mm;padding-bottom:.75mm}
.page.is-fit-compact[data-page] .mon__r{padding-top:.6mm;padding-bottom:.6mm}
.page.is-fit-compact[data-page] .kv > div{padding-top:.65mm;padding-bottom:.65mm}
.page.is-fit-compact[data-page] .usp{padding-top:3.2mm;padding-bottom:3.2mm}
.page.is-fit-compact[data-page] .idx__r{padding-top:1.3mm;padding-bottom:1.3mm}
.page.is-fit-compact[data-page] .pilot{padding-top:3mm;padding-bottom:3mm}
.page.is-fit-compact[data-page] .road{margin-top:2mm;padding-top:2mm}
.page.is-fit-compact[data-page] .sol{padding-top:2.8mm;padding-bottom:2.8mm}
.page.is-fit-compact[data-page] .sol__col{padding-top:1.8mm;padding-bottom:1.8mm}
.page.is-fit-compact[data-page] .sol__lead{margin-top:1.8mm}
.page.is-fit-compact[data-page] .sol__who{margin-top:1.8mm}
.page.is-fit-compact[data-page] .sol__vars{margin-top:1.8mm}
.page.is-fit-compact[data-page] .sol__foot{margin-top:1.8mm;padding-top:1.5mm}
.page.is-fit-compact[data-page] .sol__col li{margin-bottom:.45mm}
.page.is-fit-compact[data-page] .mods--sol{gap:2mm}
.page.is-fit-compact[data-page] .obs-list > li{padding-top:1mm;padding-bottom:1mm}
"""
V8_FIT_JS = """numbers => document.querySelectorAll('.page').forEach((page, index) => {
  if (numbers.includes(index + 1)) page.classList.add('is-fit-compact');
})"""
OVERFLOW_JS = """() => {
  const out=[];
  const fields=window.__radarPdfTextFields || [];
  const norm=value => String(value || '').replace(/\\s+/g,' ').trim();
  document.querySelectorAll('.page').forEach((page,i)=>{
    const body=page.querySelector('.body');
    const limit=body.getBoundingClientRect().bottom+0.5;
    const prefixes=page.dataset.page.startsWith('solutions-')
      ? Array.from(page.querySelectorAll('.sol__n')).map(el=>
          'offer.modules.'+(parseInt(el.textContent,10)-1)+'.')
      : ({situation:['situation.','priority.'],diagnosis:['diagnosis.'],
          scenario:['scenario.'],offer:['offer.'],
          'observation-passport':['qualification.'],
          'observation-evidence':['official.','signals.'],
          'observation-sources':['sources.','qualification.manual_checks.']
         })[page.dataset.page] || [];
    const add=(el,kind,px)=>{
      const text=norm(el.textContent);
      const matching=fields.filter(f=>f.text.length>=12 &&
        prefixes.some(prefix=>f.path.startsWith(prefix)) && text.includes(norm(f.text)))
        .sort((a,b)=>b.text.length-a.text.length || a.path.localeCompare(b.path));
      out.push({page:i+1,name:page.dataset.page,block:el.className,kind,
        over_px:Math.ceil(px),field_paths:matching.map(f=>f.path),
        text_fields:matching.slice(0,6).map(f=>({path:f.path,chars:f.text.length,
          excerpt:f.text.slice(0,180)}))});
    };
    if(body.scrollHeight>body.clientHeight+1)add(body,'height',body.scrollHeight-body.clientHeight);
    body.querySelectorAll(':scope > *').forEach(el=>{
      const r=el.getBoundingClientRect();if(r.bottom>limit)add(el,'bottom',r.bottom-limit);
    });
    page.querySelectorAll('*').forEach(el=>{
      const css=getComputedStyle(el);
      if(el.clientWidth>0 && el.scrollWidth>el.clientWidth+2 
        && !['inline','contents'].includes(css.display))
        add(el,'width',el.scrollWidth-el.clientWidth);
      if(['hidden','clip'].includes(css.overflowY) 
        && el.clientHeight>0 && el.scrollHeight>el.clientHeight+2)
        add(el,'clipped',el.scrollHeight-el.clientHeight);
    });
    const hero=page.querySelector('.hero');
    if(hero && hero.scrollHeight>hero.clientHeight+1)
      add(hero,'height',hero.scrollHeight-hero.clientHeight);
  });return out;
}"""


def _report_text_fields(data):
    """Exact diagnostic field identities; snippets never replace exported content."""
    result = []

    def visit(value, parts):
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in {"meta", "object", "url", "move", "service_ids", "method_ids"}:
                    visit(item, [*parts, key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, [*parts, str(index)])
        elif isinstance(value, str):
            result.append({"path": ".".join(parts), "text": value})

    visit(data, [])
    return result


def build_html(data):
    env = Environment(
        loader=FileSystemLoader(ROOT / "template"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    if is_v8_observation(data):
        env.filters["observation_link"] = _observation_link
        urls = {source["n"]: source.get("url") for source in data["sources"]}
        return env.get_template("v8/observation.html.j2").render(
            **data, src_url=urls, base_url=ROOT.as_uri() + "/"
        )
    urls = {s["n"]: s.get("url") or "#" for s in data["diagnosis"]["sources"]}
    template = (
        "v8/report.html.j2"
        if is_v8(data)
        else "v5/report.html.j2"
        if is_v5(data)
        else "legacy/report.html.j2"
    )
    return env.get_template(template).render(**data, src_url=urls, base_url=ROOT.as_uri() + "/")


def is_v5(data):
    """Historical saved payloads keep their original layout and contract."""
    return data.get("meta", {}).get("schema_version") == "5" or (
        "scenario" in data and "pilot" in data.get("offer", {})
    )


def is_v8(data):
    return data.get("meta", {}).get("schema_version") == "8" or any(
        "move" in module for module in data.get("offer", {}).get("modules", [])
    )


def is_v8_observation(data):
    meta = data.get("meta", {})
    return meta.get("schema_version") == "8" and meta.get("mode") == "observation"


def _observation_link(value):
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme in {"https", "http"} and parts.hostname and not (
            parts.username or parts.password
        ):
            return value
    except ValueError:
        pass
    return ""


def page_count(data):
    if is_v8_observation(data):
        return 3
    base = 3 if data["meta"]["layout"] == "compact" else 4
    return base + ((len(data["offer"]["modules"]) + 1) // 2 if is_v5(data) else 0)


def template_hash():
    digest = hashlib.sha256()
    digest.update(Path(__file__).read_bytes())
    for directory in ("template", "assets", "config", "config_v8", "matrix_data"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file():
                digest.update(path.read_bytes())
    return digest.hexdigest()


def render(data, output=None, png=False):
    """No external network, no text truncation, no fallback to another PDF engine."""
    with tempfile.TemporaryDirectory(prefix="megapolis-pdf-") as folder:
        path = Path(folder) / "report.html"
        path.write_text(build_html(data), encoding="utf-8")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page(
                        viewport={"width": 794, "height": 1123}, device_scale_factor=2
                    )
                    page.route("http://**", lambda route: route.abort())
                    page.route("https://**", lambda route: route.abort())
                    page.goto(path.as_uri())
                    page.evaluate("document.fonts.ready")
                    page.wait_for_load_state("networkidle")
                    page.evaluate(
                        "fields => {window.__radarPdfTextFields = fields}",
                        _report_text_fields(data),
                    )
                    overflow = page.evaluate(OVERFLOW_JS)
                    if overflow and is_v8(data):
                        numbers = sorted(
                            {
                                item["page"]
                                for item in overflow
                                if item["kind"] in {"height", "bottom", "clipped"}
                            }
                        )
                        if numbers:
                            page.add_style_tag(content=V8_FIT_CSS)
                            page.evaluate(V8_FIT_JS, numbers)
                            overflow = page.evaluate(OVERFLOW_JS)
                    elif overflow and not is_v5(data):
                        # Recover whitespace before spending model calls on rewriting.
                        # Font sizes and every word remain unchanged.
                        page.evaluate(
                            """names => document.querySelectorAll('.page').forEach(p => {
                            if (names.includes(p.dataset.page)) p.classList.add('is-dense');
                        })""",
                            list({item["name"] for item in overflow}),
                        )
                        overflow = page.evaluate(OVERFLOW_JS)
                    count = page.locator(".page").count()
                    if is_v8(data) and count > MAX_V8_PAGES:
                        raise ValueError("Разбор V8 превышает шесть страниц")
                    if count != page_count(data):
                        raise ValueError("Неверное число страниц отчёта")
                    if overflow and is_v8(data):
                        # The caller may request one editorial revision. Do not
                        # materialize or persist a clipped document in the meantime.
                        return b"", overflow
                    binary = page.pdf(prefer_css_page_size=True, print_background=True)
                    if is_v8(data) and len(PDF_PAGE.findall(binary)) != count:
                        raise ValueError("Число страниц PDF не совпадает с макетом V8")
                    if output:
                        output = Path(output)
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(binary)
                        if png:
                            for i, el in enumerate(page.query_selector_all(".page"), 1):
                                el.screenshot(path=str(output.with_name(f"{output.stem}-{i}.png")))
                finally:
                    browser.close()
        except Error as exc:
            raise ValueError(
                "Не удалось собрать PDF в Chromium. Проверьте установку: "
                "uv run playwright install chromium. Другой движок не используется."
            ) from exc
    return binary, overflow


@lru_cache(maxsize=12)
def _cached(serialized, version):
    data = json.loads(serialized)
    binary, overflow = render(data)
    if overflow and (is_v5(data) or is_v8(data)):
        raise ValueError("Текст отчёта не поместился в макет; требуется редактура полей")
    return binary


def pdf_bytes(data):
    return _cached(json.dumps(data, ensure_ascii=False, sort_keys=True), template_hash())


def write_pdf_cache(path, binary):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pdf-", dir=path.parent) as folder:
            pending = Path(folder) / path.name
            with pending.open("wb") as stream:
                stream.write(binary)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(path)
    except OSError:
        LOGGER.warning("Could not write PDF cache %s", path.name, exc_info=True)


def _complete_v8_pdf(binary, pages):
    # Chromium emits PDF 1.4 with a plain cross-reference table.
    trailer = PDF_TRAILER.search(binary[-128:])
    return (
        binary.startswith(b"%PDF-")
        and trailer is not None
        and binary.startswith(b"xref", int(trailer[1]))
        and len(PDF_PAGE.findall(binary)) == pages
    )


def download_v8_pdf(data, path):
    from radar.reports.schema_v8 import Report

    pages = page_count(data)
    try:
        binary = path.read_bytes()
    except OSError:
        binary = b""
    if _complete_v8_pdf(binary, pages):
        return binary

    binary = pdf_bytes(Report.model_validate(data).model_dump())
    if not _complete_v8_pdf(binary, pages):
        raise ValueError("Не удалось восстановить полный PDF отчёта")
    write_pdf_cache(path, binary)
    return binary
