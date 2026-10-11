"""
Planning and checking of scenario sets.

The scenario set bounds everything the message generators can produce: which labels occur, how
often, where, and in what kind of traffic. It is therefore planned and checked by the pipeline:

- a *profile* states what a set must look like (coverage, balance of the labels, mix of scenario
  types, limits per scenario),
- `check_outline` checks the outline of a set (scenarios with their labels) against the profile,
- `validate_scenario` checks a single scenario template,
- `set_report` describes a set of templates, and
- `forecast` predicts, without any LLM call, how often each label will be planned for the
  messages of the set.
"""

import copy
import json
import math
import statistics
from collections import Counter

SCENARIO_TYPES = {
    "first_party": "a request of the app to its own backend (account, profile, content, transactions, "
                   "app-specific features)",
    "third_party_sdk": "a request of a third-party SDK embedded in the app to the SDK vendor "
                       "(analytics, advertising, attribution, crash reporting, push messaging, A/B testing)",
    "fetch": "a retrieval without request body (remote configuration, feature flags, content, search, "
             "lookups), with all data in the path, the query string and the headers",
}

DEFAULT_PROFILE = {
    # share of the PII scenarios per type, and how far the actual shares may deviate
    "type_shares": {"first_party": 0.5, "third_party_sdk": 0.3, "fetch": 0.2},
    "type_tolerance": 0.15,
    # every label is a candidate of at least this many scenarios ...
    "min_scenarios_per_label": 2,
    # ... and of at most this share of the scenarios
    "max_label_share": 0.3,
    "max_labels_per_scenario": 8,
    "max_response_labels_per_scenario": 6,
    # share of the candidate placements outside the body, across the set
    "min_non_body_placement_share": 0.25,
    "min_endpoint_families": 3,
    "min_host_variants": 2,
    # forecast: a label is rare if it is planned less often than this share of the median label
    "rare_label_ratio": 0.25,
    # rebalancing: the weights of a label are raised until it is planned at least this share of
    # the median label (0 switches rebalancing off)
    "rebalance_label_ratio": 0.5,
}

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
BODY_METHODS = {"POST", "PUT", "PATCH"}
PLACEMENTS = {"path", "query", "header", "body"}
RESPONSE_PLACEMENTS = {"header", "body"}
NO_BODY = "(none)"

NEGATIVE_CONTROLS = [
    ("static_asset_fetch", "Negative control: fetch of a static asset (script, stylesheet, font) from a CDN, "
                           "without any user or device data."),
    ("feature_flag_poll", "Negative control: anonymous poll of the public feature flags and remote "
                          "configuration of an app, without any user or device data."),
    ("service_health_check", "Negative control: health and version check of a public API endpoint, "
                             "without any user or device data."),
    ("public_catalog_fetch", "Negative control: fetch of public catalogue or help content that is the same "
                             "for every user, without any user or device data."),
    ("legal_document_fetch", "Negative control: fetch of the public terms of service or privacy policy, "
                             "without any user or device data."),
]


def load_profile(path: str = None) -> dict:
    """Load a scenario profile; values missing in the file are taken from the default profile."""
    profile = copy.deepcopy(DEFAULT_PROFILE)
    if path:
        with open(path, encoding="utf-8") as f:
            custom = json.load(f)
        for key, value in custom.items():
            if isinstance(value, dict) and isinstance(profile.get(key), dict):
                profile[key] = value
            else:
                profile[key] = value
    return profile


def effective_limits(n_labels: int, n_scenarios: int, profile: dict) -> tuple[dict, list[str]]:
    """
    The limits that a set of n scenarios can actually satisfy.

    With few scenarios or many labels, the profile cannot be met: the limits are relaxed as
    little as possible, and the notes say what was relaxed and how many scenarios would be needed.
    """
    limits = {
        "min_scenarios_per_label": profile["min_scenarios_per_label"],
        "max_labels_per_scenario": profile["max_labels_per_scenario"],
    }
    notes = []
    n_scenarios = max(1, n_scenarios)
    if limits["min_scenarios_per_label"] > n_scenarios:
        # a label cannot be in more scenarios than there are
        limits["min_scenarios_per_label"] = n_scenarios
        notes.append(f"With {n_scenarios} scenario(s), every label can be in at most {n_scenarios} scenario(s).")
    needed = math.ceil(n_labels * limits["min_scenarios_per_label"] / limits["max_labels_per_scenario"])
    if needed > n_scenarios:
        while limits["min_scenarios_per_label"] > 1 and \
                n_labels * limits["min_scenarios_per_label"] > n_scenarios * limits["max_labels_per_scenario"]:
            limits["min_scenarios_per_label"] -= 1
        if n_labels * limits["min_scenarios_per_label"] > n_scenarios * limits["max_labels_per_scenario"]:
            limits["max_labels_per_scenario"] = math.ceil(n_labels / n_scenarios)
        notes.append(
            f"{n_scenarios} scenarios are too few for {n_labels} labels under the scenario profile "
            f"(at least {needed} are needed). Relaxed to: every label in at least "
            f"{limits['min_scenarios_per_label']} scenario(s), at most {limits['max_labels_per_scenario']} "
            f"labels per scenario."
        )
    limits["max_scenarios_per_label"] = max(
        limits["min_scenarios_per_label"], math.ceil(profile["max_label_share"] * n_scenarios)
    )
    return limits, notes


