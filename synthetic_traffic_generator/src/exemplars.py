"""
Exemplar pool of real-world HTTP messages.

The pool is built from one or more traffic databases (SQLite, with `Request` and `Response`
tables) and is used to ground each generated message in the structure and size of a real one.

Build the pool:
    python -m src.exemplars build real_data/batch1-manual.db real_data/batch1-auto.db

This writes exemplars_requests.jsonl, exemplars_responses.jsonl and profile.json to the
output folder (default: real_data/).
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable

from src import util

logger = logging.getLogger(__name__)

REQUESTS_FILE = "exemplars_requests.jsonl"
RESPONSES_FILE = "exemplars_responses.jsonl"
PROFILE_FILE = "profile.json"

MIN_BODY_CHARS = 40
MAX_BODY_CHARS = 20000
MAX_PER_SIGNATURE = 3       # exemplars kept per structurally identical endpoint
MAX_BODYLESS_REQUESTS = 600

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
FORM_PATTERN = re.compile(r"^[^=&\s]+=[^&\s]*(&[^=&\s]+=[^&\s]*)*$")
HEADER_NAME_PATTERN = re.compile(r"^([A-Za-z0-9!#$%&'*+.^_`|~-]+):\s?(.*)$", re.S)


# --------------------------------------------------------------------------- parsing helpers

def is_text(body: str) -> bool:
    """Whether a stored body is readable text (the databases store binary and compressed bodies as mangled text)."""
    if not body or "�" in body or "\x00" in body:
        return False
    sample = body[:4000]
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
    return printable / len(sample) >= 0.98


def classify_format(body: str) -> str:
    """Classify the serialisation format of a text body."""
    stripped = body.strip()
    if not stripped:
        return "none"
    if stripped[0] in "{[":
        try:
            json.loads(stripped)
            return "json"
        except ValueError:
            pass
    if stripped.startswith("--") and "Content-Disposition" in stripped:
        return "multipart"
    if stripped.startswith("<"):
        return "xml"
    if "=" in stripped and FORM_PATTERN.match(stripped):
        return "form"
    return "text"


def _json_leaves(value, depth=1):
    """Yield (leaf_value, depth) for all leaves of a parsed JSON value."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _json_leaves(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _json_leaves(v, depth + 1)
    else:
        yield value, depth


def body_shape(body: str, body_format: str) -> dict[str, int | None]:
    """Number of fields and nesting depth of a body (None if not applicable to the format)."""
    if body_format == "json":
        leaves = list(_json_leaves(json.loads(body), depth=0))
        return {"n_fields": len(leaves), "depth": max((d for _, d in leaves), default=0)}
    if body_format == "form":
        return {"n_fields": body.count("&") + 1, "depth": 1}
    return {"n_fields": None, "depth": None}


def body_values(body: str, body_format: str = None) -> list[str]:
    """The values (not the keys) of a body, as strings."""
    body_format = body_format or classify_format(body)
    if body_format == "json":
        try:
            return [str(v) for v, _ in _json_leaves(json.loads(body)) if v is not None and not isinstance(v, bool)]
        except ValueError:
            return [body]
    if body_format == "form":
        return [urllib.parse.unquote_plus(v) for _, v in urllib.parse.parse_qsl(body, keep_blank_values=False)]
    return [body]


