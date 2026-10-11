"""
Encoding layer for generated HTTP messages.

The generator produces messages whose values are plaintext. Real traffic is not like that:
bodies are compressed, values are Base64- or hex-encoded, JSON documents travel inside strings
or form parameters. This layer takes the plaintext messages and applies encodings and compression,
so that the dataset contains messages that

- are plaintext,
- contain encoded values,
- contain compressed values,
- are encoded as a whole,
- are compressed as a whole,

and combinations thereof. Encoding happens in two independent layers: the *value layer* transforms
individual values or sub-documents of a message, the *body layer* then transforms the body as a
whole. Everything is deterministic under a seed and reversible: each message records what was
applied to it, and `decode_message` restores the plaintext message.

Re-encode the messages of an existing run (no LLM needed):
    python -m src.encoding <run folder> [-seed <int>] [-profile <profile.json>]
"""

import argparse
import base64
import binascii
import copy
import gzip
import json
import logging
import os
import random
import re
import sys
import urllib.parse
import zlib
from collections import Counter
from typing import Any

try:
    import brotli
except ImportError:  # optional dependency
    brotli = None

from src import positions, util
from src.pipeline_steps import message_validation

logger = logging.getLogger(__name__)

# Shares observed in real mobile app traffic (see real_data/), adjusted so that every kind of
# message is represented. Real traffic: 15% of the request bodies and half of the text response
# bodies are compressed; about a third of the text bodies contain encoded values.
DEFAULT_PROFILE = {
    "request": {
        "value_layer": {"none": 0.58, "encoded": 0.32, "compressed": 0.10},
        "body_layer": {"none": 0.72, "gzip": 0.15, "deflate": 0.03, "brotli": 0.02, "encoded": 0.08},
    },
    "response": {
        "value_layer": {"none": 0.68, "encoded": 0.24, "compressed": 0.08},
        "body_layer": {"none": 0.50, "gzip": 0.36, "deflate": 0.02, "brotli": 0.05, "encoded": 0.07},
    },
    # share of the values of a body that the value layer transforms (at least one)
    "value_share": 0.2,
    # probability that an annotated PII value is among the transformed values
    "pii_value_rate": 0.5,
    # probability that an annotated PII value in a header or the query string is encoded
    "header_query_rate": 0.08,
}

# codecs work on bytes; a chain is applied left to right
TEXT_CODECS = ("base64", "base64url", "hex", "url")
COMPRESSION_CODECS = ("gzip", "deflate", "brotli")
WRAPPER_KEYS = ["data", "payload", "d", "p", "msg", "blob", "body", "content", "e", "enc"]


# --------------------------------------------------------------------------- codecs

def _encode_bytes(data: bytes, codec: str) -> bytes:
    if codec == "base64":
        return base64.b64encode(data)
    if codec == "base64url":
        return base64.urlsafe_b64encode(data).rstrip(b"=")
    if codec == "hex":
        return binascii.hexlify(data)
    if codec == "url":
        return urllib.parse.quote(data, safe="").encode("ascii")
    if codec == "gzip":
        return gzip.compress(data, mtime=0)
    if codec == "deflate":
        return zlib.compress(data)
    if codec == "brotli":
        return brotli.compress(data)
    raise ValueError(f"Unknown codec '{codec}'")


def _decode_bytes(data: bytes, codec: str) -> bytes:
    if codec == "base64":
        return base64.b64decode(data)
    if codec == "base64url":
        return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))
    if codec == "hex":
        return binascii.unhexlify(data)
    if codec == "url":
        return urllib.parse.unquote_to_bytes(data)
    if codec == "gzip":
        return gzip.decompress(data)
    if codec == "deflate":
        return zlib.decompress(data)
    if codec == "brotli":
        return brotli.decompress(data)
    raise ValueError(f"Unknown codec '{codec}'")


def encode_text(text: str, codecs: list[str]) -> str:
    """Apply a codec chain to a text. The chain must end with a text codec."""
    data = text.encode("utf-8")
    for codec in codecs:
        data = _encode_bytes(data, codec)
    # codecs produce ASCII; an empty chain (e.g. a JSON document that is only serialised) keeps the text
    return data.decode("utf-8")


def decode_text(text: str, codecs: list[str]) -> str:
    """Reverse a codec chain."""
    data = text.encode("utf-8")
    for codec in reversed(codecs):
        data = _decode_bytes(data, codec)
    return data.decode("utf-8")


def available_compression() -> list[str]:
    return [c for c in COMPRESSION_CODECS if c != "brotli" or brotli is not None]


# --------------------------------------------------------------------------- helpers