def candidate_label(candidate: dict) -> str | None:
    return candidate.get("data_type") or candidate.get("label") if isinstance(candidate, dict) else None


def scenario_labels(scenario: dict, key: str = "pii_candidates") -> list[str]:
    return [label for label in (candidate_label(c) for c in scenario.get(key) or []) if label]


# --------------------------------------------------------------------------- outline

def check_outline(outline: list, labels: list[str], n_scenarios: int, profile: dict) -> tuple[list[str], list[str]]:
    """
    Check the outline of a scenario set against the profile.

    Returns (errors, warnings). Errors make the outline unusable (structure, unknown labels,
    labels that are not covered); warnings are deviations from the profile (balance, mix).
    """
    errors, warnings = [], []
    taxonomy = set(labels)
    if not isinstance(outline, list) or not outline:
        return ["The outline must be a non-empty JSON array of scenarios"], warnings

    limits, _ = effective_limits(len(labels), n_scenarios, profile)
    usage = Counter()
    names, apps, types = Counter(), Counter(), Counter()

    for index, entry in enumerate(outline):
        name = entry.get("scenario") if isinstance(entry, dict) else None
        prefix = f"Scenario {index + 1} ('{name}')"
        if not isinstance(entry, dict):
            errors.append(f"Scenario {index + 1}: must be a JSON object")
            continue
        for field in ("scenario", "goal", "app"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                errors.append(f"{prefix}: missing '{field}'")
        if entry.get("type") not in SCENARIO_TYPES:
            errors.append(f"{prefix}: 'type' must be one of {', '.join(SCENARIO_TYPES)}")
        else:
            types[entry["type"]] += 1
        names[name] += 1
        apps[entry.get("app")] += 1

        request_labels = entry.get("request_labels")
        if not isinstance(request_labels, list) or not request_labels:
            errors.append(f"{prefix}: 'request_labels' must be a non-empty array")
            request_labels = []
        response_labels = entry.get("response_labels", [])
        if not isinstance(response_labels, list):
            errors.append(f"{prefix}: 'response_labels' must be an array")
            response_labels = []
        for label in list(request_labels) + list(response_labels):
            if label not in taxonomy:
                errors.append(f"{prefix}: '{label}' is not a label of the taxonomy")
        if len(set(request_labels)) != len(request_labels):
            errors.append(f"{prefix}: 'request_labels' contains duplicates")
        if len(request_labels) > limits["max_labels_per_scenario"]:
            warnings.append(
                f"{prefix}: has {len(request_labels)} request labels; at most "
                f"{limits['max_labels_per_scenario']} are allowed"
            )
        if len(response_labels) > profile["max_response_labels_per_scenario"]:
            warnings.append(
                f"{prefix}: has {len(response_labels)} response labels; at most "
                f"{profile['max_response_labels_per_scenario']} are allowed"
            )
        usage.update(set(label for label in request_labels if label in taxonomy))

    if len(outline) != n_scenarios:
        errors.append(f"The outline has {len(outline)} scenarios; exactly {n_scenarios} are required")
    for name, count in names.items():
        if name and count > 1:
            errors.append(f"The scenario name '{name}' is used {count} times; names must be unique")
    for app, count in apps.items():
        if app and count > 1:
            warnings.append(f"The app '{app}' is used by {count} scenarios; use a different app for each scenario")

    missing = [label for label in labels if usage[label] == 0]
    if missing:
        errors.append("Labels that no scenario requests: " + ", ".join(missing))
    rare = [label for label in labels if 0 < usage[label] < limits["min_scenarios_per_label"]]
    if rare:
        warnings.append(
            f"Labels in fewer than {limits['min_scenarios_per_label']} scenarios (add each to more scenarios "
            f"where it fits): " + ", ".join(rare)
        )
    frequent = [f"{label} ({usage[label]})" for label in labels if usage[label] > limits["max_scenarios_per_label"]]
    if frequent:
        warnings.append(
            f"Labels in more than {limits['max_scenarios_per_label']} scenarios (remove each from scenarios "
            f"where it is least typical): " + ", ".join(frequent)
        )

    total = sum(types.values())
    for scenario_type, share in profile["type_shares"].items():
        actual = types[scenario_type] / total if total else 0
        if abs(actual - share) > profile["type_tolerance"]:
            target = round(share * n_scenarios)
            warnings.append(
                f"{types[scenario_type]} scenarios are of type '{scenario_type}'; about {target} are wanted"
            )
    return errors, warnings


def negative_control_entries(count: int) -> list[dict]:
    """Outline entries of the negative controls (scenarios without any PII)."""
    entries = []
    for index in range(count):
        name, goal = NEGATIVE_CONTROLS[index % len(NEGATIVE_CONTROLS)]
        suffix = "" if index < len(NEGATIVE_CONTROLS) else f"_{index // len(NEGATIVE_CONTROLS) + 1}"
        entries.append({
            "scenario_id": f"NC{index + 1:02d}", "scenario": name + suffix, "goal": goal, "type": "fetch",
            "app": None, "request_labels": [], "response_labels": [], "negative_control": True,
        })
    return entries


# --------------------------------------------------------------------------- templates

def normalise_scenario(scenario: dict) -> dict:
    """Bring a scenario template into the current form (templates of earlier versions use 'label')."""
    for key in ("pii_candidates", "response_pii_candidates"):
        for candidate in scenario.get(key) or []:
            if isinstance(candidate, dict) and "data_type" not in candidate and "label" in candidate:
                candidate["data_type"] = candidate.pop("label")
    scenario.setdefault("type", "first_party")
    scenario.setdefault("response_pii_candidates", [])
    return scenario


def _validate_candidates(scenario: dict, key: str, taxonomy: set, placements: set, prefix: str) -> list[str]:
    errors = []
    candidates = scenario.get(key)
    if not isinstance(candidates, list):
        return [f"{prefix}: '{key}' must be an array"]
    seen = set()
    for index, candidate in enumerate(candidates):
        where = f"{prefix}: {key}[{index}]"
        if not isinstance(candidate, dict):
            errors.append(f"{where} must be an object")
            continue
        label = candidate_label(candidate)
        if not label:
            errors.append(f"{where} missing 'data_type'")
        elif label not in taxonomy:
            errors.append(f"{where} unknown label '{label}'")
        elif label in seen:
            errors.append(f"{where} duplicate label '{label}'")
        seen.add(label)

        weight = candidate.get("weight")
        if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float))
                                   or not 0 <= weight <= 1):
            errors.append(f"{where} weight must be in [0, 1]")
        candidate_placements = candidate.get("typical_placements", [])
        if not isinstance(candidate_placements, list):
            errors.append(f"{where} 'typical_placements' must be an array")
            continue
        for placement in candidate_placements:
            if placement not in placements:
                errors.append(f"{where} invalid placement '{placement}'")
    return errors