def _key_signature(body: str, body_format: str) -> str:
    if body_format == "json":
        parsed = json.loads(body)
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed and isinstance(parsed[0], dict) else {}
        return ",".join(sorted(parsed)) if isinstance(parsed, dict) else ""
    if body_format == "form":
        return ",".join(sorted(k for k, _ in urllib.parse.parse_qsl(body, keep_blank_values=True)))
    return str(len(body) // 200)


def _path_shape(path: str) -> str:
    path = (path or "").split("?", 1)[0]
    return re.sub(r"[0-9a-fA-F-]{6,}|\d+", "#", path)


def parse_headers(raw: str, known_names: Iterable[str] = ()) -> list[dict[str, str]]:
    """
    Parse the stored header block into key-value pairs.

    The databases sometimes store the first two headers without a line break in between
    (e.g. 'Content-Type: application/jsonAccept: application/json'). Such lines are split
    at the longest known header name that directly follows a value.
    """
    headers = []
    for line_idx, line in enumerate((raw or "").replace("\r", "").split("\n")):
        match = HEADER_NAME_PATTERN.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2)

        if line_idx == 0 and known_names:
            best = None
            for name in known_names:
                pos = value.find(name + ": ")
                if pos > 0 and not value[pos - 1].isspace() and (best is None or len(name) > len(best[0])):
                    best = (name, pos)
            if best:
                name, pos = best
                headers.append({"key": key, "value": value[:pos]})
                key, value = name, value[pos + len(name) + 2:]

        headers.append({"key": key, "value": value})
    return headers


def _known_header_names(connection, table: str) -> list[str]:
    """Header names that occur at the start of a (well-formed) line in the given table."""
    names = Counter()
    for (raw,) in connection.execute(f"SELECT headers FROM {table} LIMIT 20000"):
        for line in (raw or "").split("\n")[1:]:
            match = HEADER_NAME_PATTERN.match(line.strip())
            if match:
                names[match.group(1)] += 1
    return [name for name, count in names.items() if count >= 2]


def _header_value(headers: list[dict[str, str]], name: str) -> str:
    for header in headers:
        if header["key"].lower() == name:
            return header["value"]
    return ""


# --------------------------------------------------------------------------- pool building

def _quantiles(values: list[int]) -> dict[str, int]:
    if not values:
        return {}
    ordered = sorted(values)
    pick = lambda p: ordered[int(p * (len(ordered) - 1))]
    return {"n": len(ordered), "p10": pick(.1), "p25": pick(.25), "p50": pick(.5),
            "p75": pick(.75), "p90": pick(.9), "p99": pick(.99), "max": ordered[-1]}