def _choose(rng: random.Random, weights: dict[str, float]) -> str:
    options = [(k, w) for k, w in weights.items() if w > 0 and (k != "brotli" or brotli is not None)]
    names, shares = zip(*options)
    return rng.choices(names, weights=shares)[0]


def _header(http_message: dict, name: str) -> dict | None:
    for header in http_message.get("headers") or []:
        if isinstance(header, dict) and str(header.get("key", "")).lower() == name:
            return header
    return None


def _set_header(http_message: dict, name: str, value: str, only_if_present: bool = False):
    header = _header(http_message, name.lower())
    if header is not None:
        header["value"] = value
    elif not only_if_present:
        http_message.setdefault("headers", []).append({"key": name, "value": value})


def _body_format(http_message: dict) -> str:
    body = http_message.get("body")
    if not isinstance(body, str) or not body.strip():
        return "none"
    content_type = message_validation._content_type(http_message)
    stripped = body.strip()
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return "json"
        except ValueError:
            pass
    if content_type == "application/x-www-form-urlencoded" and "=" in stripped and " " not in stripped:
        return "form"
    return "text"


class _Number(float):
    """A JSON number that remembers how it was written (e.g. 47.3780 or 1e5)."""

    def __new__(cls, raw: str):
        number = super().__new__(cls, raw)
        number.raw = raw
        return number


_NUMBER_MARK = "\ue000"


def _load_json(text: str):
    """Parse JSON, keeping the notation of non-integer numbers."""
    return json.loads(text, parse_float=_Number)


def _dumps(value, compact: bool = True) -> str:
    """Serialise JSON; numbers are written the way they were read."""
    def mark(node):
        if isinstance(node, _Number):
            return f"{_NUMBER_MARK}{node.raw}{_NUMBER_MARK}"
        if isinstance(node, dict):
            return {key: mark(child) for key, child in node.items()}
        if isinstance(node, list):
            return [mark(child) for child in node]
        return node

    text = json.dumps(mark(value), ensure_ascii=False, separators=(",", ":") if compact else None)
    return re.sub(f'"{_NUMBER_MARK}([^"{_NUMBER_MARK}]+){_NUMBER_MARK}"', r"\1", text)


def _dump_json(value, like: str) -> str:
    """Serialise JSON in the style (compact or spaced) of the original body."""
    return _dumps(value, compact='": ' not in like and '", ' not in like)


