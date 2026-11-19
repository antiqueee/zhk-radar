"""Bounded public-page reader. Connections are pinned to validated public IPs."""

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from radar.web.public_communities import is_public_preview, public_preview_messages
from radar.web.store import now

MAX_BYTES = 2_000_000
MAX_TEXT = 40_000
PRIMARY_TEXT_VERSION = "primary-page-boundaries-v3"


def primary_text(url, text):
    """Site-specific related cards are not evidence from the requested publication."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in {"banki.ru", "www.banki.ru"} and "/realty/responses/response/" in url:
        boundary = re.search(r"(?im)^\s*ещ[её] отзывы о застройщике\s*$", text)
        if boundary:
            return text[: boundary.start()].rstrip()
    if host == "news.mondiara.com" and re.fullmatch(
        r"/categories/[^/]+/posts/[^/]+/?", parts.path
    ):
        boundary = re.search(
            r"(?im)^\s*(?:другие новости сообщества|"
            r"полный профиль компании доступен(?: в MONDIARA)?)\s*$",
            text,
        )
        if boundary:
            return text[: boundary.start()].rstrip()
    if host in {"chatdoma.ru", "www.chatdoma.ru"} and re.fullmatch(
        r"/zhk-[^/]+/otzyvy/?", parts.path
    ):
        reviews = re.search(r"(?im)^\s*все отзывы\s*$", text)
        if reviews:
            boundary = re.compile(
                r"(?im)^\s*(?:перейти в telegram чат|группа соседей в мах|последние отзывы)\s*$"
            ).search(text, reviews.end())
            if boundary:
                return text[: boundary.start()].rstrip()
    return text


class SourceError(ValueError):
    pass


def canonical_url(value):
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise SourceError("Допустимы только публичные HTTP(S)-страницы без пароля")
    try:
        port = p.port
    except ValueError:
        raise SourceError("Некорректный адрес") from None
    if port and port not in {80, 443}:
        raise SourceError("Нестандартный порт источника")
    host = p.hostname.encode("idna").decode().lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"yclid", "gclid", "fbclid"}
    ]
    return urlunsplit((p.scheme, netloc, p.path or "/", urlencode(query), ""))


def public_addresses(host, port, resolver=None):
    resolver = resolver or socket.getaddrinfo
    addresses = list(
        dict.fromkeys(row[4][0] for row in resolver(host, port, type=socket.SOCK_STREAM))
    )
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
        for ip in addresses
    ):
        raise SourceError("Локальные и служебные адреса не загружаются")
    return addresses


def public_get(url):
    """No environment proxies, cookies or auth. A redirect requires a new public-IP check."""
    parts = urlsplit(canonical_url(url))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = public_addresses(parts.hostname, port)
    connection = http.client.HTTPConnection(parts.hostname, port, timeout=12)
    sock = None
    try:
        # Use the checked IP, not the hostname, for connect (DNS rebinding protection).
        sock = socket.create_connection((addresses[0], port), timeout=12)
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        connection.sock = sock
        path = parts.path + ("?" + parts.query if parts.query else "")
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": "MegapolisIntelligence/0.4 (public research)",
                "Accept": "text/html,text/plain;q=0.9",
                "Accept-Encoding": "identity",
            },
        )
        response = connection.getresponse()
        chunks, length = [], 0
        deadline = time.monotonic() + 20
        while length <= MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceError("Источник слишком медленно отдаёт страницу")
            sock.settimeout(min(12, remaining))
            chunk = response.read1(min(65536, MAX_BYTES + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
        body = b"".join(chunks)
        if len(body) > MAX_BYTES:
            raise SourceError("Страница превышает лимит 2 МБ")
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), body
    finally:
        connection.close()
        if sock:
            sock.close()


def _publication_date(value):
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}(?:T|\s|$)", value):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _ld_articles(value, depth=0):
    """Only document roots and @graph nodes, never nested related-article cards."""
    if depth > 15:
        return
    if isinstance(value, list):
        for child in value:
            yield from _ld_articles(child, depth + 1)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(types, list) and any(
            isinstance(kind, str)
            and re.split(r"[/#]", kind)[-1] in {"Article", "NewsArticle", "BlogPosting"}
            for kind in types
        ):
            yield value
        yield from _ld_articles(value.get("@graph"), depth + 1)


def _same_article_url(value, current):
    if isinstance(value, dict):
        value = value.get("@id", value.get("url"))
    if not isinstance(value, str):
        return False
    try:
        return canonical_url(urljoin(current, value)).rstrip("/") == current.rstrip("/")
    except (SourceError, ValueError):
        return False


def _article_rank(article, current):
    if not current:
        return 1
    for rank, key in [(3, "mainEntityOfPage"), (2, "url"), (1, "@id")]:
        if article.get(key):
            return rank if _same_article_url(article[key], current) else 0
    return 0


def _initial_data(value, depth=0):
    if depth > 15:
        return
    if isinstance(value, dict):
        state = value.get("initialState")
        if isinstance(state, dict) and isinstance(state.get("data"), dict):
            yield state["data"]
        for child in value.values():
            yield from _initial_data(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _initial_data(child, depth + 1)


class ArticleParser(HTMLParser):
    def __init__(self, url=None):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.article = 0
        self.title_depth = 0
        self.text = []
        self.main = []
        self.title = []
        self.published = None
        self.links = []
        self.structured = None
        self.structured_type = None
        self.document_url = canonical_url(url) if url else None
        self.structured_articles = []
        self.flight_data = []
        self.structured_published = None

    def primary_article(self):
        candidates = [
            (_article_rank(article, self.document_url), article)
            for article in self.structured_articles
        ]
        candidates = [(rank, article) for rank, article in candidates if rank]
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def structured_body(self):
        article = self.primary_article()
        body = article.get("articleBody") if article else None
        if not self.document_url or not isinstance(body, str):
            return ""
        # Reuse the HTML text filter for articleBody markup. Scripts remain inert/omitted.
        parser = ArticleParser()
        parser.feed(body[:200_000])
        return clean_text("".join(parser.text))

    def update_structured_date(self):
        article = self.primary_article()
        published = _publication_date(article.get("datePublished")) if article else None
        if (
            not published
            and article
            and isinstance(article.get("headline"), str)
            and article["headline"].strip()
            and self.document_url
        ):
            # A first-party SSR record must match BOTH the selected Article and its URL.
            dates = {
                _publication_date(item.get("dateBegin"))
                for item in self.flight_data
                if item.get("header") == article.get("headline")
                and _same_article_url(item.get("link"), self.document_url)
            } - {None}
            if len(dates) == 1:
                published = dates.pop()
        if not self.published or self.published == self.structured_published:
            self.published = published
            self.structured_published = published

    def read_structured(self, raw, kind):
        try:
            if kind == "ld":
                self.structured_articles.extend(list(_ld_articles(json.loads(raw)))[:50])
            else:
                match = re.fullmatch(
                    r"\s*self\.__next_f\.push\((.*)\)\s*;?\s*", raw, re.S
                )
                transport = json.loads(match[1]) if match else None
                if (
                    isinstance(transport, list)
                    and len(transport) == 2
                    and isinstance(transport[1], str)
                ):
                    for line in transport[1].splitlines():
                        if not re.match(r"^[0-9a-fA-F]+:", line):
                            continue
                        try:
                            value = json.loads(line.split(":", 1)[1])
                        except ValueError:
                            continue
                        self.flight_data.extend(list(_initial_data(value))[:50])
            self.update_structured_date()
        except (ValueError, TypeError, RecursionError):
            pass

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and not attributes.get("src"):
            self.structured = []
            self.structured_type = (
                "ld" if attributes.get("type", "").lower() == "application/ld+json" else "flight"
            )
        if tag == "a" and attributes.get("href") and len(self.links) < 500:
            self.links.append(attributes["href"])
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}:
            self.skip += 1
        if tag in {"article", "main"}:
            self.article += 1
        if tag == "title":
            self.title_depth += 1
        if tag == "meta" and attributes.get(
            "property", attributes.get("name", attributes.get("itemprop"))
        ) in {
            "article:published_time",
            "datePublished",
            "date",
            "pubdate",
        }:
            value = attributes.get("content", "")[:10]
            try:
                self.published = date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        if (
            tag == "time"
            and attributes.get("datetime")
            and not self.published
            and not self.skip
            and (self.article or attributes.get("itemprop") == "datePublished")
        ):
            try:
                self.published = date.fromisoformat(attributes["datetime"][:10]).isoformat()
            except ValueError:
                pass
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.text.append("\n")
            if self.article:
                self.main.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self.structured is not None:
            raw, self.structured = "".join(self.structured), None
            self.read_structured(raw, self.structured_type)
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"article", "main"}:
            self.article = max(0, self.article - 1)
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr", "td", "th"}:
            self.text.append("\n")
            if self.article:
                self.main.append("\n")

    def handle_data(self, data):
        if self.structured is not None:
            if sum(map(len, self.structured)) + len(data) <= 200_000:
                self.structured.append(data)
        if self.title_depth:
            self.title.append(data)
        if not self.skip:
            self.text.append(data)
            if self.article:
                self.main.append(data)


def clean_text(text):
    return "\n".join(
        line for line in (re.sub(r"\s+", " ", x).strip() for x in text.splitlines()) if line
    )


@dataclass
class Page:
    url: str
    final_url: str
    status: str
    title: str = ""
    text: str = ""
    published_at: str | None = None
    fetched_at: str = ""
    content_hash: str = ""
    truncated: bool = False
    error: str = ""
    id: str = ""
    links: list[str] | None = None
    messages: list[dict] | None = None


def is_access_page(title, text, html=""):
    """Recognize access challenges without treating a CAPTCHA in an article as its content."""
    heading = re.sub(r"[.!?…]+$", "", title.strip().casefold()).strip()
    if heading in {
        "проверяем браузер",
        "проверка браузера",
        "вы не робот",
        "доступ ограничен",
        "just a moment",
        "checking your browser",
        "access denied",
        "attention required",
        "attention required! | cloudflare",
    }:
        return True
    return len(text) < 2000 and any(
        marker in html.casefold() for marker in ("cf-chl-", "__cf_chl", "challenge-platform")
    )


def fetch_page(url, transport=public_get):
    initial = canonical_url(url)
    current = initial
    for _ in range(4):
        status, headers, body = transport(current)
        if status in {301, 302, 303, 307, 308}:
            if not headers.get("location"):
                raise SourceError("Перенаправление без адреса")
            current = canonical_url(urljoin(current, headers["location"]))
            continue
        if status != 200:
            raise SourceError(f"Источник недоступен: HTTP {status}")
        if len(body) > MAX_BYTES:
            raise SourceError("Страница превышает лимит 2 МБ")
        mime = headers.get("content-type", "").lower()
        if not any(m in mime for m in ["text/html", "application/xhtml+xml", "text/plain"]):
            raise SourceError("Формат страницы пока не поддерживается")
        if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise SourceError("Источник проигнорировал запрос несжатого текста")
        encoding = re.search(r"charset=[\"']?([\w-]+)", mime)
        try:
            html = body.decode(encoding[1] if encoding else "utf-8", errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
        parser = ArticleParser(current)
        parser.feed(html)
        main = clean_text("".join(parser.main))
        text = main if len(main) >= 120 else clean_text("".join(parser.text))
        if len(main) < 120:
            structured_body = parser.structured_body()
            if len(structured_body) >= 120:
                text = structured_body
        if "text/plain" in mime:
            text = clean_text(html)
        title = clean_text("".join(parser.title))[:1000]
        if is_access_page(title, text, html):
            raise SourceError("Источник требует проверки доступа; текст статьи не получен")
        messages = None
        if is_public_preview(current):
            messages, text = public_preview_messages(current, html, max_text=MAX_TEXT)
            if not messages:
                raise SourceError("Общедоступные сообщения в предпросмотре не получены")
        if messages is None and len(text) < 120:
            raise SourceError(
                "Недостаточно текста; возможно, страница требует JavaScript или входа"
            )
        page = Page(
            initial,
            current,
            "loaded",
            title=title,
            text=text[:MAX_TEXT],
            published_at=None if messages is not None else parser.published,
            fetched_at=now(),
            content_hash=hashlib.sha256(body).hexdigest(),
            truncated=len(text) > MAX_TEXT,
            links=(
                [message["url"] for message in messages]
                if messages is not None
                else list(dict.fromkeys(urljoin(current, link) for link in parser.links))
            ),
            messages=messages,
        )
        return page
    raise SourceError("Слишком много перенаправлений")


def initialize_sources(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS source_pages(
          id TEXT PRIMARY KEY, url TEXT NOT NULL, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS source_page_url ON source_pages(url,fetched_at);
        CREATE TABLE IF NOT EXISTS record_signals(
          id TEXT PRIMARY KEY, record_id TEXT NOT NULL, source_id TEXT NOT NULL,
          payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS signals_record ON record_signals(record_id);
        """)


