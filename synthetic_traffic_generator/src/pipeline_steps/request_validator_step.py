"""
Pipeline step for validating generated HTTP requests.
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


class RequestValidatorStep(PipelineStep):
    """
    Pipeline step that validates generated HTTP requests.
    
    Validates:
    - Required fields (id, scenario, http_request, pii_annotations, data_types_present)
    - HTTP request structure (method, path, headers, body)
    - PII annotation structure (data_type, placement, value)
    - Taxonomy adherence (data_type values match taxonomy)
    - PII value presence (annotated values appear verbatim in the component named by their placement)
    - Negative controls (requests of negative-control scenarios carry no annotations)
    """

    VALID_PLACEMENTS = {"path", "query", "header", "body"}
    HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    REQUIRED_FIELDS = ["id", "scenario", "http_request", "pii_annotations", "data_types_present"]
    REQUIRED_HTTP_FIELDS = ["method", "path", "http_version", "headers"]

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
    ):
        super().__init__(
            run_id=run_id,
            task="request_validation",
            details="Validate generated HTTP requests",
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
        self.pool = pool
        self.min_length_ratio = min_length_ratio
        self.valid_requests = []
        self.invalid_requests = []
        self.all_errors = []
        self.labels_covered = set()
        self.label_counts = Counter()

    def start(self):
        self.valid_requests = []
        self.invalid_requests = []
        self.all_errors = []
        self.labels_covered = set()
        self.label_counts = Counter()

    async def execute(self, message: dict) -> dict[str, Any]:
        """
        Validate a single HTTP request.
        
        Args:
            message: Request object to validate
            
        Returns:
            Validation result dict
        """
        request_id = message.get("id", "unknown") if isinstance(message, dict) else "unknown"
        errors, labels_found = self.validate(message)

        is_valid = len(errors) == 0
        self._record_result(message, is_valid, errors, labels_found)

        return {
            "is_valid": is_valid,
            "errors": errors,
            "labels_found": list(labels_found),
            "request_id": request_id
        }

    def validate(self, message: dict) -> tuple[list[str], set[str]]:
        """
        Validate a single HTTP request without recording the result.

        Returns:
            Tuple of (errors, taxonomy labels found in the request)
        """
        if not isinstance(message, dict):
            return ["Request unknown: Must be a JSON object"], set()

        idx = message.get("id", "unknown")
        prefix = f"Request {idx}"

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
            message, message_validation.request_components(message["http_request"]), prefix
        )
        errors.extend(placement_errors)
        if not placement_errors:
            errors.extend(positions.resolve_positions(message, "http_request", prefix))

        # 5. Negative controls must not carry annotations
        errors.extend(message_validation.validate_negative_control(message, prefix))

        # 6. Body matches the declared content type
        errors.extend(message_validation.validate_body_format(message["http_request"], prefix))

        errors.extend(message_validation.validate_identifiers(message["http_request"], prefix))
        errors.extend(message_validation.validate_plaintext(message["http_request"], prefix))

        # 7. Adherence to the blueprint (body length, no values copied from the exemplar)
        errors.extend(message_validation.validate_blueprint(
            message, message["http_request"], self.pool, prefix, self.min_length_ratio
        ))

        return errors, labels_found

    def _validate_structure(self, request: dict) -> list[str]:
        """Validate request adheres to required schema."""
        errors = []
        idx = request.get("id", "unknown")

        for field in self.REQUIRED_FIELDS:
            if field not in request:
                errors.append(f"Request {idx}: Missing required field '{field}'")

        if "id" in request and not isinstance(request["id"], str):
            errors.append(f"Request {idx}: Field 'id' must be a string")

        if "http_request" in request and not isinstance(request["http_request"], dict):
            errors.append(f"Request {idx}: Field 'http_request' must be a JSON object")

        errors.extend(message_validation.validate_annotation_structure(
            request, self.VALID_PLACEMENTS, f"Request {idx}"
        ))

        return errors

    def _validate_http_format(self, request: dict) -> list[str]:
        """Validate HTTP request format."""
        errors = []
        idx = request.get("id", "unknown")

        http_req = request.get("http_request", {})
        if not isinstance(http_req, dict):
            return errors

        for field in self.REQUIRED_HTTP_FIELDS:
            if field not in http_req:
                errors.append(f"Request {idx}: http_request missing '{field}'")

        if "method" in http_req:
            method = http_req["method"]
            if not isinstance(method, str) or method.upper() not in self.HTTP_METHODS:
                errors.append(f"Request {idx}: Invalid HTTP method '{method}'")

        if "path" in http_req:
            path = http_req["path"]
            if not isinstance(path, str) or not path.startswith("/"):
                errors.append(f"Request {idx}: http_request.path must start with '/'")

        if "http_version" in http_req:
            version = http_req["http_version"]
            if not isinstance(version, str) or not re.match(r"^HTTP/\d\.\d$", version):
                errors.append(f"Request {idx}: Invalid http_version format '{version}'")

        if "headers" in http_req:
            errors.extend(message_validation.validate_headers(http_req["headers"], f"Request {idx}"))

        return errors

    def _record_result(self, request: dict, is_valid: bool, errors: list, labels: set):
        """Record validation result."""
        if is_valid:
            self.valid_requests.append(request)
            self.labels_covered.update(labels)
            self.label_counts.update(labels)
        else:
            self.invalid_requests.append({
                "request": request,
                "errors": errors
            })
            self.all_errors.extend(errors)
            # Log errors
            for err in errors:
                logger.info(f"  [INVALID] {err}")

    async def finalize(self):
        """Finalize validation and write summary."""
        os.makedirs(self.out_folder, exist_ok=True)

        # Write valid requests
        valid_path = os.path.join(self.out_folder, "requests_validated.json")
        with open(valid_path, "w", encoding="utf-8") as f:
            json.dump(self.valid_requests, f, indent=2, ensure_ascii=False)

        # Write valid requests as JSONL
        jsonl_path = os.path.join(self.out_folder, "requests_validated.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for req in self.valid_requests:
                f.write(json.dumps(req, ensure_ascii=False) + "\n")

        # Write invalid requests
        invalid_path = os.path.join(self.out_folder, "requests_invalid.json")
        if self.invalid_requests:
            with open(invalid_path, "w", encoding="utf-8") as f:
                json.dump(self.invalid_requests, f, indent=2, ensure_ascii=False)
        elif os.path.exists(invalid_path):
            os.remove(invalid_path)

        # Calculate coverage
        coverage_pct = (
            len(self.labels_covered) / len(self.taxonomy_labels) * 100
            if self.taxonomy_labels else 0
        )
        missing_labels = self.taxonomy_labels - self.labels_covered

        # Write validation summary
        total = len(self.valid_requests) + len(self.invalid_requests)
        summary = {
            "total_requests": total,
            "valid_requests": len(self.valid_requests),
            "invalid_requests": len(self.invalid_requests),
            "validation_rate": len(self.valid_requests) / max(1, total),
            "labels_covered": len(self.labels_covered),
            "total_labels": len(self.taxonomy_labels),
            "coverage_pct": coverage_pct,
            "missing_labels": sorted(missing_labels),
            "label_counts": {label: self.label_counts.get(label, 0) for label in sorted(self.taxonomy_labels)},
            "negative_control_requests": sum(1 for r in self.valid_requests if r.get("negative_control", False)),
            "error_count": len(self.all_errors),
            "audit": {
                "audited": sum(1 for m in self.valid_requests if "audit" in m),
                "confirmed_complete": sum(1 for m in self.valid_requests if (m.get("audit") or {}).get("complete")),
                "annotations_added": sum(
                    1 for m in self.valid_requests for a in m.get("pii_annotations", []) if a.get("source")
                ),
            },
            "diversity": diversity_report(self.valid_requests),
            "realism": message_validation.realism_report(
                self.valid_requests, "http_request", (self.pool.profile.get("requests") if self.pool else None)
            ),
        }

        summary_path = os.path.join(self.out_folder, "validation_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        logger.info("\n[RequestValidator] Validation complete:")
        logger.info(f"  Valid: {len(self.valid_requests)}, Invalid: {len(self.invalid_requests)}")
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
        if missing_labels:
            logger.info(f"  Missing labels: {sorted(missing_labels)}")
