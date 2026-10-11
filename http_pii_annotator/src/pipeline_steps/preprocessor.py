import base64
import binascii
import html
import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote

from src.pipeline_steps.pipeline_step import PipelineStep
from src.records import Message

NORMALIZATIONS = ("paper", "strict")

#: Message field that holds the body as it was received
ORIGINAL_BODY = 'original_body'

_PERCENT_ENCODED = re.compile(r'(?:%[0-9a-fA-F]{2})+')
_PERCENT_ENCODED_OR_PLUS = re.compile(r'\+|(?:%[0-9a-fA-F]{2})+')
# a character reference as html.unescape recognises it, i.e. also without its semicolon
_CHARACTER_REFERENCE = re.compile(r'&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)')
_TERMINATED_CHARACTER_REFERENCE = re.compile(r'&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);')

# A Base64 candidate: at least 16 characters of the standard or of the URL-safe alphabet, in complete groups of
# four with correct padding, and not part of a longer run of such characters
_BASE64_CANDIDATE = re.compile(
    r'(?<![A-Za-z0-9+/_-])'
    r'(?:(?:[A-Za-z0-9+/]{4}){4,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?'
    r'|(?:[A-Za-z0-9_-]{4}){4,}(?:[A-Za-z0-9_-]{2}==|[A-Za-z0-9_-]{3}=)?)'
    r'(?![A-Za-z0-9+/_=-])'
)
#: Share of printable characters that the decoded text of a Base64 candidate must reach
BASE64_MIN_PRINTABLE = 0.9


def looks_form_encoded(body: str) -> bool:
    """Whether a body is a list of key=value pairs rather than JSON, XML or free text."""
    stripped = body.lstrip()
    if not stripped or stripped[0] in '{[<"':
        return False
    first_pair = stripped.split('&', 1)[0]
    return '=' in first_pair and not any(c.isspace() for c in first_pair.split('=', 1)[0])


def decode_base64_text(candidate: str) -> str | None:
    """The text that a Base64 string encodes, or None if it does not decode to predominantly printable UTF-8 text."""
    try:
        decoded = base64.b64decode(candidate.replace('-', '+').replace('_', '/'), validate=True).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        return None
    printable = sum(1 for c in decoded if c.isprintable() or c in '\t\n\r')
    if not decoded or printable < BASE64_MIN_PRINTABLE * len(decoded):
        return None
    return decoded


@dataclass(frozen=True)
class _Replacement:
    """A span of the text before a decoding pass and the span that replaced it in the text after the pass."""
    old_start: int
    old_end: int
    new_start: int
    new_end: int


def _decode(text: str, pattern: re.Pattern, decode: Callable[[str], str | None],
            trim: bool = False) -> tuple[str, list[_Replacement]]:
    """
    Replaces the matches of the pattern by their decoded form and records what was replaced. With trim, text at
    the end of a match that the decoding left unchanged is not counted as replaced.
    """
    parts, replacements, position, offset = [], [], 0, 0
    for match in pattern.finditer(text):
        decoded = decode(match.group())
        if decoded is None or decoded == match.group():
            continue
        # a character reference without semicolon is matched together with the text that follows it, which is
        # not part of what was replaced
        encoded, end = match.group(), match.end()
        while trim and encoded and decoded and encoded[-1] == decoded[-1]:
            encoded, decoded, end = encoded[:-1], decoded[:-1], end - 1
        parts += [text[position:match.start()], decoded]
        new_start = match.start() + offset
        replacements.append(_Replacement(match.start(), end, new_start, new_start + len(decoded)))
        offset += len(decoded) - len(encoded)
        position = end
    parts.append(text[position:])
    return ''.join(parts), replacements


def _old_start(replacements: list[_Replacement], position: int) -> int:
    """
    The position in the text before a decoding pass at which the character at a position in the text after it
    begins. A character within a replaced span begins where the replaced text began.
    """
    index = bisect_right(replacements, position, key=lambda r: r.new_start) - 1
    if index < 0:
        return position
    replacement = replacements[index]
    if position < replacement.new_end:
        return replacement.old_start
    return replacement.old_end + (position - replacement.new_end)


def _old_end(replacements: list[_Replacement], position: int) -> int:
    """
    The position in the text before a decoding pass at which the character before a position in the text after it
    ends. A character within a replaced span ends where the replaced text ended.
    """
    index = bisect_right(replacements, position - 1, key=lambda r: r.new_start) - 1
    if index < 0:
        return position
    replacement = replacements[index]
    if position - 1 < replacement.new_end:
        return replacement.old_end
    return replacement.old_end + (position - replacement.new_end)


