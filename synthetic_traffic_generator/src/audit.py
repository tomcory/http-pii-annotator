"""
Audit of the completeness of PII annotations.

The validators check that what is annotated is correct. They cannot check that everything that
should be annotated is annotated: realistic messages are full of filler, and filler often matches
the taxonomy (a device model in a User-Agent header, a country code in an SDK block). The audit
closes this gap as far as it can be closed automatically:

1. **Propagation** (deterministic): an identifying or long value that is annotated for one
   component and also occurs in another component is annotated there as well.
2. **LLM audit**: an independent LLM call is given the message, the taxonomy and the existing
   annotations, and reports values that match the taxonomy but are not annotated. Every finding
   is verified mechanically (label in the taxonomy, value verbatim in the named component) before
   it is added. The audit is repeated until a round adds nothing.

Audit the validated messages of an existing run, or measure how many removed annotations the
auditor finds again:
    python -m src.audit <run folder> -schema <name> -model <model> [-api <api>] [-leave-one-out]
"""

import argparse
import asyncio
import copy
import json
import logging
import os
import random
import re
import sys
import urllib.parse
from collections import Counter

from src import positions, util
from src.blueprints import REASONING_TOKENS, REASONING_TOKENS_OFF
from src.diversity import _is_unique_value
from src.pipeline_steps import message_validation
from src.pipeline_steps.pipeline_step import PipelineStep

logger = logging.getLogger(__name__)

PLACEMENTS = {"http_request": ("path", "query", "header", "body"), "http_response": ("header", "body")}
MAX_VALUE_CHARS = 400
# values of this length are annotated wherever they occur without asking the LLM
PROPAGATION_MIN_CHARS = 8


def _components(message: dict, http_key: str) -> dict[str, list[str]]:
    if http_key == "http_request":
        return message_validation.request_components(message[http_key])
    return message_validation.response_components(message[http_key])


def _occurs(value: str, texts: list[str]) -> bool:
    """Whether the value occurs in one of the texts as a value of its own (not inside a longer word or number)."""
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])")
    return any(pattern.search(text) for text in texts)


def _annotated(message: dict) -> set:
    return {
        (a.get("placement"), a.get("value")) for a in message.get("pii_annotations", []) if isinstance(a, dict)
    }


def _refresh_data_types(message: dict):
    present = list(message.get("data_types_present") or [])
    for annotation in message.get("pii_annotations", []):
        if annotation.get("data_type") not in present:
            present.append(annotation.get("data_type"))
    message["data_types_present"] = present


def unannotated_occurrences(message: dict, http_key: str) -> list[dict]:
    """Annotated values that also occur in a component for which they are not annotated."""
    components = _components(message, http_key)
    annotated = _annotated(message)
    found = []
    for annotation in list(message.get("pii_annotations", [])):
        value = annotation.get("value")
        if not isinstance(value, str) or len(value) < 4:
            continue
        for placement in PLACEMENTS[http_key]:
            if placement == annotation.get("placement") or (placement, value) in annotated:
                continue
            if _occurs(value, components.get(placement, [])):
                found.append({"data_type": annotation.get("data_type"), "placement": placement, "value": value})
                annotated.add((placement, value))
    return found


def propagate_annotations(message: dict, http_key: str) -> int:
    """
    Annotate identifying and long values in every component in which they occur.

    Short common values (e.g. 'Berlin', which also occurs in 'Europe/Berlin') are left to the
    LLM audit, which is given them as hints. Returns the number of annotations added.
    """
    added = 0
    for finding in unannotated_occurrences(message, http_key):
        value = finding["value"]
        if _is_unique_value(value) or len(value) >= PROPAGATION_MIN_CHARS:
            message["pii_annotations"].append({**finding, "source": "propagated"})
            added += 1
    if added:
        _refresh_data_types(message)
    return added


