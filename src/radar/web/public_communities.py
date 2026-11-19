"""Read public Telegram previews; never join, sign in or enumerate participants."""

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

PUBLIC_PREVIEW_VERSION = "public-telegram-preview-v1"
_SLUG = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{3,31}\Z")
_POST = re.compile(r"([a-zA-Z][a-zA-Z0-9_]{3,31})/([1-9][0-9]*)\Z")
_COMMUNITY_TITLE = re.compile(
    r"\b(?:чат(?:а|е|ы|ов|у)?|канал(?:а|е|ы|ов|у)?|telegram|телеграм)\b", re.I
)
_PROFILE_TITLE = re.compile(
    r"\b(?:contact|contacts|profile|профиль|контакт|связаться|бот|bot)\b", re.I
)
_RESERVED = {
    "addemoji",
    "addlist",
    "addstickers",
    "auth",
    "boost",
    "c",
    "confirmphone",
    "contact",
    "giftcode",
    "invoice",
    "iv",
    "joinchat",
    "login",
    "m",
    "nft",
    "passport",
    "proxy",
    "s",
    "setlanguage",
    "share",
    "socks",
    "stars",
    "web",
    "z",
}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK = {"p", "div", "br", "li", "blockquote", "pre"}
_SKIP = {"script", "style", "svg", "noscript"}


def _slug(value):
    return (
        bool(_SLUG.fullmatch(value))
        and value.casefold() not in _RESERVED
        and not value.casefold().endswith("bot")
    )


def _parts(url):
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or (parts.hostname or "").casefold() != "t.me"
        or parts.username
        or parts.password
    ):
        return []
    try:
        if parts.port not in {None, 80, 443}:
            return []
    except ValueError:
        return []
    return parts.path.strip("/").split("/")


def public_preview_url(url, title=""):
    """Choose one public read URL, or None for Telegram invites/profiles/actions."""
    if (urlsplit(url).hostname or "").casefold() != "t.me":
        return url
    parts = _parts(url)
    # Already selected public previews/posts retain their original URL and query.
    if is_public_preview(url) or (
        len(parts) == 2 and _slug(parts[0]) and re.fullmatch(r"[1-9][0-9]*", parts[1])
    ):
        return url
    if len(parts) != 1 or not _slug(parts[0]):
        return None
    if not _COMMUNITY_TITLE.search(title) or _PROFILE_TITLE.search(title):
        return None
    if urlsplit(url).query:
        # Action/deep-link parameters can identify profiles or bot interactions.
        return None
    return "https://t.me/s/" + parts[0]


def is_public_preview(url):
    parts = _parts(url)
    return (
        len(parts) in {2, 3}
        and parts[0] == "s"
        and _slug(parts[1])
        and (len(parts) == 2 or bool(re.fullmatch(r"[1-9][0-9]*", parts[2])))
    )


def _text(parts):
    return "\n".join(
        line
        for line in (re.sub(r"[\t\r\f\v ]+", " ", s).strip() for s in "".join(parts).splitlines())
        if line
    )


class _PreviewParser(HTMLParser):
    def __init__(self, channel):
        super().__init__(convert_charrefs=True)
        self.channel = channel.casefold()
        self.stack = []
        self.current = None
        self.body_depth = None
        self.messages = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        if tag not in _VOID:
            self.stack.append(tag)
        if "tgme_widget_message" in classes and self.current is None:
            post = _POST.fullmatch(attributes.get("data-post", ""))
            if post and post[1].casefold() == self.channel:
                self.current = {
                    "depth": len(self.stack),
                    "tag": tag,
                    "url": "https://t.me/" + post[0],
                    "date": None,
                    "parts": [],
                }
        if self.current is None:
            return
        if "tgme_widget_message_text" in classes:
            self.body_depth = len(self.stack)
        if self.body_depth is not None and tag in _BLOCK:
            self.current["parts"].append("\n")
        if tag == "time" and attributes.get("datetime") and self.current["date"] is None:
            value = attributes["datetime"]
            try:
                if len(value) <= 40:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    self.current["date"] = value
            except ValueError:
                pass

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.current and depth <= self.current["depth"]:
            message = {
                "url": self.current["url"],
                "date": self.current["date"],
                "text": _text(self.current["parts"]),
            }
            if message["text"] and message["url"] not in self.seen and len(self.messages) < 100:
                self.messages.append(message)
                self.seen.add(message["url"])
            self.current, self.body_depth = None, None
        elif self.body_depth is not None and depth <= self.body_depth:
            self.body_depth = None
        elif self.current and self.body_depth is not None and tag in _BLOCK:
            self.current["parts"].append("\n")
        del self.stack[depth - 1 :]

    def handle_data(self, data):
        if (
            self.current
            and self.body_depth is not None
            and not any(tag in _SKIP for tag in self.stack)
        ):
            self.current["parts"].append(data)


def public_preview_messages(url, html, *, max_text=40_000):
    """Return actual public post bodies and their own explicit dates, without page dating."""
    if not is_public_preview(url):
        return [], ""
    parser = _PreviewParser(_parts(url)[1])
    parser.feed(html)
    messages, chunks, used = [], [], 0
    for message in parser.messages:
        chunk = "Дата сообщения: " + message["date"] + "\n" if message["date"] else ""
        chunk += message["url"] + "\n" + message["text"]
        if used + len(chunk) + 2 > max_text:
            break
        messages.append(message)
        chunks.append(chunk)
        used += len(chunk) + 2
    return messages, "\n\n".join(chunks)
