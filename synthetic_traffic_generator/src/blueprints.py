"""
Per-message blueprints for request and response generation.

A blueprint specifies, for one message to generate, the size and structure to aim for
(grounded in a real-world exemplar if a pool is available) and the PII to embed. Blueprints
are sampled in code from a seeded random generator, so that the distribution of message
sizes, formats and PII placements is controlled by the pipeline rather than left to the LLM.
"""

import random
import re

from src.exemplars import ExemplarPool, body_values

# Fallback if no exemplar pool is available: (target body length, weight)
FALLBACK_LENGTHS = [(150, 0.15), (400, 0.25), (900, 0.25), (2000, 0.2), (4000, 0.1), (7000, 0.05)]

# Fallback if no exemplar pool is available: kinds of non-PII filler that real payloads carry
NOISE_PROFILES = [
    "analytics event envelope: batched events with names, timestamps, sequence numbers, screen names and properties",
    "device and app info block: OS and SDK versions, build numbers, screen metrics, connectivity, battery, locale",
    "feature flags and remote config: many boolean and string flags, experiment and variant assignments",
    "advertising/attribution SDK payload: campaign fields, install referrer, consent flags, SDK and bidding metadata",
    "performance and diagnostics: timings, counters, memory and network metrics, trace and span identifiers",
    "first-party business object: nested resource with metadata, timestamps, pagination, links and settings",
    "session state sync: preferences, UI state, cached resource versions, sync cursors and checksums",
]

# Diversity seeds: sampled per message, so that separately generated messages differ from each other
CLIENT_PROFILES = [
    "native Android app using OkHttp",
    "native Android app using HttpURLConnection (Dalvik user agent)",
    "native iOS app using URLSession (CFNetwork user agent)",
    "cross-platform app (React Native or Flutter)",
    "hybrid app sending requests from a WebView",
    "third-party SDK embedded in a mobile app (background request)",
    "mobile web browser",
]
PERSONA_LOCALES = [
    "en-US", "en-GB", "en-IN", "en-AU", "en-CA", "de-DE", "de-AT", "fr-FR", "fr-CA", "es-ES", "es-MX", "pt-BR",
    "it-IT", "nl-NL", "sv-SE", "nb-NO", "da-DK", "fi-FI", "pl-PL", "cs-CZ", "tr-TR", "el-GR", "ro-RO", "hu-HU",
    "ja-JP", "ko-KR", "zh-TW", "vi-VN", "id-ID", "th-TH", "hi-IN", "ar-EG", "he-IL", "sw-KE", "en-NG", "en-ZA",
]
INITIALS = "ABCDEFGHIJKLMNOPRSTUVWYZ"

CONTENT_TYPE_FORMATS = {
    "application/json": "json",
    "application/x-www-form-urlencoded": "form",
    "multipart/form-data": "multipart",
    "text/plain": "text",
    "application/xml": "xml",
    "text/xml": "xml",
}
BODY_METHODS = {"POST", "PUT", "PATCH"}
ERROR_STATUS_CODES = [400, 400, 401, 401, 403, 404, 404, 409, 422, 429, 500, 500, 502, 503]

# overhead per message in a generation call (HTTP structure, headers, annotations), in characters
MESSAGE_OVERHEAD_CHARS = 1200
# a scenario type restricts the exemplars only if at least this many exemplars fit it
MIN_FITTING_EXEMPLARS = 10
# tokens a call may spend on reasoning before it answers (if reasoning is not switched off)
REASONING_TOKENS = 16000
REASONING_TOKENS_OFF = 3000
# at most one planned PII value per this many characters of body
CHARS_PER_BODY_PII = 150
COPY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]{12,}")