def verify_finding(message: dict, http_key: str, finding, labels: set) -> tuple[dict | None, str]:
    """
    Check a finding of the auditor mechanically.

    Returns (annotation, "") if the finding can be added, or (None, reason) if it is dropped.
    """
    if not isinstance(finding, dict):
        return None, "not an object"
    data_type, placement, value = finding.get("data_type"), finding.get("placement"), finding.get("value")
    if not isinstance(data_type, str) or data_type not in labels:
        return None, "unknown data type"
    if placement not in PLACEMENTS[http_key]:
        return None, "invalid placement"
    if not isinstance(value, str) or not value.strip():
        return None, "empty value"
    if len(value) > MAX_VALUE_CHARS:
        return None, "value too long"

    texts = _components(message, http_key).get(placement, [])
    if not any(value in text for text in texts):
        # the auditor may have reported the URL-encoded form
        decoded = urllib.parse.unquote_plus(value)
        if decoded == value or not any(decoded in text for text in texts):
            return None, "value not in component"
        value = decoded

    # annotations carry decoded values (see normalise_annotation_encoding)
    probe = {http_key: message[http_key], "pii_annotations": [{"placement": placement, "value": value}]}
    message_validation.normalise_annotation_encoding(probe, http_key)
    value = probe["pii_annotations"][0]["value"]

    for annotation in message.get("pii_annotations", []):
        if annotation.get("placement") != placement or not isinstance(annotation.get("value"), str):
            continue
        if annotation["value"] == value:
            return None, "already annotated"
        if annotation.get("data_type") == data_type and value in annotation["value"]:
            return None, "part of an annotated value"

    # the value must be locatable; the field named by the auditor pins down short values
    field = finding.get("field") if isinstance(finding.get("field"), str) and finding.get("field").strip() else None
    spans, reason = positions.locate(message[http_key], placement, value, field)
    if not spans:
        return None, "cannot be located" if reason == "not found" else "ambiguous position"

    annotation = {"data_type": data_type, "placement": placement, "value": value, "source": "audit"}
    if field:
        annotation["field"] = field
    return annotation, ""


