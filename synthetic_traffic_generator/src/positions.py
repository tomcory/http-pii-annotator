"""
Positions of annotated values in a message.

An annotation names a value and the component it is in. That is not enough to say *where* the
value is: a short value such as `3` can occur in several places of a body, only one of which is
the annotated one. This module resolves every annotation to the exact spans of the message
that hold the value, and reports annotations that cannot be located unambiguously.

A span is given by character offsets into a string of the stored message:

- `path` and `query`: offsets into `http_request.path` (the path including the query string),
- `header`: offsets into the value of the header with index `header_index`,
- `body`: offsets into the body.

Annotation values are decoded values, the stored message is the transmitted one. The text of a
span is therefore the *raw* form of the value: in a JSON body it may contain escape sequences,
in a query string or form-encoded body it is URL-encoded. `span_text()` returns it,
`span_value()` its decoded form.
"""

import json
import re
import urllib.parse

SHORT_VALUE_CHARS = 5
NUMERIC_PATTERN = re.compile(r"^[\d.,:+\-/ ]+$")


# --------------------------------------------------------------------------- decoding with offsets

def _map_plain(raw: str) -> tuple[str, list[int], list[int]]:
    positions = list(range(len(raw)))
    return raw, positions, [p + 1 for p in positions]


def _map_json_string(raw: str) -> tuple[str, list[int], list[int]]:
    """Decode the content of a JSON string; for each decoded character, the raw range it comes from."""
    decoded, starts, ends = [], [], []
    i = 0
    while i < len(raw):
        if raw[i] != "\\" or i + 1 >= len(raw):
            decoded.append(raw[i])
            starts.append(i)
            ends.append(i + 1)
            i += 1
            continue
        length = 2
        if raw[i + 1] == "u":
            length = 6
            # a surrogate pair encodes one character
            if re.match(r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}", raw[i:i + 12]):
                length = 12
        try:
            char = json.loads('"' + raw[i:i + length] + '"')
        except ValueError:
            char = raw[i:i + length]
        decoded.append(char)
        starts.extend([i] * len(char))
        ends.extend([i + length] * len(char))
        i += length
    return "".join(decoded), starts, ends


