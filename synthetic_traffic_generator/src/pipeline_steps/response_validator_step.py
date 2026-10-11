"""
Pipeline step for validating generated HTTP responses.
"""

import json
import logging
import os
import re
from collections import Counter
from typing import Any

from src import positions
from src.diversity import diversity_report
from src.pipeline_steps import message_validation
from src.pipeline_steps.pipeline_step import PipelineStep
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)


class ResponseValidatorStep(PipelineStep):
    """
    Pipeline step that validates generated HTTP responses.

    Validates:
    - Required fields (id, scenario, http_response, pii_annotations, data_types_present)
    - HTTP response structure (http_version, status_code, headers, body)
    - PII annotation structure (data_type, placement, value)
    - Taxonomy adherence (data_type values match taxonomy)
    - PII value presence (annotated values appear verbatim in the component named by their placement)
    - Negative controls (responses of negative-control scenarios carry no annotations)
    - Uniqueness of response IDs
    """

    VALID_PLACEMENTS = {"header", "body"}
    REQUIRED_FIELDS = ["id", "scenario", "http_response", "pii_annotations", "data_types_present"]
    REQUIRED_HTTP_FIELDS = ["http_version", "status_code", "headers"]

    def __init__(
            self,
            run_id: str,
            state_manager: BaseStateManager,
            in_folder: str,
            out_folder: str,
            skip: bool = False,
            schema: str = "",
            schema_name: str = "",
            labels: list[str] = None,
            pool=None,
            min_length_ratio: float = 0.6,
            requests_path: str = None,
    ):
        super().__init__(
            run_id=run_id,
            task="response_validation",
            details="Validate generated HTTP responses",
            skip=skip,
            is_llm_step=False,
            in_folder=in_folder,
            out_folder=out_folder,
            state_manager=state_manager,
            schema=schema,
            schema_name=schema_name,
            labels=labels
        )

        self.taxonomy_labels = set(labels) if labels else set()
        self.requests_path = requests_path
        self.requests = {}
        self.pool = pool
        self.min_length_ratio = min_length_ratio
        self.start()

    def start(self):
        self.valid_responses = []
        self.invalid_responses = []
        self.all_errors = []
        self.labels_covered = set()
        self.label_counts = Counter()
        self.seen_ids = set()
        self.requests = self._load_requests()

    def _load_requests(self) -> dict:
        """Load the requests the responses belong to (by id), to check that responses fit them."""
        path = self.requests_path
        if not path:
            return {}
        if os.path.isdir(path):
            for filename in ("requests_validated.json", "all_requests.json"):
                candidate = os.path.join(path, filename)
                if os.path.exists(candidate):
                    path = candidate
                    break
            else:
                return {}
        try:
            with open(path, encoding="utf-8") as f:
                requests = json.load(f)
        except (OSError, ValueError):
            return {}
        return {r["id"]: r for r in requests if isinstance(r, dict) and isinstance(r.get("id"), str)}

    async def execute(self, message: dict) -> dict[str, Any]:
        """
        Validate a single HTTP response.

        Args:
            message: Response object to validate

        Returns:
            Validation result dict
        """
        response_id = message.get("id", "unknown") if isinstance(message, dict) else "unknown"
        errors, labels_found = self.validate(message)

        if isinstance(response_id, str):
            if response_id in self.seen_ids:
                errors.append(f"Response {response_id}: Duplicate response id")
            self.seen_ids.add(response_id)

        is_valid = len(errors) == 0
        self._record_result(message, is_valid, errors, labels_found)

        return {
            "is_valid": is_valid,
            "errors": errors,
            "labels_found": list(labels_found),
            "response_id": response_id
        }

    def validate(self, message: dict) -> tuple[list[str], set[str]]:
        """
        Validate a single HTTP response without recording the result.

        Returns:
            Tuple of (errors, taxonomy labels found in the response)
        """
        if not isinstance(message, dict):
            return ["Response unknown: Must be a JSON object"], set()

        idx = message.get("id", "unknown")
        prefix = f"Response {idx}"

        # 1. Structure validation
        errors = self._validate_structure(message)
        if errors:
            # If structure is broken, skip other validations
            return errors, set()

        # 2. HTTP format validation
        errors.extend(self._validate_http_format(message))

        # 3. Taxonomy adherence
        taxonomy_errors, labels_found = message_validation.validate_taxonomy(message, self.taxonomy_labels, prefix)
        errors.extend(taxonomy_errors)

        # 4. PII value presence in the specified message component, and its exact position
        placement_errors = message_validation.validate_value_placement(
            message, message_validation.response_components(message["http_response"]), prefix
        )
        errors.extend(placement_errors)
        if not placement_errors:
            errors.extend(positions.resolve_positions(message, "http_response", prefix))

        # 5. Negative controls must not carry annotations
        errors.extend(message_validation.validate_negative_control(message, prefix))

        # 6. Body matches the declared content type
        errors.extend(message_validation.validate_body_format(message["http_response"], prefix))

        errors.extend(message_validation.validate_identifiers(message["http_response"], prefix))
        errors.extend(message_validation.validate_plaintext(message["http_response"], prefix))

        # 7. Fit to the request the response belongs to
        request = self.requests.get(idx) if isinstance(idx, str) else None
        if request:
            errors.extend(message_validation.validate_response_fit(message, request, prefix))

        # 8. Adherence to the blueprint (body length, no values copied from the exemplar)
        errors.extend(message_validation.validate_blueprint(
            message, message["http_response"], self.pool, prefix, self.min_length_ratio
        ))

        return errors, labels_found

    def _validate_structure(self, response: dict) -> list[str]:
        """Validate response adheres to required schema."""
        errors = []
        idx = response.get("id", "unknown")

        for field in self.REQUIRED_FIELDS:
            if field not in response:
                errors.append(f"Response {idx}: Missing required field '{field}'")

        if "id" in response and not isinstance(response["id"], str):
            errors.append(f"Response {idx}: Field 'id' must be a string")

        if "http_response" in response and not isinstance(response["http_response"], dict):
            errors.append(f"Response {idx}: Field 'http_response' must be a JSON object")

        errors.extend(message_validation.validate_annotation_structure(
            response, self.VALID_PLACEMENTS, f"Response {idx}"
        ))

        return errors

    def _validate_http_format(self, response: dict) -> list[str]:
        """Validate HTTP response format."""
        errors = []
        idx = response.get("id", "unknown")
        http_res = response["http_response"]

        for field in self.REQUIRED_HTTP_FIELDS:
            if field not in http_res:
                errors.append(f"Response {idx}: http_response missing '{field}'")

        if "http_version" in http_res:
            version = http_res["http_version"]
            if not isinstance(version, str) or not re.match(r"^HTTP/\d\.\d$", version):
                errors.append(f"Response {idx}: Invalid http_version format '{version}'")

        if "status_code" in http_res:
            status_code = http_res["status_code"]
            if isinstance(status_code, bool) or not isinstance(status_code, int) or not 100 <= status_code <= 599:
                errors.append(f"Response {idx}: Invalid status_code '{status_code}'")

        if "reason" in http_res and not isinstance(http_res["reason"], str):
            errors.append(f"Response {idx}: http_response.reason must be a string")

        if "body" in http_res and http_res["body"] is not None and not isinstance(http_res["body"], str):
            errors.append(f"Response {idx}: http_response.body must be a string")
        elif http_res.get("status_code") in (204, 304) and http_res.get("body"):
            errors.append(f"Response {idx}: A {http_res['status_code']} response must not have a body")

        if "headers" in http_res:
            errors.extend(message_validation.validate_headers(http_res["headers"], f"Response {idx}"))

        return errors

    def _record_result(self, response: dict, is_valid: bool, errors: list, labels: set):
        """Record validation result."""
        if is_valid:
            self.valid_responses.append(response)
            self.labels_covered.update(labels)
            self.label_counts.update(labels)
        else:
            self.invalid_responses.append({
                "response": response,
                "errors": errors
            })
            self.all_errors.extend(errors)
            # Log errors
            for err in errors:
                logger.info(f"  [INVALID] {err}")

    async def finalize(self):
        """Finalize validation and write summary."""
        os.makedirs(self.out_folder, exist_ok=True)

        # Write valid responses
        valid_path = os.path.join(self.out_folder, "responses_validated.json")
        with open(valid_path, "w", encoding="utf-8") as f:
            json.dump(self.valid_responses, f, indent=2, ensure_ascii=False)

        # Write valid responses as JSONL
        jsonl_path = os.path.join(self.out_folder, "responses_validated.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for res in self.valid_responses:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")

        # Write invalid responses (remove a stale file from a previous run otherwise)
        invalid_path = os.path.join(self.out_folder, "responses_invalid.json")
        if self.invalid_responses:
            with open(invalid_path, "w", encoding="utf-8") as f:
                json.dump(self.invalid_responses, f, indent=2, ensure_ascii=False)
        elif os.path.exists(invalid_path):
            os.remove(invalid_path)

        # Calculate coverage
        coverage_pct = (
            len(self.labels_covered) / len(self.taxonomy_labels) * 100
            if self.taxonomy_labels else 0
        )
        missing_labels = self.taxonomy_labels - self.labels_covered

        # Write validation summary
        total = len(self.valid_responses) + len(self.invalid_responses)
        summary = {
            "total_responses": total,
            "valid_responses": len(self.valid_responses),
            "invalid_responses": len(self.invalid_responses),
            "validation_rate": len(self.valid_responses) / max(1, total),
            "labels_covered": len(self.labels_covered),
            "total_labels": len(self.taxonomy_labels),
            "coverage_pct": coverage_pct,
            "missing_labels": sorted(missing_labels),
            "label_counts": {label: self.label_counts.get(label, 0) for label in sorted(self.taxonomy_labels)},
            "error_count": len(self.all_errors),
            "audit": {
                "audited": sum(1 for m in self.valid_responses if "audit" in m),
                "confirmed_complete": sum(1 for m in self.valid_responses if (m.get("audit") or {}).get("complete")),
                "annotations_added": sum(
                    1 for m in self.valid_responses for a in m.get("pii_annotations", []) if a.get("source")
                ),
            },
            "diversity": diversity_report(self.valid_responses),
            "realism": message_validation.realism_report(
                self.valid_responses, "http_response", (self.pool.profile.get("responses") if self.pool else None)
            ),
        }

        summary_path = os.path.join(self.out_folder, "validation_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        logger.info("\n[ResponseValidator] Validation complete:")
        logger.info(f"  Valid: {len(self.valid_responses)}, Invalid: {len(self.invalid_responses)}")
        logger.info(f"  Validation rate: {summary['validation_rate']:.1%}")
        diversity = summary["diversity"]
        logger.info(f"  Diversity: unique paths {diversity.get('unique_path_share')}, "
              f"unique PII values {diversity.get('unique_pii_value_share')}, "
              f"body key similarity {diversity.get('body_key_similarity_mean')}")
        realism = summary["realism"]
        logger.info(f"  Body length (p50/p90/max): {realism['body_length'].get('p50')}/"
              f"{realism['body_length'].get('p90')}/{realism['body_length'].get('max')}, "
              f"PII share of body characters (median): {realism['pii_char_share_median']}")
        logger.info(f"  Label coverage: {coverage_pct:.1f}% ({len(self.labels_covered)}/{len(self.taxonomy_labels)})")
