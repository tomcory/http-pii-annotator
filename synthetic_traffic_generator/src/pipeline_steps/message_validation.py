"""
Shared validation checks for generated HTTP messages (requests and responses).
"""

import base64
import binascii
import html
import json
import re
import statistics
import urllib.parse
from collections.abc import Iterable

from src.blueprints import copy_tokens

REQUIRED_ANNOTATION_FIELDS = ["data_type", "placement", "value"]


def validate_annotation_structure(message: dict, valid_placements: set[str], prefix: str) -> list[str]:
    """Validate the structure of pii_annotations and data_types_present."""
    errors = []

    if "pii_annotations" in message:
        if not isinstance(message["pii_annotations"], list):
            errors.append(f"{prefix}: Field 'pii_annotations' must be an array")
        else:
            for ann_idx, ann in enumerate(message["pii_annotations"]):
                if not isinstance(ann, dict):
                    errors.append(f"{prefix}, annotation {ann_idx}: Must be an object")
                    continue
                for field in REQUIRED_ANNOTATION_FIELDS:
                    if field not in ann:
                        errors.append(f"{prefix}, annotation {ann_idx}: Missing '{field}'")
                if "placement" in ann and ann["placement"] not in valid_placements:
                    errors.append(f"{prefix}, annotation {ann_idx}: Invalid placement '{ann['placement']}'")
                if "value" in ann and (not isinstance(ann["value"], str) or not ann["value"]):
                    errors.append(f"{prefix}, annotation {ann_idx}: 'value' must be a non-empty string")

    if "data_types_present" in message and not isinstance(message["data_types_present"], list):
        errors.append(f"{prefix}: Field 'data_types_present' must be an array")

    return errors


def validate_headers(headers, prefix: str) -> list[str]:
    """Validate that headers are an array of key-value objects."""
    errors = []
    if not isinstance(headers, list):
        return [f"{prefix}: 'headers' must be an array"]
    for h_idx, header in enumerate(headers):
        if not isinstance(header, dict):
            errors.append(f"{prefix}, header {h_idx}: Must be an object")
        elif "key" not in header or "value" not in header:
            errors.append(f"{prefix}, header {h_idx}: Missing 'key' or 'value'")
    return errors


def validate_taxonomy(message: dict, taxonomy_labels: set[str], prefix: str) -> tuple[list[str], set[str]]:
    """Validate that all data_type values occur in the taxonomy."""
    errors = []
    labels_found = set()

    for ann_idx, ann in enumerate(message.get("pii_annotations", [])):
        if "data_type" in ann:
            data_type = ann["data_type"]
            if not isinstance(data_type, str) or data_type not in taxonomy_labels:
                errors.append(f"{prefix}, annotation {ann_idx}: Unknown data_type '{data_type}'")
            else:
                labels_found.add(data_type)

    for data_type in message.get("data_types_present", []):
        if not isinstance(data_type, str) or data_type not in taxonomy_labels:
            errors.append(f"{prefix}: Unknown data_type in data_types_present: '{data_type}'")
        else:
            labels_found.add(data_type)

    return errors, labels_found


def validate_negative_control(message: dict, prefix: str) -> list[str]:
    """Messages of negative-control scenarios must not carry any PII annotations."""
    if not message.get("negative_control", False):
        return []
    if message.get("pii_annotations") or message.get("data_types_present"):
        return [f"{prefix}: Negative control must not contain PII annotations"]
    return []