def build_pool(db_paths: list[str], out_dir: str) -> dict:
    """Build the exemplar pool and the traffic profile from the given databases."""
    requests, responses = [], []
    signature_counts = Counter()
    bodyless_signatures = set()
    stats = defaultdict(list)
    counts = Counter()

    for db_path in db_paths:
        source = os.path.splitext(os.path.basename(db_path))[0]
        # immutable: never write to the traffic database (not even WAL/SHM side files)
        connection = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        connection.text_factory = lambda b: b.decode("utf-8", errors="replace")

        kept_requests = {}  # request row id -> exemplar, to pair requests with their real responses
        known = _known_header_names(connection, "Request")
        query = "SELECT id, method, remoteHost, remotePath, headers, content, isTracker FROM Request ORDER BY id"
        for row_id, method, host, path, raw_headers, body, is_tracker in connection.execute(query):
            counts["requests_total"] += 1
            method = (method or "").upper()
            if method not in HTTP_METHODS:
                continue
            counts[f"method:{method}"] += 1
            headers = parse_headers(raw_headers, known)
            body = body or ""
            has_body = len(body.strip()) >= MIN_BODY_CHARS

            if not has_body:
                counts["requests_without_body"] += 1
                signature = (host, _path_shape(path))
                if (method == "GET" and signature not in bodyless_signatures
                        and len(bodyless_signatures) < MAX_BODYLESS_REQUESTS and is_text(path or "/")):
                    bodyless_signatures.add(signature)
                    requests.append({
                        "id": f"{source}:req:{row_id}", "kind": "request", "method": method, "path": path or "/",
                        "headers": headers, "content_type": "", "format": "none", "body": "", "length": 0,
                        "n_fields": None, "depth": None, "is_tracker": bool(is_tracker),
                    })
                    kept_requests[row_id] = requests[-1]
                continue

            counts["requests_with_body"] += 1
            if not is_text(body):
                counts["requests_binary"] += 1
                continue
            stats["request_length"].append(len(body))
            stats["request_headers"].append(len(headers))
            if len(body) > MAX_BODY_CHARS:
                counts["requests_too_long"] += 1
                continue

            body_format = classify_format(body)
            signature = ("req", host, _path_shape(path), body_format, _key_signature(body, body_format))
            signature_counts[signature] += 1
            if signature_counts[signature] > MAX_PER_SIGNATURE:
                continue

            content_type = _header_value(headers, "content-type").split(";")[0].strip().lower()
            stats[f"request_length:{body_format}"].append(len(body))
            requests.append({
                "id": f"{source}:req:{row_id}", "kind": "request", "method": method, "path": path or "/",
                "headers": headers, "content_type": content_type, "format": body_format, "body": body,
                "length": len(body), "is_tracker": bool(is_tracker), **body_shape(body, body_format),
                "_signature": signature,
            })
            kept_requests[row_id] = requests[-1]

        known = _known_header_names(connection, "Response")
        query = ("SELECT id, requestId, statusCode, statusMsg, headers, content, isTracker "
                 "FROM Response ORDER BY id")
        for row_id, request_id, status_code, reason, raw_headers, body, is_tracker in connection.execute(query):
            counts["responses_total"] += 1
            counts[f"status:{status_code}"] += 1
            body = body or ""
            headers = parse_headers(raw_headers, known)
            content_type = _header_value(headers, "content-type").split(";")[0].strip().lower()
            text = is_text(body) if body.strip() else True

            # what the real server answered to a request exemplar (even if the body itself is not usable)
            paired = kept_requests.get(request_id)
            if paired is not None and "response" in paired:
                paired = None  # only the first response of a request is used
            if paired is not None:
                paired["response"] = {
                    "status_code": status_code, "reason": reason or "", "content_type": content_type,
                    "header_names": [h["key"] for h in headers], "exemplar_id": None,
                    "body": "binary" if not text else ("none" if not body.strip() else "text"),
                    "short_body": body.strip() if text and 0 < len(body.strip()) < MIN_BODY_CHARS else None,
                }

            if len(body.strip()) < MIN_BODY_CHARS:
                counts["responses_without_body"] += 1
                continue
            if not text:
                counts["responses_binary"] += 1
                continue
            stats["response_length"].append(len(body))
            stats["response_headers"].append(len(headers))
            if len(body) > MAX_BODY_CHARS:
                if paired is not None:
                    paired["response"]["body"] = "long text"
                continue

            body_format = classify_format(body)
            signature = ("res", content_type, body_format, _key_signature(body, body_format))
            signature_counts[signature] += 1
            # responses of request exemplars are always kept, so that the pair stays intact
            if signature_counts[signature] > MAX_PER_SIGNATURE and paired is None:
                continue

            stats[f"response_length:{body_format}"].append(len(body))
            responses.append({
                "id": f"{source}:res:{row_id}", "kind": "response", "status_code": status_code,
                "reason": reason or "", "headers": headers, "content_type": content_type, "format": body_format,
                "body": body, "length": len(body), "is_tracker": bool(is_tracker),
                **body_shape(body, body_format), "_signature": signature,
                "request_id": paired["id"] if paired is not None else None,
            })
            if paired is not None:
                paired["response"]["exemplar_id"] = responses[-1]["id"]

        connection.close()

    # An exemplar stands for all messages with its signature: its weight restores the frequencies
    # of the real traffic that the de-duplication removed
    for exemplar in requests + responses:
        signature = exemplar.pop("_signature", None)
        occurrences = signature_counts.get(signature, 1)
        exemplar["weight"] = round(occurrences / min(occurrences, MAX_PER_SIGNATURE), 2)

    total_requests = max(1, counts["requests_with_body"] + counts["requests_without_body"])
    profile = {
        "sources": [os.path.basename(p) for p in db_paths],
        "requests": {
            "total": counts["requests_total"],
            "share_with_body": round(counts["requests_with_body"] / total_requests, 3),
            "share_of_bodies_binary": round(counts["requests_binary"] / max(1, counts["requests_with_body"]), 3),
            "methods": {k.split(":", 1)[1]: v for k, v in counts.most_common() if k.startswith("method:")},
            "body_length": _quantiles(stats["request_length"]),
            "body_length_by_format": {
                k.split(":", 1)[1]: _quantiles(v) for k, v in stats.items() if k.startswith("request_length:")
            },
            "headers_per_message": _quantiles(stats["request_headers"]),
            "exemplars": len(requests),
            "exemplar_formats": dict(Counter(e["format"] for e in requests)),
            "exemplars_with_real_response": sum(1 for e in requests if e.get("response")),
            "exemplars_with_text_response_exemplar": sum(
                1 for e in requests if (e.get("response") or {}).get("exemplar_id")
            ),
        },
        "responses": {
            "total": counts["responses_total"],
            "share_with_text_body": round(len(stats["response_length"]) / max(1, counts["responses_total"]), 3),
            "status_codes": {k.split(":", 1)[1]: v for k, v in counts.most_common(12) if k.startswith("status:")},
            "body_length": _quantiles(stats["response_length"]),
            "body_length_by_format": {
                k.split(":", 1)[1]: _quantiles(v) for k, v in stats.items() if k.startswith("response_length:")
            },
            "headers_per_message": _quantiles(stats["response_headers"]),
            "exemplars": len(responses),
            "exemplar_formats": dict(Counter(e["format"] for e in responses)),
        },
    }

    os.makedirs(out_dir, exist_ok=True)
    for filename, exemplars in ((REQUESTS_FILE, requests), (RESPONSES_FILE, responses)):
        with open(os.path.join(out_dir, filename), "w", encoding="utf-8") as f:
            for exemplar in exemplars:
                f.write(json.dumps(exemplar, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, PROFILE_FILE), "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    return profile


# --------------------------------------------------------------------------- pool loading

class ExemplarPool:
    """Exemplar pool loaded from a folder created by build_pool()."""

    def __init__(self, requests: list[dict] = None, responses: list[dict] = None, profile: dict = None):
        self.requests = requests or []
        self.responses = responses or []
        self.profile = profile or {}
        self._by_id = {e["id"]: e for e in self.requests + self.responses}

    def __bool__(self):
        return bool(self.requests or self.responses)

    def get(self, exemplar_id: str) -> dict | None:
        return self._by_id.get(exemplar_id)

    @classmethod
    def load(cls, pool_dir: str) -> "ExemplarPool":
        def read(filename):
            path = os.path.join(pool_dir or "", filename)
            if not pool_dir or not os.path.exists(path):
                return []
            with open(path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

        profile = {}
        profile_path = os.path.join(pool_dir or "", PROFILE_FILE)
        if pool_dir and os.path.exists(profile_path):
            with open(profile_path, encoding="utf-8") as f:
                profile = json.load(f)

        return cls(read(REQUESTS_FILE), read(RESPONSES_FILE), profile)

    @staticmethod
    def exists(pool_dir: str) -> bool:
        return bool(pool_dir) and (
            os.path.exists(os.path.join(pool_dir, REQUESTS_FILE))
            or os.path.exists(os.path.join(pool_dir, RESPONSES_FILE))
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.exemplars", allow_abbrev=False,
        description="Builds an exemplar pool of real-world messages from captured traffic.")
    parser.add_argument("command", choices=["build"], help="build: create the pool")
    parser.add_argument("databases", metavar="TRAFFIC_DB", nargs="+", help="database(s) with captured traffic")
    parser.add_argument("-out", metavar="PATH", default="real_data", help="folder of the pool (default: real_data)")
    args = parser.parse_args(argv or ["-h"])
    util.setup_logging()
    out_dir = args.out

    profile = build_pool(args.databases, out_dir)
    logger.info(f"Exemplar pool written to {out_dir}")
    for kind in ("requests", "responses"):
        logger.info(f"  {kind}: {profile[kind]['exemplars']} exemplars {profile[kind]['exemplar_formats']}")
        logger.info(f"    real body length: {profile[kind]['body_length']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