def validate_scenario(scenario: dict, labels: list[str], profile: dict = None, outline_entry: dict = None,
                      strict: bool = False) -> list[str]:
    """
    Validate a scenario template.

    strict: additionally apply the rules for newly generated templates (the templates of
    earlier versions do not satisfy all of them, but can still be used).
    """
    profile = profile or DEFAULT_PROFILE
    taxonomy = set(labels)
    scenario_id = scenario.get("scenario_id", "unknown")
    prefix = str(scenario_id)
    errors = []

    for field in ("scenario_id", "scenario", "goal", "host_variants", "endpoint_families", "methods",
                  "content_types", "pii_candidates"):
        if field not in scenario:
            errors.append(f"{prefix}: Missing required field '{field}'")
    if errors:
        return errors

    negative_control = bool(scenario.get("negative_control", False))
    for field in ("host_variants", "endpoint_families", "methods", "content_types"):
        if not isinstance(scenario[field], list) or not scenario[field] \
                or not all(isinstance(item, str) and item for item in scenario[field]):
            errors.append(f"{prefix}: '{field}' must be a non-empty array of strings")
    if errors:
        return errors

    methods = [method.upper() for method in scenario["methods"]]
    for method in methods:
        if method not in HTTP_METHODS:
            errors.append(f"{prefix}: invalid HTTP method '{method}'")
    for family in scenario["endpoint_families"]:
        if not family.startswith("/"):
            errors.append(f"{prefix}: endpoint family '{family}' must start with '/'")

    errors.extend(_validate_candidates(scenario, "pii_candidates", taxonomy, PLACEMENTS, prefix))
    if "response_pii_candidates" in scenario:
        errors.extend(_validate_candidates(scenario, "response_pii_candidates", taxonomy, RESPONSE_PLACEMENTS, prefix))
    if negative_control and (scenario.get("pii_candidates") or scenario.get("response_pii_candidates")):
        errors.append(f"{prefix}: Negative control should have empty pii_candidates")

    if not strict:
        return errors

    # ---- rules for newly generated templates
    if scenario.get("type") not in SCENARIO_TYPES:
        errors.append(f"{prefix}: 'type' must be one of {', '.join(SCENARIO_TYPES)}")
    for host in scenario["host_variants"]:
        if not host.endswith(".example") or "/" in host or " " in host:
            errors.append(f"{prefix}: host '{host}' must be a host name ending in '.example'")
    if len(set(scenario["host_variants"])) < profile["min_host_variants"]:
        errors.append(f"{prefix}: at least {profile['min_host_variants']} different host variants are required")
    if len(set(scenario["endpoint_families"])) < profile["min_endpoint_families"]:
        errors.append(f"{prefix}: at least {profile['min_endpoint_families']} different endpoint families are required")

    has_body = any(method in BODY_METHODS for method in methods) and \
        any(content_type != NO_BODY for content_type in scenario["content_types"])
    body_content_types = [c for c in scenario["content_types"] if c != NO_BODY]
    for content_type in body_content_types:
        if "/" not in content_type or " " in content_type:
            errors.append(f"{prefix}: '{content_type}' is not a content type (use '{NO_BODY}' for requests without body)")
    if any(method in BODY_METHODS for method in methods) and not body_content_types:
        errors.append(f"{prefix}: methods with a body ({', '.join(m for m in methods if m in BODY_METHODS)}) "
                      f"need a content type")
    if scenario.get("type") == "fetch" and has_body:
        errors.append(f"{prefix}: a 'fetch' scenario has no request body (methods GET or HEAD, content type '{NO_BODY}')")

    for index, candidate in enumerate(scenario.get("pii_candidates") or []):
        if not isinstance(candidate, dict):
            continue
        placements = candidate.get("typical_placements") or []
        if not placements:
            errors.append(f"{prefix}: pii_candidates[{index}] needs at least one placement")
        elif not has_body and set(placements) == {"body"}:
            errors.append(f"{prefix}: pii_candidates[{index}] ('{candidate_label(candidate)}') is placed in the "
                          f"body, but the scenario has no request body")
        if candidate.get("weight") is None:
            errors.append(f"{prefix}: pii_candidates[{index}] needs a weight")
    for index, candidate in enumerate(scenario.get("response_pii_candidates") or []):
        if isinstance(candidate, dict) and candidate.get("weight") is None:
            errors.append(f"{prefix}: response_pii_candidates[{index}] needs a weight")

    if outline_entry is not None:
        for key, planned in (("pii_candidates", outline_entry.get("request_labels") or []),
                             ("response_pii_candidates", outline_entry.get("response_labels") or [])):
            actual = scenario_labels(scenario, key)
            missing = [label for label in planned if label not in actual]
            extra = [label for label in actual if label not in planned]
            if missing:
                errors.append(f"{prefix}: {key} lacks the planned labels: {', '.join(missing)}")
            if extra:
                errors.append(f"{prefix}: {key} contains labels that were not planned: {', '.join(extra)}")
    return errors