def _json_string_leaves(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _json_string_leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _json_string_leaves(v)


def _variants(text: str, is_body: bool = False) -> list[str]:
    """Return the text together with its URL- and HTML-decoded representations."""
    variants = [text, urllib.parse.unquote_plus(text), urllib.parse.unquote(text), html.unescape(text)]
    if is_body:
        # JSON bodies carry escaped strings (e.g. \\" or \\n); also search the decoded string values
        try:
            variants.extend(_json_string_leaves(json.loads(text)))
        except (ValueError, TypeError):
            pass
    return variants


def _header_texts(headers) -> list[str]:
    texts = []
    if isinstance(headers, list):
        for header in headers:
            if isinstance(header, dict) and isinstance(header.get("value"), str):
                texts.extend(_variants(header["value"]))
    return texts


def request_components(http_request: dict) -> dict[str, list[str]]:
    """Split an HTTP request into the searchable texts of each placement."""
    full_path = http_request.get("path", "")
    if not isinstance(full_path, str):
        full_path = ""
    path, _, query = full_path.partition("?")
    body = http_request.get("body")

    return {
        "path": _variants(path),
        "query": _variants(query) if query else [],
        "header": _header_texts(http_request.get("headers")),
        "body": _variants(body, is_body=True) if isinstance(body, str) and body else [],
    }


def response_components(http_response: dict) -> dict[str, list[str]]:
    """Split an HTTP response into the searchable texts of each placement."""
    body = http_response.get("body")
    return {
        "header": _header_texts(http_response.get("headers")),
        "body": _variants(body, is_body=True) if isinstance(body, str) and body else [],
    }


def validate_value_placement(message: dict, components: dict[str, list[str]], prefix: str) -> list[str]:
    """Validate that each annotated value appears verbatim in the message component named by its placement."""
    errors = []

    for ann_idx, ann in enumerate(message.get("pii_annotations", [])):
        value = ann.get("value")
        placement = ann.get("placement")
        if not isinstance(value, str) or placement not in components:
            continue  # reported by the structure validation

        if any(value in text for text in components[placement]):
            continue

        data_type = ann.get("data_type", "unknown")
        elsewhere = sorted(p for p, texts in components.items() if any(value in t for t in texts))
        if elsewhere:
            detail = f"not found in {placement} (found in {', '.join(elsewhere)})"
        else:
            detail = "not found in message"
        errors.append(f"{prefix}, annotation {ann_idx}: PII value '{value}' ({data_type}, {placement}) {detail}")

    return errors


def validate_blueprint(message: dict, http_message: dict, pool, prefix: str, min_length_ratio: float = 0.6) -> list[str]:
    """
    Validate a message against the blueprint it was generated from.

    - The body must reach the target length of the blueprint (within a tolerance).
    - Identifier-like values of the real-world exemplar must not be copied into the message.
    """
    blueprint = message.get("blueprint")
    if not isinstance(blueprint, dict):
        return []

    errors = []
    body = http_message.get("body")
    body = body if isinstance(body, str) else ""

    target = blueprint.get("target_body_length") or 0
    if target and len(body) < min_length_ratio * target:
        errors.append(
            f"{prefix}: Body is too short ({len(body)} characters, the blueprint targets about {target}); "
            f"add realistic non-PII fields as in the exemplar"
        )

    exemplar = pool.get(blueprint.get("exemplar_id")) if pool and blueprint.get("exemplar_id") else None
    if exemplar:
        serialised = json.dumps(http_message, ensure_ascii=False)
        copied = [token for token in copy_tokens(exemplar) if token in serialised or token in body]
        if copied:
            shown = ", ".join(f"'{token}'" for token in copied[:5])
            errors.append(
                f"{prefix}: Values copied from the real-world exemplar ({shown}); replace every exemplar value "
                f"with a newly invented one"
            )

    return errors


def _quantiles(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    pick = lambda p: ordered[int(p * (len(ordered) - 1))]
    return {"n": len(ordered), "p10": pick(.1), "p25": pick(.25), "p50": pick(.5),
            "p75": pick(.75), "p90": pick(.9), "max": ordered[-1]}


def _count_fields(body: str):
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        def leaves(value):
            if isinstance(value, dict):
                return sum(leaves(v) for v in value.values())
            if isinstance(value, list):
                return sum(leaves(v) for v in value)
            return 1
        return leaves(parsed)
    if "=" in body and "&" in body and " " not in body.strip():
        return body.count("&") + 1
    return None


def realism_report(messages: list[dict], http_key: str, real_profile: dict = None) -> dict:
    """
    Size and PII-density statistics of generated messages (negative controls excluded),
    next to the profile of the real traffic the exemplar pool was built from.
    """
    messages = [m for m in messages if not m.get("negative_control", False)]
    lengths, header_counts, char_shares, field_shares, field_counts = [], [], [], [], []

    for message in messages:
        http_message = message.get(http_key, {})
        headers = http_message.get("headers")
        header_counts.append(len(headers) if isinstance(headers, list) else 0)

        body = http_message.get("body")
        if not isinstance(body, str) or not body:
            continue
        lengths.append(len(body))

        body_annotations = [a for a in message.get("pii_annotations", []) if a.get("placement") == "body"]
        char_shares.append(min(1.0, sum(len(a.get("value", "")) for a in body_annotations) / len(body)))
        fields = _count_fields(body)
        if fields:
            field_counts.append(fields)
            field_shares.append(min(1.0, len(body_annotations) / fields))

    report = {
        "messages": len(messages),
        "share_with_body": round(len(lengths) / max(1, len(messages)), 3),
        "body_length": _quantiles(lengths),
        "headers_per_message": _quantiles(header_counts),
        "fields_per_body": _quantiles(field_counts),
        "pii_char_share_median": round(statistics.median(char_shares), 3) if char_shares else None,
        "annotated_field_share_median": round(statistics.median(field_shares), 3) if field_shares else None,
    }
    if real_profile:
        report["real_traffic"] = {
            "share_with_body": real_profile.get("share_with_body", real_profile.get("share_with_text_body")),
            "body_length": real_profile.get("body_length"),
            "headers_per_message": real_profile.get("headers_per_message"),
        }
    return report


def _content_type(http_message: dict) -> str:
    headers = http_message.get("headers")
    if isinstance(headers, list):
        for header in headers:
            if isinstance(header, dict) and str(header.get("key", "")).lower() == "content-type":
                return str(header.get("value", "")).split(";")[0].strip().lower()
    return ""


def validate_body_format(http_message: dict, prefix: str) -> list[str]:
    """Validate that the body can be parsed as what the Content-Type header declares."""
    body = http_message.get("body")
    if not isinstance(body, str) or not body.strip():
        return []

    content_type = _content_type(http_message)
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            json.loads(body)
        except ValueError as e:
            return [f"{prefix}: Content-Type is {content_type}, but the body is not valid JSON ({e})"]
    elif content_type == "application/x-www-form-urlencoded":
        stripped = body.strip()
        if any(ch in stripped for ch in " \n{") or "=" not in stripped:
            return [f"{prefix}: Content-Type is {content_type}, but the body is not form-encoded"]
    return []


def validate_response_fit(response: dict, request: dict, prefix: str) -> list[str]:
    """
    Validate that a response fits its request as far as this can be checked mechanically.

    - PII values of the request that the response repeats must be annotated in the response as well.
    - A value must carry the same label in request and response.
    """
    errors = []
    http_response = response.get("http_response", {})
    components = response_components(http_response)
    texts = components["header"] + components["body"]

    annotated = {}
    for annotation in response.get("pii_annotations", []):
        if isinstance(annotation.get("value"), str):
            annotated.setdefault(annotation["value"], set()).add(annotation.get("data_type"))

    request_labels = {}
    for annotation in request.get("pii_annotations", []):
        if isinstance(annotation.get("value"), str):
            request_labels.setdefault(annotation["value"], set()).add(annotation.get("data_type"))

    missing, relabelled = [], []
    for value, labels in request_labels.items():
        if value in annotated:
            if not (annotated[value] & labels):
                relabelled.append((value, sorted(labels)[0], sorted(annotated[value])[0]))
            continue
        # short values (e.g. '35', 'en') occur by chance; only longer ones are checked
        if len(value) >= 5 and any(value in text for text in texts):
            # the value may be part of a longer annotated value (e.g. a name inside an email address)
            if not any(value in other for other in annotated):
                missing.append((value, sorted(labels)[0]))

    if missing:
        shown = ", ".join(f"'{value}' ({label})" for value, label in missing[:5])
        errors.append(f"{prefix}: PII values of the request are repeated in the response without annotation: {shown}")
    if relabelled:
        shown = ", ".join(f"'{v}' is {a} in the request but {b} in the response" for v, a, b in relabelled[:3])
        errors.append(f"{prefix}: Inconsistent labels between request and response: {shown}")
    return errors


HEX_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}(?:-?[0-9a-fA-F]{4}){3}-?[0-9a-fA-F]{12}(?![0-9A-Za-z])")


def _is_patterned(identifier: str) -> bool:
    """Whether a hexadecimal identifier follows a counting pattern (e.g. a1b2c3d4-e5f6-..., 8f7e6d5c-4b3a-...)."""
    digits = [int(ch, 16) for ch in identifier if ch != "-"]
    if len(set(digits)) <= 2:
        return True
    # a digit continues a count if it is one above or below the previous or the second-previous digit
    counting = 0
    for i in range(2, len(digits)):
        if (digits[i] - digits[i - 1]) % 16 in (1, 15) or (digits[i] - digits[i - 2]) % 16 in (1, 15):
            counting += 1
    return counting / (len(digits) - 2) >= 0.6


def validate_identifiers(http_message: dict, prefix: str) -> list[str]:
    """Validate that UUID-like identifiers look random instead of following a counting pattern."""
    serialised = json.dumps(http_message, ensure_ascii=False)
    patterned = sorted({m for m in HEX_ID_PATTERN.findall(serialised) if _is_patterned(m)})
    if not patterned:
        return []
    shown = ", ".join(f"'{identifier}'" for identifier in patterned[:3])
    return [f"{prefix}: Identifiers follow a counting pattern ({shown}); identifiers must look random"]


def normalise_annotation_encoding(message: dict, http_key: str) -> int:
    """
    Replace URL-encoded annotation values by their decoded form (e.g. 'Mara+Lindqvist' -> 'Mara Lindqvist').

    In query strings and form-encoded bodies, values are transmitted URL-encoded. LLMs annotate them
    sometimes in encoded and sometimes in decoded form. Annotations consistently carry the decoded
    value, which is what a PII detector that normalises the message will extract.

    Returns the number of annotations that were changed.
    """
    http_message = message.get(http_key)
    annotations = message.get("pii_annotations")
    if not isinstance(http_message, dict) or not isinstance(annotations, list):
        return 0

    encoded_components = {}
    path = http_message.get("path")
    if isinstance(path, str) and "?" in path:
        encoded_components["query"] = path.split("?", 1)[1]
    body = http_message.get("body")
    if isinstance(body, str) and _content_type(http_message) == "application/x-www-form-urlencoded":
        encoded_components["body"] = body

    changed = 0
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        value, component = annotation.get("value"), encoded_components.get(annotation.get("placement"))
        if not isinstance(value, str) or component is None or value not in component:
            continue
        decoded = urllib.parse.unquote_plus(value)
        if decoded != value and decoded.strip():
            annotation["value"] = decoded
            changed += 1
    return changed


BASE64_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_=-])")
HEX_TOKEN_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-fA-F]{2}){24,}(?![0-9A-Za-z])")
JWT_HEADER_KEYS = {"alg", "typ", "kid", "cty", "enc"}