def _map_url(raw: str) -> tuple[str, list[int], list[int]]:
    """URL-decode a query or form value; for each decoded character, the raw range it comes from."""
    decoded, starts, ends = [], [], []
    i = 0
    while i < len(raw):
        if raw[i] == "+":
            decoded.append(" ")
            starts.append(i)
            ends.append(i + 1)
            i += 1
        elif raw[i] == "%" and re.match(r"%[0-9a-fA-F]{2}", raw[i:i + 3]):
            # a character may be encoded as several percent-encoded bytes
            first = int(raw[i + 1:i + 3], 16)
            count = 1 if first < 0x80 else 2 if first >> 5 == 0b110 else 3 if first >> 4 == 0b1110 else \
                4 if first >> 3 == 0b11110 else 1
            chunk = raw[i:i + 3 * count]
            try:
                if not re.fullmatch(r"(%[0-9a-fA-F]{2})+", chunk) or len(chunk) != 3 * count:
                    raise ValueError
                char = bytes.fromhex(chunk.replace("%", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                char, count = urllib.parse.unquote(raw[i:i + 3], errors="replace"), 1
            decoded.append(char)
            starts.extend([i] * len(char))
            ends.extend([i + 3 * count] * len(char))
            i += 3 * count
        else:
            decoded.append(raw[i])
            starts.append(i)
            ends.append(i + 1)
            i += 1
    return "".join(decoded), starts, ends


CODECS = {"plain": _map_plain, "json": _map_json_string, "url": _map_url}


# --------------------------------------------------------------------------- leaves of a message

def _leaf(text: str, start: int, end: int, path: list, key: str | None, codec: str, **extra) -> dict:
    return {"start": start, "end": end, "path": path, "key": key, "codec": codec, "raw": text[start:end], **extra}


def json_leaves(text: str) -> list[dict]:
    """The scalar values of a JSON document with their offsets (the document must be valid JSON)."""
    leaves = []
    whitespace = " \t\r\n"

    def skip(i):
        while i < len(text) and text[i] in whitespace:
            i += 1
        return i

    def string_end(i):  # i: index of the opening quote; returns the index of the closing quote
        j = i + 1
        while text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return j

    def scan(i, path, key):
        i = skip(i)
        char = text[i]
        if char == "{":
            i = skip(i + 1)
            while text[i] != "}":
                end = string_end(i)
                name = json.loads(text[i:end + 1])
                i = skip(end + 1)
                i = scan(i + 1, path + [name], name)  # text[i] is the colon
                i = skip(i)
                if text[i] == ",":
                    i = skip(i + 1)
            return i + 1
        if char == "[":
            i = skip(i + 1)
            index = 0
            while text[i] != "]":
                i = scan(i, path + [index], key)
                index += 1
                i = skip(i)
                if text[i] == ",":
                    i = skip(i + 1)
            return i + 1
        if char == '"':
            end = string_end(i)
            leaves.append(_leaf(text, i + 1, end, path, key, "json"))
            return end + 1
        end = i
        while end < len(text) and text[end] not in ",]}" + whitespace:
            end += 1
        leaves.append(_leaf(text, i, end, path, key, "plain"))
        return end

    scan(0, [], None)
    return leaves


def form_leaves(text: str, offset: int = 0) -> list[dict]:
    """The parameter values of a query string or form-encoded body with their offsets."""
    leaves = []
    position = 0
    for index, pair in enumerate(text.split("&")):
        if "=" in pair:
            key, _ = pair.split("=", 1)
            start = position + len(key) + 1
            leaf = _leaf(text, start, position + len(pair), [index], urllib.parse.unquote_plus(key), "url")
            leaf["start"] += offset
            leaf["end"] += offset
            leaves.append(leaf)
        position += len(pair) + 1
    return leaves


def body_format(http_message: dict) -> str:
    body = http_message.get("body")
    if not isinstance(body, str) or not body.strip():
        return "none"
    if body.lstrip()[:1] in "{[":
        try:
            json.loads(body)
            return "json"
        except ValueError:
            pass
    content_type = ""
    for header in http_message.get("headers") or []:
        if isinstance(header, dict) and str(header.get("key", "")).lower() == "content-type":
            content_type = str(header.get("value", "")).split(";")[0].strip().lower()
    stripped = body.strip()
    if content_type == "application/x-www-form-urlencoded" and "=" in stripped and " " not in stripped:
        return "form"
    return "text"


def component_leaves(http_message: dict, placement: str) -> list[dict]:
    """The values of a component of a message, with the offsets of their raw text."""
    if placement in ("path", "query"):
        full = http_message.get("path")
        if not isinstance(full, str):
            return []
        path, separator, query = full.partition("?")
        if placement == "query":
            return form_leaves(query, len(path) + 1) if separator else []
        leaves, position = [], 0
        for index, segment in enumerate(path.split("/")):
            if segment:
                leaves.append(_leaf(full, position, position + len(segment), [index], None, "url"))
            position += len(segment) + 1
        return leaves

    if placement == "header":
        leaves = []
        for index, header in enumerate(http_message.get("headers") or []):
            if isinstance(header, dict) and isinstance(header.get("value"), str):
                leaves.append(_leaf(header["value"], 0, len(header["value"]), [index], str(header.get("key", "")),
                                    "plain", header_index=index))
        return leaves

    if placement == "body":
        body = http_message.get("body")
        if not isinstance(body, str) or not body or http_message.get("body_is_base64"):
            return []
        kind = body_format(http_message)
        if kind == "json":
            return json_leaves(body)
        if kind == "form":
            return form_leaves(body)
        return [_leaf(body, 0, len(body), [], None, "plain")]

    return []


# --------------------------------------------------------------------------- locating values

def is_short(value: str) -> bool:
    """Short and numeric values occur by chance; their position must be pinned down by a field."""
    return len(value) < SHORT_VALUE_CHARS or (len(value) < 8 and bool(NUMERIC_PATTERN.match(value)))


def occurrences(leaf: dict, value: str, boundaries: bool = False) -> list[dict]:
    """The spans of a value inside a leaf."""
    decoded, starts, ends = CODECS[leaf["codec"]](leaf["raw"])
    spans = []
    index = decoded.find(value)
    while index != -1:
        last = index + len(value) - 1
        inside_word = (index > 0 and decoded[index - 1].isalnum()) or (last + 1 < len(decoded) and decoded[last + 1].isalnum())
        if not (boundaries and inside_word):
            span = {
                "start": leaf["start"] + starts[index], "end": leaf["start"] + ends[last],
                "path": leaf["path"], "key": leaf["key"], "whole": decoded == value,
            }
            if "header_index" in leaf:
                span["header_index"] = leaf["header_index"]
            spans.append(span)
        index = decoded.find(value, index + len(value))
    return spans


def _path_string(path: list) -> str:
    text = ""
    for part in path:
        text += f"[{part}]" if isinstance(part, int) else ("." if text else "") + str(part)
    return text.lower()


def field_matches(field: str, span: dict) -> bool:
    """Whether a field given for an annotation (a key, header name or path) names the place of a span."""
    field = (field or "").strip().strip("$.").lower()
    if not field:
        return False
    key = (span.get("key") or "").lower()
    path = _path_string(span.get("path") or [])
    last = [part for part in re.split(r"[.\[\]/ ]+", field) if part]
    return (bool(key) and (field == key or (last and last[-1] == key))) or path == field or path.endswith("." + field)


def locate(http_message: dict, placement: str, value: str, field: str = None) -> tuple[list[dict], str]:
    """
    Locate an annotated value in a component.

    Returns (spans, "") or ([], reason). A long or identifying value is annotated wherever it
    occurs. A short or numeric value is only located where the field says, or if it occurs once.
    """
    leaves = component_leaves(http_message, placement)
    short = is_short(value)
    candidates = [span for leaf in leaves for span in occurrences(leaf, value, boundaries=short)]
    if not candidates:
        return [], "not found"
    if not short:
        return candidates, ""

    matched = [span for span in candidates if field_matches(field, span)]
    if matched:
        return matched, ""
    if len(candidates) == 1:
        return candidates, ""
    whole = [span for span in candidates if span["whole"]]
    if len(whole) == 1:
        return whole, ""
    return [], f"occurs {len(candidates)} times"


def resolve_positions(message: dict, http_key: str, prefix: str = None) -> list[str]:
    """
    Set the `spans` of every annotation of a message (and its `field`, if it is missing).

    Returns the errors for annotations that cannot be located.
    """
    prefix = prefix or f"{'Request' if http_key == 'http_request' else 'Response'} {message.get('id', 'unknown')}"
    http_message = message.get(http_key)
    errors = []
    if not isinstance(http_message, dict):
        return errors

    for index, annotation in enumerate(message.get("pii_annotations") or []):
        if not isinstance(annotation, dict):
            continue
        value, placement = annotation.get("value"), annotation.get("placement")
        if not isinstance(value, str) or not value or not isinstance(placement, str):
            continue
        field = annotation.get("field") if isinstance(annotation.get("field"), str) else None
        spans, reason = locate(http_message, placement, value, field)
        if not spans:
            annotation["spans"] = []
            if reason == "not found":
                errors.append(
                    f"{prefix}, annotation {index}: PII value '{value}' ({annotation.get('data_type')}, {placement}) "
                    f"cannot be located as a value of the {placement}"
                )
            else:
                errors.append(
                    f"{prefix}, annotation {index}: PII value '{value}' ({annotation.get('data_type')}, {placement}) "
                    f"{reason} in the {placement}; set `field` to the key, parameter or header that holds this value"
                )
            continue
        annotation["spans"] = [{k: v for k, v in span.items() if k != "whole"} for span in spans]
        if not field:
            named = next((span for span in spans if span.get("key")), None)
            if named:
                annotation["field"] = named["key"]
    return errors


# --------------------------------------------------------------------------- reading spans

def _target(http_message: dict, placement: str, span: dict) -> str:
    if placement in ("path", "query"):
        return http_message.get("path") or ""
    if placement == "header":
        return http_message["headers"][span["header_index"]]["value"]
    return http_message.get("body") or ""


def span_text(http_message: dict, placement: str, span: dict) -> str:
    """The raw text of a span, as it is in the stored message."""
    return _target(http_message, placement, span)[span["start"]:span["end"]]


def span_value(http_message: dict, placement: str, span: dict) -> str:
    """The decoded text of a span (what an annotation's `value` holds)."""
    raw = span_text(http_message, placement, span)
    if placement in ("path", "query"):
        return _map_url(raw)[0]
    if placement == "body":
        kind = body_format(http_message)
        if kind == "form":
            return _map_url(raw)[0]
        if kind == "json" and "\\" in raw:
            return _map_json_string(raw)[0]
    return raw


def leaf_at(http_message: dict, placement: str, path: list) -> dict | None:
    """The leaf of a component with the given structural path."""
    for leaf in component_leaves(http_message, placement):
        if leaf["path"] == path:
            return leaf
    return None