def _json_nodes(value, path=()):
    """Yield (path, node) for all nodes of a parsed JSON document except the root."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield path + (key,), child
            yield from _json_nodes(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield path + (index,), child
            yield from _json_nodes(child, path + (index,))


def _get(document, path):
    for key in path:
        document = document[key]
    return document


def _set(document, path, value):
    for key in path[:-1]:
        document = document[key]
    document[path[-1]] = value


# --------------------------------------------------------------------------- value layer

def _value_codecs(rng: random.Random, mode: str, is_container: bool) -> list[str]:
    """Sample the codec chain for one value."""
    if mode == "compressed":
        chain = [rng.choice([c for c in available_compression() if c != "brotli"]),
                 rng.choices(["base64", "base64url", "hex"], weights=[6, 2, 1])[0]]
    else:
        chain = [rng.choices(["base64", "base64url", "hex", "url"], weights=[6, 2, 2, 2])[0]]
        # a container that is only serialised is the common "JSON in a string"
        if is_container and rng.random() < 0.45:
            chain = []
    return (["json"] if is_container else []) + chain


def _encode_node(node, codecs: list[str]) -> str:
    if codecs and codecs[0] == "json":
        return encode_text(_dumps(node), codecs[1:])
    return encode_text(node if isinstance(node, str) else _dumps(node), codecs)


def _decode_node(text: str, codecs: list[str], was_string: bool):
    if codecs and codecs[0] == "json":
        return _load_json(decode_text(text, codecs[1:]))
    decoded = decode_text(text, codecs)
    return decoded if was_string else _load_json(decoded)


def _plain_text(node) -> str:
    return node if isinstance(node, str) else _dumps(node)


def _contains(record: dict, value: str) -> bool:
    """Whether the plaintext that a record encoded contains a value."""
    return value in record["_plain"] or any(value in text for text in record.get("_leaves", ()))


def _leaf_texts(node) -> list[str]:
    """The values of a sub-document as plain strings (its serialisation escapes quotes and the like)."""
    if isinstance(node, dict):
        return [text for child in node.values() for text in _leaf_texts(child)]
    if isinstance(node, list):
        return [text for child in node for text in _leaf_texts(child)]
    return [_plain_text(node)]


def _apply_value_layer(http_message: dict, annotations: list[dict], mode: str, profile: dict,
                       rng: random.Random) -> list[dict]:
    """Encode individual values of the body. Returns the records of what was applied."""
    body = http_message.get("body")
    body_format = _body_format(http_message)
    pii_values = [a["value"] for a in annotations if a.get("placement") == "body" and isinstance(a.get("value"), str)]
    records = []

    if body_format == "json":
        document = _load_json(body)
        nodes = [
            (path, node) for path, node in _json_nodes(document)
            if (isinstance(node, str) and len(node) >= 3)
            or (isinstance(node, (dict, list)) and node and len(_dumps(node)) <= 4000)
            or (isinstance(node, (int, float)) and not isinstance(node, bool) and _plain_text(node) in pii_values)
        ]
        if not nodes:
            return records

        def carries_pii(node):
            text = _plain_text(node)
            return any(value in text for value in pii_values)

        count = max(1, round(len(nodes) * profile["value_share"] * rng.uniform(0.5, 1.5)))
        chosen = []
        # annotated PII values are encoded with a fixed probability, the rest is filler
        for path, node in nodes:
            if not isinstance(node, (dict, list)) and carries_pii(node) and rng.random() < profile["pii_value_rate"]:
                chosen.append(path)
        rng.shuffle(nodes)
        for path, node in nodes:
            if len(chosen) >= count:
                break
            if path not in chosen:
                chosen.append(path)

        # transformed nodes must not contain each other
        chosen.sort(key=len)
        selected = []
        for path in chosen:
            if not any(path[:len(other)] == other for other in selected):
                selected.append(path)

        for path in selected:
            node = _get(document, path)
            codecs = _value_codecs(rng, mode, isinstance(node, (dict, list)))
            if not codecs:
                continue
            _set(document, path, _encode_node(node, codecs))
            records.append({
                "placement": "body", "path": list(path), "codecs": codecs,
                "was_string": isinstance(node, str), "_plain": _plain_text(node), "_leaves": _leaf_texts(node),
            })
        http_message["body"] = _dump_json(document, body)

    elif body_format == "form":
        pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
        candidates = [i for i, (_, value) in enumerate(pairs) if len(value) >= 3]
        if not candidates:
            return records
        count = max(1, round(len(candidates) * profile["value_share"] * rng.uniform(0.5, 1.5)))
        chosen = [i for i in candidates
                  if any(v in pairs[i][1] for v in pii_values) and rng.random() < profile["pii_value_rate"]]
        rng.shuffle(candidates)
        chosen += [i for i in candidates if i not in chosen][:max(0, count - len(chosen))]
        for index in sorted(chosen):
            key, value = pairs[index]
            # form values are URL-encoded anyway, so 'url' would not change anything
            codecs = [c for c in _value_codecs(rng, mode, False) if c != "url"] or ["base64"]
            pairs[index] = (key, encode_text(value, codecs))
            records.append({"placement": "body", "path": [index], "codecs": codecs, "was_string": True,
                            "_plain": value})
        http_message["body"] = urllib.parse.urlencode(pairs)

    elif body_format == "text":
        # other formats (XML, multipart, plain text): only annotated values can be located
        for value in dict.fromkeys(pii_values):
            if len(value) >= 3 and value in http_message["body"] and rng.random() < profile["pii_value_rate"]:
                codecs = [c for c in _value_codecs(rng, mode, False) if c != "url"] or ["base64"]
                encoded = encode_text(value, codecs)
                http_message["body"] = http_message["body"].replace(value, encoded)
                records.append({"placement": "body", "path": None, "codecs": codecs, "was_string": True,
                                "encoded": encoded, "_plain": value})
    return records


def _apply_header_query_layer(http_message: dict, annotations: list[dict], profile: dict,
                              rng: random.Random) -> list[dict]:
    """Encode annotated PII values in headers and in the query string."""
    records = []
    for annotation in annotations:
        value, placement = annotation.get("value"), annotation.get("placement")
        if placement not in ("header", "query") or not isinstance(value, str) or len(value) < 4:
            continue
        if rng.random() >= profile["header_query_rate"]:
            continue
        codecs = [rng.choices(["base64", "base64url", "hex"], weights=[5, 3, 2])[0]]
        encoded = encode_text(value, codecs)

        if placement == "query":
            path = http_message.get("path", "")
            if "?" not in path:
                continue
            base, query = path.split("?", 1)
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
            if not any(v == value for _, v in pairs):
                continue  # only whole parameter values are encoded
            pairs = [(k, encoded if v == value else v) for k, v in pairs]
            http_message["path"] = base + "?" + urllib.parse.urlencode(pairs)
        else:
            headers = [h for h in http_message.get("headers") or [] if isinstance(h, dict) and h.get("value") == value]
            if not headers:
                continue  # only whole header values are encoded
            for header in headers:
                header["value"] = encoded
        records.append({"placement": placement, "path": None, "codecs": codecs, "was_string": True,
                        "encoded": encoded, "_plain": value})
    return records


# --------------------------------------------------------------------------- body layer

def _apply_body_layer(http_message: dict, mode: str, rng: random.Random) -> dict | None:
    """Encode or compress the body as a whole. Returns the record of what was applied."""
    body = http_message.get("body")
    if not isinstance(body, str) or not body:
        return None

    # A message that already declares a content encoding gets exactly that encoding: the generator
    # writes the header of a compressed message, but its body is plaintext.
    declared = (_header(http_message, "content-encoding") or {}).get("value")
    if declared is not None:
        declared_codec = {"gzip": "gzip", "x-gzip": "gzip", "deflate": "deflate", "br": "brotli"}.get(
            str(declared).strip().lower()
        )
        if declared_codec in available_compression():
            mode = declared_codec
        else:
            # an encoding that cannot be applied (or 'identity'): the header is dropped
            http_message["headers"] = [h for h in http_message["headers"] if h is not _header(http_message, "content-encoding")]
            declared = None
    if mode == "none":
        return None

    if mode in COMPRESSION_CODECS:
        data = _encode_bytes(body.encode("utf-8"), mode)
        # compressed bodies are binary; they are stored Base64-encoded
        http_message["body"] = base64.b64encode(data).decode("ascii")
        http_message["body_is_base64"] = True
        _set_header(http_message, "Content-Encoding", declared or {"brotli": "br"}.get(mode, mode))
        _set_header(http_message, "Content-Length", str(len(data)), only_if_present=True)
        return {"codecs": [mode], "wrapper": None, "content_type": None, "binary": True,
                "header_added": declared is None}

    # mode == "encoded": the whole body is encoded as text, optionally wrapped in a parameter
    codecs = [rng.choices(["base64", "base64url", "hex", "url"], weights=[6, 2, 1, 1])[0]]
    if rng.random() < 0.25:
        codecs = [rng.choice([c for c in available_compression() if c != "brotli"])] + \
                 [rng.choice(["base64", "base64url"])]
    encoded = encode_text(body, codecs)
    original_content_type = (_header(http_message, "content-type") or {}).get("value")

    wrapper_type = rng.choices(["none", "json", "form"], weights=[4, 4, 3])[0]
    wrapper = None
    if wrapper_type == "json":
        key = rng.choice(WRAPPER_KEYS)
        document = {key: encoded}
        # wrapped payloads often come with a little metadata
        if rng.random() < 0.5:
            document.update({"v": rng.randint(1, 4), "ts": 1700000000 + rng.randrange(40000000)})
        http_message["body"] = json.dumps(document, separators=(",", ":"))
        wrapper = {"type": "json", "key": key}
        new_content_type = "application/json"
    elif wrapper_type == "form":
        key = rng.choice(WRAPPER_KEYS)
        http_message["body"] = urllib.parse.urlencode([(key, encoded)])
        wrapper = {"type": "form", "key": key}
        new_content_type = "application/x-www-form-urlencoded"
    else:
        http_message["body"] = encoded
        new_content_type = rng.choice(["text/plain", "application/octet-stream"])

    if original_content_type is not None or wrapper_type != "none":
        _set_header(http_message, "Content-Type", new_content_type)
    _set_header(http_message, "Content-Length", str(len(http_message["body"].encode("utf-8"))), only_if_present=True)
    return {"codecs": codecs, "wrapper": wrapper, "content_type": original_content_type, "binary": False}


# --------------------------------------------------------------------------- messages

def _http_key(message: dict) -> str:
    return "http_request" if "http_request" in message else "http_response"


def _visible(http_message: dict, placement: str, value) -> bool:
    """
    Whether a value can be read at its place in the transmitted message.

    The encoding that is inherent to a place does not hide a value: the URL encoding of a query
    string or form-encoded body, and the escaping of a JSON string. Everything else does.
    """
    if not isinstance(value, str) or not value:
        return False
    return any(
        positions.occurrences(leaf, value) for leaf in positions.component_leaves(http_message, placement)
    )


def _transmitted_spans(http_message: dict, annotation: dict, records: list[dict], body_record: dict | None) -> list[dict]:
    """
    Where an annotated value is in the transmitted (encoded) message.

    Each span has a `form`:
    - `plain`: the span holds the value itself,
    - `encoded`: the span holds the encoded form of the value (the value was encoded on its own),
    - `container`: the span holds an encoded value that contains the annotated value among other
      data (e.g. a Base64-encoded sub-document).
    Encoded and container spans carry their own `encodings` (outermost first): a value that
    occurs in several places can be encoded differently in each of them.
    A value inside an encoded or compressed body has no span: the body as a whole hides it.
    """
    value, placement = annotation.get("value"), annotation.get("placement")
    if not isinstance(value, str) or (placement == "body" and body_record):
        return []

    def whole_leaf(leaf, form):
        span = {"start": leaf["start"], "end": leaf["end"], "path": leaf["path"], "key": leaf["key"], "form": form}
        if "header_index" in leaf:
            span["header_index"] = leaf["header_index"]
        return span

    spans = []
    structured = placement == "body" and positions.body_format(http_message) in ("json", "form")
    if structured:
        for plain_span in annotation.get("spans") or []:
            path = plain_span["path"]
            record = next((r for r in records if r["placement"] == "body" and r["path"] is not None
                           and path[:len(r["path"])] == r["path"]), None)
            leaf = positions.leaf_at(http_message, "body", record["path"] if record else path)
            if leaf is None:
                continue
            # a value that is still readable at its place (also inside a JSON document that was only
            # serialised into a string) keeps a span of its own
            readable = positions.occurrences(leaf, value)
            if readable:
                spans.extend({**occurrence, "form": "plain"} for occurrence in readable)
            elif record is not None:
                span = whole_leaf(leaf, "encoded" if record["_plain"] == value else "container")
                span["encodings"] = list(reversed(record["codecs"]))
                spans.append(span)
    else:
        # headers, path, query and unstructured bodies: the value itself or its encoded token
        hiding = [r for r in records if r["placement"] == placement and r.get("encoded") and value in r["_plain"]]
        for leaf in positions.component_leaves(http_message, placement):
            for record in hiding:
                form = "encoded" if record["_plain"] == value else "container"
                spans.extend(
                    {**occurrence, "form": form, "encodings": list(reversed(record["codecs"]))}
                    for occurrence in positions.occurrences(leaf, record["encoded"])
                )
        located, _ = positions.locate(http_message, placement, value, annotation.get("field"))
        spans.extend({**span, "form": "plain"} for span in located)

    unique = []
    for span in spans:
        span.pop("whole", None)
        if span not in unique:
            unique.append(span)
    return unique


def encode_message(message: dict, profile: dict = None, rng: random.Random = None) -> dict:
    """
    Return an encoded copy of a plaintext message.

    The copy has an `encoding` field that records what was applied, and each annotation gets
    - `visible`: whether the plaintext value can still be read at one of its annotated places
      (the URL encoding of a query string or form body and JSON escaping do not hide a value),
    - `encodings`: the codecs that hide the value, outermost first (empty if visible),
    - `spans`: where the value is in the transmitted message (see _transmitted_spans),
    - `encoded_value`: the encoded form of the value, if the value was encoded on its own and
      this form occurs in the transmitted message (like `value`, it is given without the URL
      encoding of a query string or form-encoded body).
    """
    profile = profile or DEFAULT_PROFILE
    rng = rng or random.Random()
    encoded = copy.deepcopy(message)
    http_key = _http_key(encoded)
    http_message = encoded[http_key]
    kind = "request" if http_key == "http_request" else "response"
    annotations = [a for a in encoded.get("pii_annotations", []) if isinstance(a, dict)]
    # the positions in the plaintext message say which values to follow through the encoding
    if any("spans" not in a for a in annotations):
        positions.resolve_positions(encoded, http_key)

    value_mode = _choose(rng, profile[kind]["value_layer"])
    body_mode = _choose(rng, profile[kind]["body_layer"])
    has_body = isinstance(http_message.get("body"), str) and bool(http_message["body"].strip())

    records = _apply_header_query_layer(http_message, annotations, profile, rng)
    if has_body and value_mode != "none":
        records += _apply_value_layer(http_message, annotations, value_mode, profile, rng)
    body_record = _apply_body_layer(http_message, body_mode, rng) if has_body else None
    if body_record:
        body_mode = body_record["codecs"][0] if body_record["binary"] else "encoded"
    if has_body and not body_record and any(r["placement"] == "body" for r in records):
        _set_header(http_message, "Content-Length", str(len(http_message["body"].encode("utf-8"))),
                    only_if_present=True)

    # how each annotated value appears in the transmitted message
    for annotation in annotations:
        value, placement = annotation.get("value"), annotation.get("placement")
        located = bool(annotation.get("spans"))  # the plaintext message says where the value is
        spans = _transmitted_spans(http_message, annotation, records, body_record)
        # A value is visible if it can be read at (one of) its annotated places. For a value
        # without known place, any readable occurrence counts.
        if located:
            visible = any(span["form"] == "plain" for span in spans)
        else:
            visible = _visible(http_message, placement, value)

        layers, encoded_value = [], None
        if not visible and isinstance(value, str):
            hidden = [span for span in spans if span["form"] != "plain"]
            if hidden:
                layers = list(hidden[0]["encodings"])
                own = next((span for span in hidden if span["form"] == "encoded"), None)
                if own:
                    encoded_value = positions.span_value(http_message, placement, own)
            else:
                inner = [r for r in records if r["placement"] == placement and _contains(r, value)]
                if inner:
                    # the innermost transformation that contains the value
                    layers = list(reversed(min(inner, key=lambda r: len(r["_plain"]))["codecs"]))
            if placement == "body" and body_record:
                layers = list(reversed(body_record["codecs"])) + layers
        annotation["visible"] = visible
        annotation["encodings"] = layers
        if encoded_value is not None:
            annotation["encoded_value"] = encoded_value
        annotation["spans"] = spans

    for record in records:
        del record["_plain"]
        record.pop("_leaves", None)
    encoded["encoding"] = {
        "value_layer": value_mode if any(r["placement"] == "body" for r in records) else "none",
        "body_layer": body_mode if body_record else "none",
        "body": body_record,
        "values": records,
    }
    return encoded


def decode_message(message: dict) -> dict:
    """Restore the plaintext message from an encoded message (inverse of encode_message)."""
    decoded = copy.deepcopy(message)
    http_key = _http_key(decoded)
    http_message = decoded[http_key]
    encoding = decoded.pop("encoding", None) or {}

    body_record = encoding.get("body")
    if body_record:
        body = http_message["body"]
        if body_record["binary"]:
            data = base64.b64decode(body)
            for codec in reversed(body_record["codecs"]):
                data = _decode_bytes(data, codec)
            http_message["body"] = data.decode("utf-8")
            http_message.pop("body_is_base64", None)
            if body_record.get("header_added", True):
                http_message["headers"] = [
                    h for h in http_message.get("headers", [])
                    if not (isinstance(h, dict) and str(h.get("key", "")).lower() == "content-encoding")
                ]
        else:
            wrapper = body_record.get("wrapper")
            if wrapper and wrapper["type"] == "json":
                body = json.loads(body)[wrapper["key"]]
            elif wrapper and wrapper["type"] == "form":
                body = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))[wrapper["key"]]
            http_message["body"] = decode_text(body, body_record["codecs"])
            content_type = _header(http_message, "content-type")
            if content_type is not None:
                if body_record.get("content_type") is None:
                    http_message["headers"].remove(content_type)
                else:
                    content_type["value"] = body_record["content_type"]

    body_records = [r for r in encoding.get("values", []) if r["placement"] == "body"]
    if body_records:
        body = http_message["body"]
        located = [r for r in body_records if r["path"] is not None]
        if located and _body_format(http_message) == "json":
            document = _load_json(body)
            for record in located:
                _set(document, record["path"],
                     _decode_node(_get(document, record["path"]), record["codecs"], record["was_string"]))
            body = _dump_json(document, body)
        elif located:
            pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
            for record in located:
                key, value = pairs[record["path"][0]]
                pairs[record["path"][0]] = (key, decode_text(value, record["codecs"]))
            body = urllib.parse.urlencode(pairs)
        for record in body_records:
            if record["path"] is None:
                body = body.replace(record["encoded"], decode_text(record["encoded"], record["codecs"]))
        http_message["body"] = body

    for record in encoding.get("values", []):
        plain = decode_text(record["encoded"], record["codecs"]) if record.get("encoded") else None
        if record["placement"] == "query":
            base, query = http_message["path"].split("?", 1)
            pairs = [(k, plain if v == record["encoded"] else v)
                     for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)]
            http_message["path"] = base + "?" + urllib.parse.urlencode(pairs)
        elif record["placement"] == "header":
            for header in http_message.get("headers", []):
                if isinstance(header, dict) and header.get("value") == record["encoded"]:
                    header["value"] = plain

    if encoding.get("body") or any(r["placement"] == "body" for r in encoding.get("values", [])):
        _set_header(http_message, "Content-Length", str(len((http_message.get("body") or "").encode("utf-8"))),
                    only_if_present=True)

    for annotation in decoded.get("pii_annotations", []):
        if isinstance(annotation, dict):
            for key in ("visible", "encodings", "encoded_value"):
                annotation.pop(key, None)
    # the spans of the plaintext message
    positions.resolve_positions(decoded, http_key)
    return decoded


def _canonical(http_message: dict) -> tuple[Any, ...]:
    """A representation of a message that ignores formatting that encoding does not preserve."""
    body = http_message.get("body") or ""
    body_format = _body_format(http_message)
    if body_format == "json":
        body = _dumps(_load_json(body))
    elif body_format == "form":
        body = urllib.parse.parse_qsl(body, keep_blank_values=True)
    path = http_message.get("path")
    if isinstance(path, str) and "?" in path:
        base, query = path.split("?", 1)
        path = (base, tuple(urllib.parse.parse_qsl(query, keep_blank_values=True)))
    # Content-Length changes with the encoding; an unusable Content-Encoding header is dropped
    headers = [
        (h.get("key"), h.get("value")) for h in http_message.get("headers") or []
        if isinstance(h, dict) and str(h.get("key", "")).lower() not in ("content-length", "content-encoding")
    ]
    return path, headers, body


def verify_message(plain: dict, encoded: dict) -> list[str]:
    """Check that an encoded message can be decoded to the plaintext message and that its annotations hold."""
    errors = []
    http_key = _http_key(plain)
    prefix = f"Message {plain.get('id')}"
    try:
        decoded = decode_message(encoded)
        if _canonical(decoded[http_key]) != _canonical(plain[http_key]):
            errors.append(f"{prefix}: Decoding does not restore the plaintext message")
    except Exception as e:
        errors.append(f"{prefix}: Decoding failed ({type(e).__name__}: {e})")

    serialised = json.dumps(encoded[http_key], ensure_ascii=False)
    for annotation in encoded.get("pii_annotations", []):
        value, placement = annotation.get("value"), annotation.get("placement")
        spans = annotation.get("spans") or []
        if _has_spans(plain, annotation):
            present = any(span["form"] == "plain" for span in spans)
        else:
            present = _visible(encoded[http_key], placement, value)
        if annotation.get("visible") != present:
            errors.append(f"{prefix}: Annotation '{value}' has a wrong visibility flag")
        # (an annotation that the plaintext message cannot locate is not visible there either)
        if not annotation.get("visible") and not annotation.get("encodings") and _has_spans(plain, annotation):
            errors.append(f"{prefix}: Hidden annotation '{value}' has no encodings")
        if "encoded_value" in annotation and annotation["encoded_value"] not in serialised \
                and urllib.parse.quote_plus(annotation["encoded_value"]) not in serialised:
            errors.append(f"{prefix}: Encoded value of '{value}' does not occur in the message")
        for span in annotation.get("spans") or []:
            # plain spans hold the value; encoded spans are checked by decoding the message above
            text = positions.span_value(encoded[http_key], placement, span)
            if (span["form"] == "plain" and text != value) or not text:
                errors.append(f"{prefix}: A span of '{value}' holds '{text[:40]}'")
        # a value that the plaintext message locates stays locatable, unless the whole body hides it
        body_hidden = placement == "body" and (encoded.get("encoding") or {}).get("body")
        if not annotation.get("spans") and not body_hidden and _has_spans(plain, annotation):
            errors.append(f"{prefix}: Annotation '{value}' lost its position")
    return errors


def _has_spans(plain: dict, annotation: dict) -> bool:
    """Whether the plaintext message locates the annotation (messages of older runs may not)."""
    if any("spans" not in a for a in plain.get("pii_annotations", []) if isinstance(a, dict)):
        plain = copy.deepcopy(plain)
        positions.resolve_positions(plain, _http_key(plain))
    return any(
        a.get("spans") for a in plain.get("pii_annotations", [])
        if a.get("value") == annotation.get("value") and a.get("placement") == annotation.get("placement")
    )


# --------------------------------------------------------------------------- datasets

def encode_messages(messages: list[dict], seed: int = 0, profile: dict = None) -> tuple[list[dict], list[str]]:
    """Encode a list of messages. Each message is encoded independently of the others."""
    encoded_messages, errors = [], []
    for message in messages:
        kind = _http_key(message)
        # annotations carry decoded values (a no-op for messages of the current generator)
        message = copy.deepcopy(message)
        message_validation.normalise_annotation_encoding(message, kind)
        if any("spans" not in a for a in message.get("pii_annotations", []) if isinstance(a, dict)):
            positions.resolve_positions(message, kind)  # messages of runs without positions
        rng = random.Random(f"{seed}:encoding:{kind}:{message.get('id')}")
        encoded = encode_message(message, profile, rng)
        problems = verify_message(message, encoded)
        if problems:
            # an encoding that cannot be verified is not used; the message stays plaintext
            errors.extend(problems)
            encoded = encode_message(message, _plain_profile(profile or DEFAULT_PROFILE), rng)
        encoded_messages.append(encoded)
    return encoded_messages, errors


def _plain_profile(profile: dict) -> dict:
    plain = copy.deepcopy(profile)
    for kind in ("request", "response"):
        plain[kind] = {"value_layer": {"none": 1.0}, "body_layer": {"none": 1.0}}
    plain["header_query_rate"] = 0.0
    return plain


def encoding_report(messages: list[dict]) -> dict:
    """Distribution of the encodings in a set of encoded messages."""
    with_body = [m for m in messages if (m[_http_key(m)].get("body") or "")]
    kinds, codecs = Counter(), Counter()
    for message in with_body:
        encoding = message.get("encoding") or {}
        value_layer, body_layer = encoding.get("value_layer", "none"), encoding.get("body_layer", "none")
        if body_layer in COMPRESSION_CODECS:
            kinds["compressed body"] += 1
        elif body_layer == "encoded":
            kinds["encoded body"] += 1
        elif value_layer == "compressed":
            kinds["compressed values"] += 1
        elif value_layer == "encoded":
            kinds["encoded values"] += 1
        else:
            kinds["plaintext"] += 1
        if body_layer != "none" and value_layer != "none":
            kinds["(of which: encoded or compressed values inside an encoded or compressed body)"] += 1
        for record in [encoding.get("body")] + (encoding.get("values") or []):
            if record:
                codecs["+".join(record["codecs"])] += 1

    annotations = [a for m in messages for a in m.get("pii_annotations", [])]
    visible = sum(1 for a in annotations if a.get("visible"))
    span_forms = Counter(s["form"] for a in annotations for s in a.get("spans") or [])
    return {
        "messages": len(messages),
        "messages_with_body": len(with_body),
        "body_kinds": {k: v for k, v in kinds.most_common()},
        "body_kind_shares": {k: round(v / max(1, len(with_body)), 3) for k, v in kinds.most_common()},
        "codec_chains": dict(codecs.most_common()),
        "annotations": len(annotations),
        "annotations_visible": visible,
        "annotations_hidden": len(annotations) - visible,
        "annotations_with_encoded_value": sum(1 for a in annotations if "encoded_value" in a),
        "annotations_without_span": sum(1 for a in annotations if not a.get("spans")),
        "spans": dict(span_forms),
    }


def load_profile(path: str = None) -> dict:
    """Load an encoding profile; values missing in the file are taken from the default profile."""
    profile = copy.deepcopy(DEFAULT_PROFILE)
    if path:
        with open(path, encoding="utf-8") as f:
            custom = json.load(f)
        for key, value in custom.items():
            if isinstance(value, dict) and isinstance(profile.get(key), dict):
                profile[key].update(value)
            else:
                profile[key] = value
    return profile


def encode_run(output_folder: str, seed: int = 0, profile: dict = None) -> dict:
    """Encode the validated messages of a run and write them to 7_encoded/."""
    out_folder = os.path.join(output_folder, "7_encoded")
    os.makedirs(out_folder, exist_ok=True)
    summary = {"seed": seed, "profile": profile or DEFAULT_PROFILE}

    for kind, folder in (("requests", "4_requests_validated"), ("responses", "6_responses_validated")):
        source = os.path.join(output_folder, folder, f"{kind}_validated.json")
        target = os.path.join(out_folder, f"{kind}_encoded.json")
        if not os.path.exists(source):
            if os.path.exists(target):
                os.remove(target)
            continue
        with open(source, encoding="utf-8") as f:
            messages = json.load(f)
        encoded, errors = encode_messages(messages, seed, profile)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(encoded, f, indent=2, ensure_ascii=False)
        with open(os.path.join(out_folder, f"{kind}_encoded.jsonl"), "w", encoding="utf-8") as f:
            for message in encoded:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")
        summary[kind] = encoding_report(encoded)
        summary[kind]["verification_errors"] = errors

    with open(os.path.join(out_folder, "encoding_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.encoding", allow_abbrev=False,
        description="Applies encodings and compression to the validated messages of an existing run.")
    parser.add_argument("run_folder", metavar="RUN_FOLDER", help="folder of the run, e.g. ../output/<run id>")
    parser.add_argument("-seed", type=int, metavar="N", default=0, help="seed for sampling the encodings (default: 0)")
    parser.add_argument("-profile", metavar="PATH", help="JSON file with the shares of the encodings")
    args = parser.parse_args(argv or ["-h"])
    util.setup_logging()

    run_folder = args.run_folder
    output_folder = os.path.join(run_folder, "output") if os.path.isdir(os.path.join(run_folder, "output")) else run_folder
    seed = args.seed
    profile = load_profile(args.profile)

    summary = encode_run(output_folder, seed, profile)
    for kind in ("requests", "responses"):
        if kind in summary:
            report = summary[kind]
            logger.info(f"{kind}: {report['messages']} messages, {report['messages_with_body']} with body")
            logger.info(f"  body kinds: {report['body_kinds']}")
            logger.info(f"  annotations: {report['annotations_visible']} visible, {report['annotations_hidden']} hidden")
            if report["verification_errors"]:
                logger.info(f"  verification errors: {len(report['verification_errors'])}")
    logger.info(f"Written to {os.path.join(output_folder, '7_encoded')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