class BlueprintSampler:
    """Samples blueprints for the messages of a scenario."""

    def __init__(
            self,
            pool: ExemplarPool = None,
            seed: int = None,
            max_body_chars: int = 8000,
            response_body_share: float = 0.75,
            error_response_share: float = 0.08,
    ):
        self.pool = pool or ExemplarPool()
        self.seed = seed if seed is not None else random.SystemRandom().randrange(2 ** 31)
        self.max_body_chars = max_body_chars
        self.response_body_share = response_body_share
        self.error_response_share = error_response_share

        self._request_exemplars = {}
        for exemplar in self.pool.requests:
            if exemplar["length"] <= max_body_chars:
                self._request_exemplars.setdefault(exemplar["format"], []).append(exemplar)
        self._response_exemplars = [e for e in self.pool.responses if e["length"] <= max_body_chars]

    @property
    def has_request_exemplars(self) -> bool:
        return bool(self._request_exemplars)

    def _rng(self, *key) -> random.Random:
        """Random generator that only depends on the seed and the key (independent of call order)."""
        return random.Random(":".join(str(part) for part in (self.seed, *key)))

    # ------------------------------------------------------------------ requests

    def request_blueprint(self, scenario: dict, index: int) -> dict:
        """Sample the blueprint of the index-th request of a scenario."""
        scenario_id = scenario.get("scenario_id", "unknown")
        rng = self._rng("request", scenario_id, index)
        negative_control = bool(scenario.get("negative_control", False))

        method = rng.choice(scenario.get("methods") or ["POST"]).upper()
        content_types = [c for c in (scenario.get("content_types") or []) if c and c != "(none)"]
        has_body = method in BODY_METHODS and bool(content_types)
        content_type = rng.choice(content_types) if has_body else None
        body_format = CONTENT_TYPE_FORMATS.get((content_type or "").split(";")[0].strip().lower(), "text")

        blueprint = {
            "index": index,
            "method": method,
            "content_type": content_type,
            "target_body_length": 0,
            "exemplar_id": None,
            "noise_profile": None,
            "planned_pii": [],
            **self._diversity_seeds(scenario, index, rng),
        }

        if has_body:
            candidates = self._by_scenario_type(self._request_exemplars.get(body_format, []), scenario)
            if candidates and not negative_control:
                # prefer exemplars of the same method
                same_method = [e for e in candidates if e["method"] == method]
                exemplar = self._choose(rng, same_method or candidates)
                blueprint["exemplar_id"] = exemplar["id"]
                blueprint["target_body_length"] = max(80, exemplar["length"])
            else:
                blueprint["target_body_length"] = min(self._fallback_length(rng), self.max_body_chars)
                blueprint["noise_profile"] = rng.choice(NOISE_PROFILES)
        elif not negative_control:
            candidates = [e for e in self._request_exemplars.get("none", []) if e["method"] == method]
            if candidates:
                blueprint["exemplar_id"] = rng.choice(candidates)["id"]

        if not negative_control:
            blueprint["planned_pii"] = self._sample_pii(scenario, rng, has_body, blueprint["target_body_length"])
        return blueprint

    def _diversity_seeds(self, scenario: dict, index: int, rng: random.Random) -> dict:
        """
        Sample what makes this message differ from the other messages of the scenario.

        Hosts and endpoint families are cycled through in a scenario-specific order, so that all
        variants of the scenario are used; client and persona are drawn at random.
        """
        scenario_id = scenario.get("scenario_id", "unknown")
        seeds = {}
        for key, field in (("host", "host_variants"), ("endpoint_family", "endpoint_families")):
            variants = [v for v in scenario.get(field) or [] if isinstance(v, str) and v]
            if variants:
                order = list(variants)
                self._rng("order", scenario_id, field).shuffle(order)
                seeds[key] = order[(index - 1) % len(order)]

        seeds["client"] = rng.choice(CLIENT_PROFILES)
        if not scenario.get("negative_control", False):
            seeds["persona"] = {
                "locale": rng.choice(PERSONA_LOCALES),
                "name_initials": rng.choice(INITIALS) + "." + rng.choice(INITIALS) + ".",
                "birth_year": rng.randint(1950, 2007),
            }
        seeds["variation_seed"] = "%08x" % rng.getrandbits(32)
        return seeds

    @staticmethod
    def _by_scenario_type(exemplars: list[dict], scenario: dict) -> list[dict]:
        """
        The exemplars that fit the type of a scenario: requests of third-party SDKs follow real
        tracker traffic, requests of an app to its own backend follow the remaining traffic.
        """
        scenario_type = scenario.get("type")
        if scenario_type == "third_party_sdk":
            fitting = [e for e in exemplars if e.get("is_tracker")]
        elif scenario_type == "first_party":
            fitting = [e for e in exemplars if not e.get("is_tracker")]
        else:
            return exemplars
        # too few fitting exemplars would be repeated all the time
        return fitting if len(fitting) >= MIN_FITTING_EXEMPLARS else exemplars

    @staticmethod
    def _choose(rng: random.Random, exemplars: list[dict]) -> dict:
        """Choose an exemplar according to how often its kind of message occurs in the real traffic."""
        return rng.choices(exemplars, weights=[e.get("weight", 1) for e in exemplars])[0]

    def _sample_pii(
            self, scenario: dict, rng: random.Random, has_body: bool, target_body_length: int = 0
    ) -> list[dict[str, str]]:
        """
        Sample the PII types to embed from the scenario's weighted candidates.

        The number of PII values in the body is limited by the size of the body, so that
        small messages are not saturated with PII.
        """
        candidates = []
        for candidate in scenario.get("pii_candidates") or []:
            label = candidate.get("data_type") or candidate.get("label")
            if label:
                weight = candidate.get("weight")
                candidates.append((label, weight if isinstance(weight, (int, float)) else 0.5, candidate))
        if not candidates:
            return []

        chosen = [c for c in candidates if rng.random() < c[1]]
        if not chosen:
            chosen = [max(candidates, key=lambda c: c[1])]

        planned = []
        for label, _, candidate in chosen:
            placements = [p for p in candidate.get("typical_placements") or [] if p in ("path", "query", "header", "body")]
            if not has_body:
                placements = [p for p in placements if p != "body"] or ["query"]
            planned.append({"data_type": label, "placement": rng.choice(placements or ["body"])})

        if has_body:
            max_in_body = max(1, target_body_length // CHARS_PER_BODY_PII)
            in_body = [p for p in planned if p["placement"] == "body"]
            if len(in_body) > max_in_body:
                keep = rng.sample(in_body, max_in_body)
                planned = [p for p in planned if p["placement"] != "body" or p in keep]
        return planned

    @staticmethod
    def _fallback_length(rng: random.Random) -> int:
        lengths, weights = zip(*FALLBACK_LENGTHS)
        base = rng.choices(lengths, weights=weights)[0]
        return int(base * rng.uniform(0.7, 1.3))

    # ------------------------------------------------------------------ responses

    def response_blueprint(self, request: dict, scenario: dict = None) -> dict:
        """
        Sample the blueprint of the response to a request: its shape (see _response_shape) and
        the PII to embed, drawn from the `response_pii_candidates` of the scenario by their weights.
        """
        blueprint = self._response_shape(request)
        blueprint["planned_pii"] = []
        candidates = [
            c for c in (scenario or {}).get("response_pii_candidates") or []
            if isinstance(c, dict) and (c.get("data_type") or c.get("label"))
        ]
        is_error = isinstance(blueprint["status_code"], int) and blueprint["status_code"] >= 400
        if not candidates or request.get("negative_control", False) or is_error:
            return blueprint

        rng = self._rng("response-pii", request.get("id"))
        # only a body with a target size has room for planned PII
        has_body = blueprint["target_body_length"] > 0
        planned = []
        for candidate in candidates:
            weight = candidate.get("weight")
            if rng.random() >= (weight if isinstance(weight, (int, float)) else 0.5):
                continue
            placements = [p for p in candidate.get("typical_placements") or ["body"] if p in ("header", "body")]
            if not has_body:
                placements = [p for p in placements if p == "header"]
            if placements:
                planned.append({
                    "data_type": candidate.get("data_type") or candidate.get("label"),
                    "placement": rng.choice(placements),
                })
        if has_body:
            max_in_body = max(1, blueprint["target_body_length"] // CHARS_PER_BODY_PII)
            in_body = [p for p in planned if p["placement"] == "body"]
            if len(in_body) > max_in_body:
                keep = rng.sample(in_body, max_in_body)
                planned = [p for p in planned if p["placement"] != "body" or p in keep]
        blueprint["planned_pii"] = planned
        return blueprint

    def _response_shape(self, request: dict) -> dict:
        """
        Sample the shape of the response to a request.

        If the request was generated from a real-world exemplar whose real response is known,
        the response follows that real response (status code, content type, body or no body), which
        makes request and response fit each other the way they do in real traffic.
        """
        rng = self._rng("response", request.get("id"))
        blueprint = {
            "target_body_length": 0, "exemplar_id": None, "noise_profile": None,
            "status_code": None, "content_type": None, "body": None, "paired": False,
        }

        if request.get("negative_control", False):
            blueprint["body"] = (
                "none, or a short text body (at most a few hundred characters); for binary resources "
                "such as images, fonts or archives the body is omitted"
            )
            return blueprint

        # some requests fail: error responses are short and do not follow an exemplar
        if rng.random() < self.error_response_share:
            blueprint["status_code"] = rng.choice(ERROR_STATUS_CODES)
            blueprint["body"] = "short error message or error object fitting the status code, or none"
            return blueprint

        request_exemplar = self.pool.get((request.get("blueprint") or {}).get("exemplar_id"))
        real_response = (request_exemplar or {}).get("response")
        if real_response:
            blueprint["paired"] = True
            blueprint["status_code"] = real_response.get("status_code")
            blueprint["content_type"] = real_response.get("content_type") or None
            exemplar = self.pool.get(real_response.get("exemplar_id"))
            if exemplar and exemplar["length"] <= self.max_body_chars:
                blueprint["exemplar_id"] = exemplar["id"]
                blueprint["target_body_length"] = max(80, exemplar["length"])
            elif real_response.get("short_body"):
                blueprint["body"] = f"very short, like the real response: {real_response['short_body']}"
            elif real_response.get("body") == "none":
                blueprint["body"] = "none"
            else:
                # the real body is binary or too long to serve as exemplar
                blueprint["body"] = "short text body or none"
            return blueprint

        # no real response known: not every response carries a (text) body
        if rng.random() >= self.response_body_share:
            return blueprint

        # An unrelated real response as exemplar would pull the response away from its request.
        # Only its size is used; the content is derived from the request.
        if self._response_exemplars:
            blueprint["target_body_length"] = max(80, self._choose(rng, self._response_exemplars)["length"])
        else:
            blueprint["target_body_length"] = min(self._fallback_length(rng), self.max_body_chars)
        blueprint["body"] = "derived from the request: structure and keys follow the conventions of the request"
        return blueprint

    # ------------------------------------------------------------------ rendering

    def describe(self, blueprint: dict, kind: str = "request") -> dict:
        """Render a blueprint for the prompt (including the exemplar it refers to)."""
        described = {}
        if kind == "request":
            described["blueprint"] = blueprint["index"]
            described["method"] = blueprint["method"]
            described["content_type"] = blueprint["content_type"] or "(no body)"
            for key in ("host", "endpoint_family", "client", "persona", "variation_seed"):
                if blueprint.get(key):
                    described[key] = blueprint[key]
            described["pii_to_embed"] = blueprint["planned_pii"]
        else:
            for key in ("status_code", "content_type", "body"):
                if blueprint.get(key):
                    described[key] = blueprint[key]
            if blueprint.get("planned_pii"):
                described["pii_to_embed"] = blueprint["planned_pii"]

        if blueprint["target_body_length"]:
            described["target_body_length"] = blueprint["target_body_length"]
        elif kind == "request":
            described["body"] = "none"
        if blueprint.get("noise_profile"):
            described["filler"] = blueprint["noise_profile"]

        exemplar = self.pool.get(blueprint["exemplar_id"]) if blueprint.get("exemplar_id") else None
        if exemplar:
            # identifiers of the real message are scrambled, differently for every use of the exemplar
            rng = self._rng("scramble", exemplar["id"], blueprint.get("variation_seed"), blueprint.get("index"))
            shown = {"header_names": [h["key"] for h in exemplar["headers"]]}
            if kind == "request" and not exemplar["body"]:
                shown["path"] = scramble_identifiers(exemplar["path"], rng)
            if exemplar["body"]:
                shown["body"] = scramble_identifiers(exemplar["body"], rng)
            described["real_world_exemplar"] = shown
        return described


def scramble_identifiers(text: str, rng: random.Random) -> str:
    """
    Replace identifier-like tokens (long tokens containing digits) by random tokens of the same shape.

    Exemplars come from real traffic. Scrambling keeps their structure and the look of their
    values, but no real identifier, token or timestamp is shown to the LLM, and every use of an
    exemplar shows different values.
    """
    def scramble(match):
        token = match.group(0)
        if sum(ch.isdigit() for ch in token) < 3:
            return token
        is_hex = all(ch in "0123456789abcdefABCDEF-_" for ch in token)
        # numbers keep their leading digits, so that timestamps and prefixed IDs stay plausible
        keep = 3 if token.isdigit() else 0
        scrambled = []
        for position, ch in enumerate(token):
            if position < keep:
                scrambled.append(ch)
            elif ch.isdigit():
                scrambled.append(rng.choice("0123456789"))
            elif ch.isalpha():
                alphabet = "abcdef" if is_hex else "abcdefghijklmnopqrstuvwxyz"
                new = rng.choice(alphabet)
                scrambled.append(new.upper() if ch.isupper() else new)
            else:
                scrambled.append(ch)
        return "".join(scrambled)

    return COPY_TOKEN_PATTERN.sub(scramble, text)


def plan_batches(blueprints: list[dict], batch_size: int, budget_chars: int = 10000) -> list[list[dict]]:
    """Split blueprints into generation calls whose expected output fits the budget."""
    batches, current, current_chars = [], [], 0
    for blueprint in blueprints:
        chars = blueprint.get("target_body_length", 0) + MESSAGE_OVERHEAD_CHARS
        if current and (len(current) >= batch_size or current_chars + chars > budget_chars):
            batches.append(current)
            current, current_chars = [], 0
        current.append(blueprint)
        current_chars += chars
    if current:
        batches.append(current)
    return batches


def output_token_limit(blueprints: list[dict], max_tokens: int, reasoning_tokens: int = None) -> int:
    """
    Token limit for a generation call, derived from the expected size of its output.

    A model that degenerates into repetitive or garbled output would otherwise keep generating
    until the global limit is reached. Observed outputs are at most 1.5 times the expected
    number of characters; the limit leaves about twice that plus room for reasoning (a thinking
    model was observed to spend up to 10,000 tokens on reasoning about a single message).
    """
    if reasoning_tokens is None:
        reasoning_tokens = REASONING_TOKENS
    expected_chars = sum(b.get("target_body_length", 0) + MESSAGE_OVERHEAD_CHARS for b in blueprints)
    return min(max_tokens, int(expected_chars * 1.2) + reasoning_tokens)


def copy_tokens(exemplar: dict | None) -> list[str]:
    """
    Identifier-like value tokens of an exemplar (long tokens containing digits).

    Generated messages must not contain these: they are real identifiers, tokens or timestamps.
    """
    if not exemplar:
        return []
    values = body_values(exemplar["body"], exemplar["format"]) if exemplar.get("body") else []
    if exemplar.get("kind") == "request" and not exemplar.get("body"):
        values.append(exemplar.get("path", ""))

    tokens = set()
    for value in values:
        for token in COPY_TOKEN_PATTERN.findall(value):
            if sum(ch.isdigit() for ch in token) >= 3:
                tokens.add(token)
    return sorted(tokens)