# --------------------------------------------------------------------------- sets of templates

def set_report(scenarios: list[dict], labels: list[str], profile: dict = None) -> dict:
    """Describe a set of scenario templates and how it deviates from the profile."""
    profile = profile or DEFAULT_PROFILE
    pii_scenarios = [s for s in scenarios if not s.get("negative_control", False)]
    limits, notes = effective_limits(len(labels), len(pii_scenarios), profile)

    usage = Counter(label for s in pii_scenarios for label in set(scenario_labels(s)))
    response_usage = Counter(
        label for s in pii_scenarios for label in set(scenario_labels(s, "response_pii_candidates"))
    )
    placements = Counter(
        placement for s in pii_scenarios for candidate in s.get("pii_candidates") or []
        if isinstance(candidate, dict) for placement in candidate.get("typical_placements") or []
    )
    types = Counter(s.get("type", "first_party") for s in pii_scenarios)
    bodyless = sum(
        1 for s in pii_scenarios
        if not any(str(method).upper() in BODY_METHODS for method in s.get("methods") or [])
    )
    counts = [usage[label] for label in labels]
    total_placements = sum(placements.values())
    non_body_share = 1 - placements["body"] / total_placements if total_placements else 0.0

    names = Counter(s.get("scenario") for s in scenarios)
    families = Counter(family for s in scenarios for family in set(s.get("endpoint_families") or []))

    deviations = list(notes)
    uncovered = [label for label in labels if usage[label] == 0]
    if uncovered:
        deviations.append("Labels not covered: " + ", ".join(uncovered))
    rare = [label for label in labels if 0 < usage[label] < limits["min_scenarios_per_label"]]
    if rare:
        deviations.append(f"Labels in fewer than {limits['min_scenarios_per_label']} scenarios: " + ", ".join(rare))
    frequent = [label for label in labels if usage[label] > limits["max_scenarios_per_label"]]
    if frequent:
        deviations.append(f"Labels in more than {limits['max_scenarios_per_label']} scenarios: " + ", ".join(frequent))
    for scenario_type, share in profile["type_shares"].items():
        actual = types[scenario_type] / len(pii_scenarios) if pii_scenarios else 0
        if abs(actual - share) > profile["type_tolerance"]:
            deviations.append(f"Share of '{scenario_type}' scenarios is {actual:.0%} (profile: {share:.0%})")
    if total_placements and non_body_share < profile["min_non_body_placement_share"]:
        deviations.append(
            f"Only {non_body_share:.0%} of the candidate placements are outside the body "
            f"(profile: at least {profile['min_non_body_placement_share']:.0%})"
        )
    duplicates = [name for name, count in names.items() if name and count > 1]
    if duplicates:
        deviations.append("Scenario names used more than once: " + ", ".join(duplicates))
    shared = [family for family, count in families.items() if count > 1]
    if shared:
        deviations.append(f"{len(shared)} endpoint families are used by more than one scenario")

    return {
        "scenarios": len(scenarios),
        "pii_scenarios": len(pii_scenarios),
        "negative_controls": len(scenarios) - len(pii_scenarios),
        "labels_covered": sum(1 for count in counts if count),
        "total_labels": len(labels),
        "scenarios_per_label": {
            "min": min(counts) if counts else 0, "median": statistics.median(counts) if counts else 0,
            "max": max(counts) if counts else 0,
        },
        "labels_in_one_scenario": sum(1 for count in counts if count == 1),
        "most_used_labels": usage.most_common(5),
        "labels_per_scenario_median": statistics.median(len(scenario_labels(s)) for s in pii_scenarios)
        if pii_scenarios else 0,
        "response_labels_covered": sum(1 for label in labels if response_usage[label]),
        "types": dict(types),
        "scenarios_without_request_body": bodyless,
        "candidate_placements": dict(placements.most_common()),
        "non_body_placement_share": round(non_body_share, 3),
        "limits": limits,
        "deviations": deviations,
    }