class AnnotationAuditor:
    """Audits the completeness of the annotations of generated messages."""

    def __init__(
            self,
            schema: str,
            labels: list[str],
            prompts_dir: str = util.PROMPTS_DIR,
            client=None,
            rounds: int = 2,
            temperature: float = 0.2,
            timeout: float = 600.0,
            thinking: bool = None,
    ):
        self.schema = schema
        self.labels = set(labels or [])
        self.client = client  # None: the client of the step that runs the audit
        self.rounds = max(1, rounds)
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = 4000 + (REASONING_TOKENS_OFF if thinking is False else REASONING_TOKENS)

        self.system_prompt = util.read_from_file(os.path.join(prompts_dir, "annotation_auditor_system.md"))
        self.user_prompt_template = util.read_from_file(os.path.join(prompts_dir, "annotation_auditor_user.md"))
        if not self.system_prompt or not self.user_prompt_template:
            raise ValueError("Annotation auditor prompts not found")

        self.stats = Counter()
        self.added_labels = Counter()
        self.dropped_reasons = Counter()

    def _render(self, message: dict, http_key: str) -> tuple[str, str]:
        taxonomy_message = "# PII Data Taxonomy\n\nUse ONLY the following data types (exact string match):\n\n" + self.schema

        http_message = message[http_key]
        if http_key == "http_request":
            shown = {k: http_message.get(k) for k in ("method", "path", "headers", "body") if k in http_message}
        else:
            shown = {k: http_message.get(k) for k in ("status_code", "headers", "body") if k in http_message}
        annotations = [
            {k: a.get(k) for k in ("data_type", "placement", "value")}
            for a in message.get("pii_annotations", []) if isinstance(a, dict)
        ]

        notes = []
        hints = unannotated_occurrences(message, http_key)
        if hints:
            notes.append(
                "Some annotated values also occur in a component for which they are not annotated. Check whether "
                "these occurrences are the same kind of data:\n" + "\n".join(
                    f"- '{h['value']}' ({h['data_type']}) also occurs in the {h['placement']}" for h in hints[:10]
                )
            )
        if message.get("negative_control", False):
            notes.append(
                "This message belongs to a negative-control scenario and is meant to contain no PII at all. "
                "Report any value that nevertheless matches the taxonomy."
            )

        user_prompt = self.user_prompt_template.replace("{{KIND}}", http_key.split("_")[1])
        user_prompt = user_prompt.replace("{{NOTES}}", "\n\n".join(notes))
        user_prompt = user_prompt.replace("{{MESSAGE_JSON}}", json.dumps(shown, indent=2, ensure_ascii=False))
        user_prompt = user_prompt.replace("{{ANNOTATIONS_JSON}}", json.dumps(annotations, indent=2, ensure_ascii=False))
        return taxonomy_message, user_prompt

    async def find_missing(self, step: PipelineStep, message: dict, http_key: str, msg_id: str) -> tuple[list[dict], list[dict]]:
        """One audit round: ask the LLM for missing annotations and verify its findings."""
        taxonomy_message, user_prompt = self._render(message, http_key)
        findings = await step.generate_json_array(
            msg_id=msg_id,
            system_msg=self.system_prompt,
            user_msgs=[user_prompt],
            schema=taxonomy_message,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            max_retries=1,
            client=self.client,
            task="annotation_audit",
        )

        accepted, dropped = [], []
        candidate = copy.deepcopy(message)
        for finding in findings:
            annotation, reason = verify_finding(candidate, http_key, finding, self.labels)
            if annotation is None:
                dropped.append({"finding": finding, "reason": reason})
            else:
                accepted.append(annotation)
                candidate["pii_annotations"].append(annotation)
        return accepted, dropped

    async def audit(self, step: PipelineStep, message: dict, http_key: str) -> dict:
        """
        Audit a message and add the missing annotations to it.

        The message gets an `audit` field with what happened; added annotations carry a `source`
        ('propagated' or 'audit'). Returns the audit record.
        """
        record = {"propagated": propagate_annotations(message, http_key), "added": 0, "rounds": 0,
                  "dropped": [], "complete": False}
        kind = http_key.split("_")[1]

        try:
            for round_index in range(1, self.rounds + 1):
                accepted, dropped = await self.find_missing(
                    step, message, http_key, f"{message.get('id')}_{kind}_audit{round_index}"
                )
                record["rounds"] = round_index
                record["dropped"].extend(dropped)
                if not accepted:
                    # a round without new findings: the annotations are complete as far as the auditor can tell
                    record["complete"] = True
                    break
                message["pii_annotations"].extend(accepted)
                record["added"] += len(accepted)
                for annotation in accepted:
                    self.added_labels[annotation["data_type"]] += 1
                record["propagated"] += propagate_annotations(message, http_key)
        except Exception as e:
            record["error"] = str(e)
            logger.info(f"[annotation_audit] Audit of {message.get('id')} failed: {e}")

        _refresh_data_types(message)
        message["audit"] = record

        self.stats["messages"] += 1
        self.stats["messages_with_additions"] += bool(record["added"] or record["propagated"])
        self.stats["annotations_added"] += record["added"]
        self.stats["annotations_propagated"] += record["propagated"]
        self.stats["findings_dropped"] += len(record["dropped"])
        self.stats["calls"] += record["rounds"]
        self.stats["not_confirmed_complete"] += not record["complete"]
        self.stats["failed"] += "error" in record
        for dropped in record["dropped"]:
            self.dropped_reasons[dropped["reason"]] += 1
        return record

    def summary(self) -> dict:
        return {
            **{key: self.stats.get(key, 0) for key in (
                "messages", "messages_with_additions", "annotations_added", "annotations_propagated",
                "findings_dropped", "calls", "not_confirmed_complete", "failed",
            )},
            "added_by_data_type": dict(self.added_labels.most_common()),
            "dropped_by_reason": dict(self.dropped_reasons.most_common()),
        }


# --------------------------------------------------------------------------- standalone use