def research_excerpt(text, limit=8000):
    """Keep header + contiguous issue contexts instead of spending a batch on site navigation."""
    if len(text) <= limit:
        return text
    spans = [(0, min(1800, limit))]
    used = spans[0][1]
    for match in re.finditer(
        r"перенос|задерж|жалоб|просроч|протеч|ключ[ие]|срок|дефект", text, re.I
    ):
        start, end = max(0, match.start() - 300), min(len(text), match.end() + 700)
        if any(start < b and end > a for a, b in spans):
            continue
        if used + end - start + 30 > limit:
            continue
        spans.append((start, end))
        used += end - start + 30
    return "\n[пропуск текста]\n".join(text[a:b] for a, b in sorted(spans))[:limit]


def load_page(store, url, reader=fetch_page, *, require_links=False):
    initialize_sources(store)
    url = canonical_url(url)
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM source_pages WHERE url=? AND fetched_at>=? "
            "AND json_extract(payload, '$.status') = 'loaded' "
            "ORDER BY fetched_at DESC LIMIT 1",
            (url, now()[:10]),
        ).fetchone()
    if (
        row
        and (not require_links or json.loads(row[0]).get("links") is not None)
        and (not is_public_preview(url) or bool(json.loads(row[0]).get("messages")))
    ):
        value = Page(**json.loads(row[0]))
        if is_access_page(value.title, value.text):
            value.status, value.text = "protected", ""
            value.error = "Источник требует проверки доступа; текст статьи не получен"
        return value, True
    try:
        page = reader(url)
    except (ValueError, OSError, http.client.HTTPException):
        # No raw network exception text: it may include unrelated machine information.
        page = Page(
            url,
            url,
            "unavailable",
            fetched_at=now(),
            error="Страница не получена: недоступна, запрещённый адрес или неподдерживаемый формат",
        )
    page.id = hashlib.sha256(
        (url + page.status + page.text + str(page.published_at)).encode()
    ).hexdigest()
    with store.connect() as db:
        db.execute(
            "INSERT INTO source_pages VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE "
            "SET fetched_at=excluded.fetched_at,payload=excluded.payload",
            (page.id, url, page.fetched_at, json.dumps(asdict(page), ensure_ascii=False)),
        )
    return page, False
