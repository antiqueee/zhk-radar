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

from radar.web.store import now

MAX_BYTES = 2_000_000
MAX_TEXT = 40_000


def primary_text(url, text):
    """A review's related-review cards are not evidence from that review."""
    host = (urlsplit(url).hostname or "").lower()
    if host in {"banki.ru", "www.banki.ru"} and "/realty/responses/response/" in url:
        boundary = re.search(r"(?im)^\s*ещ[её] отзывы о застройщике\s*$", text)
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


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.article = 0
        self.title_depth = 0
        self.text = []
        self.main = []
        self.title = []
        self.published = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}:
            self.skip += 1
        if tag in {"article", "main"}:
            self.article += 1
        if tag == "title":
            self.title_depth += 1
        if tag == "meta" and attributes.get("property", attributes.get("name")) in {
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
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.text.append("\n")
            if self.article:
                self.main.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"article", "main"}:
            self.article = max(0, self.article - 1)
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.text.append("\n")
            if self.article:
                self.main.append("\n")

    def handle_data(self, data):
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
        parser = ArticleParser()
        parser.feed(html)
        main = clean_text("".join(parser.main))
        text = main if len(main) >= 120 else clean_text("".join(parser.text))
        if "text/plain" in mime:
            text = clean_text(html)
        if len(text) < 120:
            raise SourceError(
                "Недостаточно текста; возможно, страница требует JavaScript или входа"
            )
        page = Page(
            initial,
            current,
            "loaded",
            title=clean_text("".join(parser.title))[:1000],
            text=text[:MAX_TEXT],
            published_at=parser.published,
            fetched_at=now(),
            content_hash=hashlib.sha256(body).hexdigest(),
            truncated=len(text) > MAX_TEXT,
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


def load_page(store, url, reader=fetch_page):
    initialize_sources(store)
    url = canonical_url(url)
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM source_pages WHERE url=? AND fetched_at>=? "
            "AND json_extract(payload, '$.status') = 'loaded' "
            "ORDER BY fetched_at DESC LIMIT 1",
            (url, now()[:10]),
        ).fetchone()
    if row:
        return Page(**json.loads(row[0])), True
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