FORM_DATA_PATTERN = re.compile(r"^[\w.\-\[\]%]+=[^&=\s]*(&[\w.\-\[\]%]+=[^&=\s]*)+$")
PROSE_PATTERN = re.compile(r"[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){3,}")


def _readable(data: bytes) -> str:
    """
    The document that the bytes encode, if they encode one (empty otherwise).

    Identifiers legitimately look encoded, and many of them are: opaque tokens, hashes, Base64
    of binary data, but also Base64 of short structured strings such as 'User:1234', pagination
    cursors or resource names. None of this is hidden data. Only an encoded *document* counts:
    JSON, form data, markup, or prose.
    """
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
    if len(text) < 12 or sum(ch.isprintable() or ch in "\r\n\t" for ch in text) / len(text) < 0.97:
        return ""

    if text[0] in "{[":
        try:
            if isinstance(json.loads(text), (dict, list)):
                return text
        except ValueError:
            pass
    if text[0] == "<" and re.search(r"</[A-Za-z][\w:.-]*>|/>\s*$", text):
        return text
    if FORM_DATA_PATTERN.match(text):
        return text
    if PROSE_PATTERN.search(text):
        return text
    return ""


def encoded_data(text: str) -> list[str]:
    """Substrings of a text that are Base64- or hex-encoded meaningful text, or serialised JSON documents."""
    found = []
    for token in BASE64_TOKEN_PATTERN.findall(text):
        try:
            data = base64.b64decode(token.replace("-", "+").replace("_", "/") + "=" * (-len(token) % 4))
        except (binascii.Error, ValueError):
            continue
        decoded = _readable(data)
        if not decoded:
            continue
        # the header of a JSON Web Token is the one piece of encoded JSON that tokens legitimately carry
        try:
            parsed = json.loads(decoded)
            if isinstance(parsed, dict) and set(parsed) <= JWT_HEADER_KEYS:
                continue
        except ValueError:
            pass
        found.append(token)
    for token in HEX_TOKEN_PATTERN.findall(text):
        try:
            if _readable(binascii.unhexlify(token)):
                found.append(token)
        except (binascii.Error, ValueError):
            continue
    return found