def forecast(scenarios: list[dict], sampler, requests_per_scenario: int, labels: list[str],
             profile: dict = None) -> dict:
    """
    Predict how often each label will be planned for the messages of a scenario set.

    The blueprints of all messages are sampled exactly as the generators will sample them, so
    the forecast includes the effect of the candidate weights, of the placements, and of the
    limit of planned PII in small bodies. It counts planned PII only: what the audit adds on
    top (PII that comes with realistic filler) is not predictable.
    """
    profile = profile or DEFAULT_PROFILE
    request_labels, response_labels = Counter(), Counter()
    placements = Counter()
    requests = with_body = 0

    for scenario in scenarios:
        scenario_id = scenario.get("scenario_id", "unknown")
        negative_control = bool(scenario.get("negative_control", False))
        for index in range(1, requests_per_scenario + 1):
            blueprint = sampler.request_blueprint(scenario, index)
            requests += 1
            with_body += bool(blueprint["target_body_length"])
            for planned in blueprint["planned_pii"]:
                request_labels[planned["data_type"]] += 1
                placements[planned["placement"]] += 1
            request = {"id": f"{scenario_id}_{index:03d}", "blueprint": blueprint, "negative_control": negative_control}
            for planned in sampler.response_blueprint(request, scenario).get("planned_pii") or []:
                response_labels[planned["data_type"]] += 1

    counts = [request_labels[label] for label in labels]
    median = statistics.median(counts) if counts else 0
    threshold = profile["rare_label_ratio"] * median
    total_placements = sum(placements.values())
    return {
        "requests": requests,
        "requests_per_scenario": requests_per_scenario,
        "share_of_requests_with_body": round(with_body / requests, 3) if requests else 0,
        "planned_annotations": sum(counts),
        "planned_per_label": {label: request_labels[label] for label in sorted(labels, key=lambda l: -request_labels[l])},
        "median_per_label": median,
        "labels_never_planned": [label for label in labels if request_labels[label] == 0],
        "rare_labels": [label for label in labels if 0 < request_labels[label] < threshold],
        "placements": {
            placement: round(count / total_placements, 3) for placement, count in placements.most_common()
        } if total_placements else {},
        "planned_response_annotations": sum(response_labels.values()),
        "planned_per_label_in_responses": dict(response_labels.most_common()),
    }


