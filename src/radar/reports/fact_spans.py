"""Restore exact source spans after reversible whitespace/NFC formatting only."""

import re
import unicodedata


def normalized_with_spans(text, *, join_lines=False):
    """Each normalized character retains its exact original source coordinates."""
    chars, spans = [], []
    index = 0
    while index < len(text):
        end = index + 1
        if text[index].isspace():
            while end < len(text) and text[end].isspace():
                end += 1
            normalized = "" if join_lines and any(c in text[index:end] for c in "\r\n") else " "
        else:
            while end < len(text) and unicodedata.combining(text[end]):
                end += 1
            normalized = unicodedata.normalize("NFC", text[index:end])
        for char in normalized:
            chars.append(char)
            spans.append((index, end))
        index = end
    return "".join(chars), spans


def source_span(quote, source, prepared=None):
    """Return source bytes verbatim, never casefold, fuzzy-match or rewrite words.

    A model-added final full stop may be omitted only for a unique long sentence
    body. Internal punctuation, abbreviations and numeric endings are untouched.
    """
    if quote and quote in source:
        return quote
    normalized, positions = prepared or normalized_with_spans(source)
    target = normalized_with_spans(quote.strip())[0]

    def exact_span(needle):
        start = normalized.find(needle)
        if start < 0 or normalized.find(needle, start + 1) >= 0:
            return None
        end = start + len(needle)
        value = source[positions[start][0] : positions[end - 1][1]]
        return value if normalized_with_spans(value)[0] == needle else None

    if target:
        match = exact_span(target)
        if match is not None:
            return match
        # HTML table rows can be concatenated by the model. Remove only source
        # line-break runs, retain every other character and map back to one span.
        # Never choose between multiple normalized candidates or match fragments.
        first = normalized.find(target)
        if first >= 0 and normalized.find(target, first + 1) >= 0:
            return None
        if len(target) >= 30:
            joined, joined_positions = normalized_with_spans(source, join_lines=True)
            start = joined.find(target)
            if start >= 0 and joined.find(target, start + 1) < 0:
                value = source[
                    joined_positions[start][0] : joined_positions[start + len(target) - 1][1]
                ]
                if (
                    any(c in value for c in "\r\n")
                    and normalized_with_spans(value, join_lines=True)[0] == target
                    and re.sub(r"\s", "", unicodedata.normalize("NFC", value))
                    == re.sub(r"\s", "", unicodedata.normalize("NFC", quote.strip()))
                ):
                    return value
    if (
        target.endswith(".")
        and len(target[:-1]) >= 30
        and re.search(r"[А-Яа-яЁёA-Za-z]{3,}\.$", target)
    ):
        return exact_span(target[:-1])
    return None