def validate_plaintext(http_message: dict, prefix: str) -> list[str]:
    """
    Validate that a generated message carries its data in plaintext.

    Encodings and compression are applied by a separate encoding layer, which records what it does.
    Data that the generator encodes itself would be invisible to it and to the annotations.
    """
    errors = []
    serialised = json.dumps(http_message, ensure_ascii=False)
    encoded = encoded_data(serialised)
    if encoded:
        shown = ", ".join(f"'{token[:40]}...'" for token in encoded[:3])
        errors.append(
            f"{prefix}: The message contains an encoded document ({shown}); write its content as plaintext "
            f"fields (identifiers and opaque tokens may look encoded, but must not decode to data)"
        )

    # JSON documents serialised into a string value (of a JSON or form-encoded body)
    body = http_message.get("body")
    nested = []
    if isinstance(body, str) and body.strip():
        try:
            values = list(_json_string_leaves(json.loads(body)))
        except (ValueError, TypeError):
            values = [v for _, v in urllib.parse.parse_qsl(body)] if "=" in body and " " not in body.strip() else []
        for value in values:
            stripped = value.strip()
            if len(stripped) >= 20 and stripped[0] in "{[":
                try:
                    if isinstance(json.loads(stripped), (dict, list)):
                        nested.append(stripped)
                except ValueError:
                    pass
    if nested:
        errors.append(
            f"{prefix}: The body contains a JSON document serialised into a string value "
            f"('{nested[0][:40]}...'); write it as regular fields of the body"
        )
    return errors