def rebalance_weights(scenarios: list[dict], sampler, requests_per_scenario: int, labels: list[str],
                      profile: dict = None, max_rounds: int = 8) -> dict:
    """
    Raise the weights of labels that would be planned too rarely.

    The outline balances in how many scenarios a label occurs, but not how often it is planned
    there: that is decided by the candidate weights, which the LLM sets per scenario without
    seeing the set. Using the forecast, the weights of a label that falls below the wanted share
    of the median label are raised in all its scenarios, step by step, until the label reaches
    that share or its weights are at 1.0. Weights are only raised, never lowered: that some kinds
    of data are more common than others is realistic.

    The scenarios are changed in place; a changed candidate keeps its `original_weight`.
    Returns the changes and the labels that could not be raised far enough.
    """
    profile = profile or DEFAULT_PROFILE
    ratio = profile.get("rebalance_label_ratio", 0)
    before = forecast(scenarios, sampler, requests_per_scenario, labels, profile)
    result = {"target_share_of_median": ratio, "changes": [], "labels_below_target": [],
              "planned_before": {}, "planned_after": {}}
    if not ratio or not labels or not before["median_per_label"]:
        return result

    # the target stays fixed, so that raising some labels does not move the goal for others
    target = ratio * before["median_per_label"]
    current = before
    changed = {}
    for _ in range(max_rounds):
        low = [label for label in labels if current["planned_per_label"][label] < target]
        if not low:
            break
        raised = False
        for label in low:
            planned = current["planned_per_label"][label]
            factor = min(2.0, 1.1 * target / max(1, planned))
            for scenario in scenarios:
                for candidate in scenario.get("pii_candidates") or []:
                    if candidate_label(candidate) != label:
                        continue
                    weight = candidate.get("weight")
                    weight = weight if isinstance(weight, (int, float)) and not isinstance(weight, bool) else 0.5
                    new_weight = min(1.0, round(max(weight * factor, weight + 0.05), 2))
                    if new_weight > weight:
                        key = (scenario.get("scenario_id"), label)
                        changed.setdefault(key, weight)
                        candidate.setdefault("original_weight", weight)
                        candidate["weight"] = new_weight
                        raised = True
        if not raised:
            break  # all weights of the rare labels are at 1.0
        current = forecast(scenarios, sampler, requests_per_scenario, labels, profile)

    weights = {
        (scenario.get("scenario_id"), candidate_label(candidate)): candidate.get("weight")
        for scenario in scenarios for candidate in scenario.get("pii_candidates") or []
    }
    result["changes"] = [
        {"scenario_id": scenario_id, "data_type": label, "from": original, "to": weights[(scenario_id, label)]}
        for (scenario_id, label), original in changed.items()
    ]
    affected = sorted({change["data_type"] for change in result["changes"]} | {
        label for label in labels if before["planned_per_label"][label] < target
    })
    result["labels_below_target"] = [label for label in labels if current["planned_per_label"][label] < target]
    result["planned_before"] = {label: before["planned_per_label"][label] for label in affected}
    result["planned_after"] = {label: current["planned_per_label"][label] for label in affected}
    result["target_planned_per_label"] = round(target, 1)
    return result