class _AuditRunner(PipelineStep):
    """Minimal step that provides the LLM call for auditing outside of a pipeline run."""

    def __init__(self, run_id: str, client, concurrency: int):

        from src.state_manager import BaseStateManager
        super().__init__(
            run_id=run_id, task="annotation_audit", details="", skip=False, is_llm_step=True,
            in_folder="", out_folder="", state_manager=BaseStateManager(),
            client=client,
        )
        self.concurrency = concurrency

    async def execute(self, message: dict):
        pass


def _load_messages(output_folder: str) -> list[tuple[str, dict]]:
    messages = []
    for kind, folder, http_key in (("requests", "4_requests_validated", "http_request"),
                                   ("responses", "6_responses_validated", "http_response")):
        path = os.path.join(output_folder, folder, f"{kind}_validated.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                messages.extend((http_key, message) for message in json.load(f))
    return messages


async def audit_run(output_folder: str, auditor: AnnotationAuditor, runner: _AuditRunner, limit: int = None) -> dict:
    """Audit the validated messages of a run and write the audited messages to audit/."""
    runner.llm_semaphore = asyncio.Semaphore(runner.concurrency)
    messages = [(key, copy.deepcopy(m)) for key, m in _load_messages(output_folder)][:limit]
    await asyncio.gather(*(auditor.audit(runner, message, http_key) for http_key, message in messages))

    out_folder = os.path.join(output_folder, "audit")
    os.makedirs(out_folder, exist_ok=True)
    for kind, http_key in (("requests", "http_request"), ("responses", "http_response")):
        audited = [m for key, m in messages if key == http_key]
        if audited:
            with open(os.path.join(out_folder, f"{kind}_audited.json"), "w", encoding="utf-8") as f:
                json.dump(audited, f, indent=2, ensure_ascii=False)
    summary = auditor.summary()
    with open(os.path.join(out_folder, "audit_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


async def leave_one_out(output_folder: str, auditor: AnnotationAuditor, runner: _AuditRunner,
                        limit: int = None, seed: int = 0) -> dict:
    """
    Measure the recall of the auditor: remove one annotation from each message and check whether
    a single audit round finds it again.
    """
    runner.llm_semaphore = asyncio.Semaphore(runner.concurrency)
    rng = random.Random(seed)
    cases = []
    for http_key, message in _load_messages(output_folder):
        annotations = [a for a in message.get("pii_annotations", []) if isinstance(a.get("value"), str)]
        if message.get("negative_control", False) or len(annotations) < 2:
            continue
        removed = rng.choice(annotations)
        reduced = copy.deepcopy(message)
        # the value is removed for all components; an occurrence elsewhere would give it away as a hint
        reduced["pii_annotations"] = [a for a in reduced["pii_annotations"] if a.get("value") != removed["value"]]
        cases.append((http_key, reduced, removed))
    cases = cases[:limit]

    async def run(http_key, reduced, removed):
        try:
            accepted, _ = await auditor.find_missing(
                runner, reduced, http_key, f"{reduced.get('id')}_{http_key.split('_')[1]}_loo"
            )
        except Exception as e:
            return {"id": reduced.get("id"), "removed": removed, "error": str(e)}
        hits = [a for a in accepted if a["placement"] == removed["placement"]
                and (a["value"] == removed["value"] or removed["value"] in a["value"] or a["value"] in removed["value"])]
        return {
            "id": reduced.get("id"), "kind": http_key.split("_")[1],
            "removed": {k: removed.get(k) for k in ("data_type", "placement", "value")},
            "found": bool(hits),
            "same_label": any(a["data_type"] == removed["data_type"] for a in hits),
            "other_findings": [
                {k: a[k] for k in ("data_type", "placement", "value")} for a in accepted if a not in hits
            ],
        }

    results = await asyncio.gather(*(run(*case) for case in cases))
    valid = [r for r in results if "error" not in r]
    found = [r for r in valid if r["found"]]
    report = {
        "cases": len(results), "errors": len(results) - len(valid),
        "found": len(found), "recall": round(len(found) / max(1, len(valid)), 3),
        "found_with_same_label": sum(1 for r in found if r["same_label"]),
        "other_findings": sum(len(r["other_findings"]) for r in valid),
        "results": results,
    }
    out_folder = os.path.join(output_folder, "audit")
    os.makedirs(out_folder, exist_ok=True)
    with open(os.path.join(out_folder, "leave_one_out.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return report


def main(argv: list[str]) -> int:
    from dotenv import find_dotenv, load_dotenv

    from src.executor import PipelineExecutor
    from src.llm import SUPPORTED_APIS, create_client

    parser = argparse.ArgumentParser(
        prog="python -m src.audit", allow_abbrev=False,
        description="Audits the annotations of an existing run for PII values that are not annotated.")
    parser.add_argument("run_folder", metavar="RUN_FOLDER", help="folder of the run, e.g. ../output/<run id>")
    parser.add_argument("-schema", metavar="NAME|PATH", required=True,
                        help=f"label taxonomy: one of {util.bundled_taxonomies()} or the path of a Markdown file")
    parser.add_argument("-model", metavar="MODEL", required=True, help="model for the audit")
    parser.add_argument("-api", choices=SUPPORTED_APIS, default="openai", help="API of the model (default: openai)")
    parser.add_argument("-hostname", metavar="URL", help="base URL of a custom API endpoint")
    thinking_group = parser.add_mutually_exclusive_group()
    thinking_group.add_argument("-thinking", dest="thinking", action="store_true", default=None,
                                help="switch the reasoning of thinking-capable models on (only -api ollama)")
    thinking_group.add_argument("-no-thinking", dest="thinking", action="store_false", default=None,
                                help="switch the reasoning of thinking-capable models off (only -api ollama)")
    parser.add_argument("-prompts-dir", metavar="PATH", default=util.PROMPTS_DIR,
                        help="folder with the prompts (default: src/prompts)")
    parser.add_argument("-rounds", type=int, metavar="N", default=2, help="maximum audit rounds per message (default: 2)")
    parser.add_argument("-concurrency", type=int, metavar="N", default=1, help="number of parallel LLM calls (default: 1)")
    parser.add_argument("-limit", type=int, metavar="N", help="only audit the first N messages")
    parser.add_argument("-leave-one-out", action="store_true",
                        help="measure the recall of the audit: remove one annotation per message and check whether "
                             "the audit finds it again")
    args = parser.parse_args(argv or ["-h"])
    util.setup_logging()

    load_dotenv(find_dotenv(usecwd=True))
    run_folder = args.run_folder.rstrip("/")
    output_folder = os.path.join(run_folder, "output") if os.path.isdir(os.path.join(run_folder, "output")) else run_folder
    hostname = args.hostname or util.default_hostname(args.api)
    thinking, limit = args.thinking, args.limit

    schema_path = util.schema_path(args.schema)
    schema = util.read_from_file(schema_path)
    if not schema:
        logger.error(f"Error: could not load schema from {schema_path}")
        return 1
    labels = PipelineExecutor._extract_labels_from_schema(None, schema)

    run_id = os.path.basename(run_folder)
    client = create_client(args.api, run_id, args.model, hostname, os.path.join(run_folder, "log"), thinking)
    runner = _AuditRunner(run_id, client, args.concurrency)
    auditor = AnnotationAuditor(schema, labels, args.prompts_dir, rounds=args.rounds, thinking=thinking)

    if args.leave_one_out:
        report = asyncio.run(leave_one_out(output_folder, auditor, runner, limit))
        logger.info(f"\nLeave-one-out: {report['found']}/{report['cases'] - report['errors']} removed annotations found "
              f"again (recall {report['recall']:.0%}), {report['found_with_same_label']} with the same label; "
              f"{report['other_findings']} other findings")
    else:
        summary = asyncio.run(audit_run(output_folder, auditor, runner, limit))
        logger.info("\nAudit: " + json.dumps(summary, indent=2))
    client.close()
    logger.info(f"Written to {os.path.join(output_folder, 'audit')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