@dataclass(frozen=True)
class NormalizedBody:
    """
    A normalised message body together with the record of how it was derived from the original body.

    Attributes:
        text: The normalised body
        passes: The replacements of each decoding pass, in the order in which the passes were applied
    """
    text: str
    passes: tuple[list[_Replacement], ...] = field(default=(), compare=False)

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        """The span of the original body from which the span [start, end) of the normalised body was derived."""
        empty = start == end
        for replacements in reversed(self.passes):
            start = _old_start(replacements, start)
            end = start if empty else _old_end(replacements, end)
        return start, end


def normalize(body: str | None, normalization: str = "paper", decode_base64: bool = False) -> NormalizedBody:
    """
    Normalises a message body by decoding URL-encoded characters and HTML character references and, optionally,
    Base64-encoded substrings.

    Args:
        body: The message body; a missing body is treated as an empty body
        normalization:
            "paper" reproduces the pre-processing evaluated in the paper: every plus sign is decoded as a space,
            and character references are also decoded if their semicolon is missing. This alters bodies in
            unintended ways: the plus sign of a phone number in a JSON document becomes a space, and
            '&timestamp=' in a form-encoded body becomes '×tamp=', because '&times' is a character reference.
            "strict" decodes plus signs only in form-encoded bodies and only character references that end with
            a semicolon.
        decode_base64: Whether substrings that look like Base64 and decode to predominantly printable UTF-8 text
            are replaced by that text. The pass follows the other two, so with "paper" a Base64 string that
            contains a plus sign is no longer recognised.
    """
    text = body or ''
    passes = []

    if normalization == "paper" or looks_form_encoded(text):
        percent_pattern, percent_decode = _PERCENT_ENCODED_OR_PLUS, lambda s: ' ' if s == '+' else unquote(s)
    else:
        percent_pattern, percent_decode = _PERCENT_ENCODED, unquote
    reference_pattern = _CHARACTER_REFERENCE if normalization == "paper" else _TERMINATED_CHARACTER_REFERENCE

    steps = [(percent_pattern, percent_decode, False), (reference_pattern, html.unescape, True)]
    if decode_base64:
        steps.append((_BASE64_CANDIDATE, decode_base64_text, False))
    for pattern, decode, trim in steps:
        text, replacements = _decode(text, pattern, decode, trim)
        passes.append(replacements)
    return NormalizedBody(text, tuple(passes))


def normalize_body(body: str | None, normalization: str = "paper", decode_base64: bool = False) -> str:
    """The normalised form of a message body, see normalize()."""
    return normalize(body, normalization, decode_base64).text


def original_annotations(message: Message, normalization: str = "paper", decode_base64: bool = False) -> list | None:
    """
    Maps the annotations of a message, whose values refer to the normalised body, back to the original body.

    Returns one entry per occurrence of an annotated value in the normalised body, with the data type, the text
    of the original body from which the occurrence was derived ('value') and its position there ('start', 'end').
    A value that does not occur in the normalised body is returned as it is, without a position. Returns None if
    the message does not carry its original body or was normalised with other settings.
    """
    if ORIGINAL_BODY not in message.fields:
        return None
    original = message.fields[ORIGINAL_BODY] or ''
    normalized = normalize(original, normalization, decode_base64)
    if normalized.text != message.body:
        return None

    mapped = []
    for annotation in message.annotations or []:
        data_type, value = annotation.get('data_type'), annotation.get('value')
        entries, position = [], normalized.text.find(value) if value else -1
        while position >= 0:
            start, end = normalized.original_span(position, position + len(value))
            entries.append({'data_type': data_type, 'value': original[start:end], 'start': start, 'end': end})
            position = normalized.text.find(value, position + len(value))
        if not entries:
            entries.append({'data_type': data_type, 'value': value, 'start': None, 'end': None})
        mapped += [entry for entry in entries if entry not in mapped]
    return mapped


class Preprocessor(PipelineStep):
    """
    Normalises HTTP message bodies by decoding URL-encoded characters, HTML character references and, optionally,
    Base64-encoded substrings. The body as it was received is kept in the field 'original_body'.
    """
    task = 'preprocess'
    details = 'Prepares HTTP messages for annotation'
    output_files = ('preprocessed.jsonl',)

    def __init__(self, normalization: str = "paper", decode_base64: bool = False):
        self.normalization = normalization
        self.decode_base64 = decode_base64

    def process(self, message: Message) -> tuple[Message, ...]:
        original = message.fields.get('body')
        body = normalize_body(original, self.normalization, self.decode_base64)
        return (message.copy(fields={**message.fields, 'body': body, ORIGINAL_BODY: original or ''}),)
