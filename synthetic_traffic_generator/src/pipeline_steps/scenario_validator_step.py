"""
Pipeline step for validating generated scenarios against the label taxonomy.
"""

import json
import logging
import os
from typing import Any

from src import scenario_plan
from src.pipeline_steps.pipeline_step import PipelineStep
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)


class ScenarioValidatorStep(PipelineStep):
    """
    Pipeline step that validates scenario templates against the label taxonomy.

    Validates each scenario:
    - Required fields (scenario_id, scenario, goal, host_variants, etc.)
    - PII candidate labels against taxonomy, weight values in [0, 1], placement values
    - For templates of the current generator: type, hosts, endpoint variety, content types,
      and feasibility of the placements (see scenario_plan.validate_scenario)

    Describes the set:
    - Label coverage and balance, mix of scenario types and placements, deviations from the
      scenario profile
    - Forecast of how often each label will be planned for the messages of the set
    """

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
            profile: dict = None,
            sampler=None,
            requests_per_scenario: int = 10,
            omitted_labels: list[str] = None,
            rebalance: bool = True,
    ):
        super().__init__(
            run_id=run_id,
            task="scenario_validation",
            details="Validate scenarios against label taxonomy",
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
        self.profile = profile or scenario_plan.DEFAULT_PROFILE
        self.sampler = sampler
        self.requests_per_scenario = requests_per_scenario
        self.omitted_labels = list(omitted_labels or [])
        self.rebalance = rebalance
        self.valid_scenarios = []
        self.invalid_scenarios = []
        self.all_errors = []
        self.labels_covered = set()

    def start(self):
        self.valid_scenarios = []
        self.invalid_scenarios = []
        self.all_errors = []
        self.labels_covered = set()

    async def execute(self, message: dict) -> dict[str, Any]:
        """
        Validate a single scenario.

        Args:
            message: Scenario object to validate

        Returns:
            Validation result dict
        """
        scenario_id = message.get("scenario_id", "unknown")

        # Bookkeeping entry of scenario sets of earlier versions; not a scenario
        if scenario_id == "OMISSIONS":
            return {"is_valid": True, "scenario_id": scenario_id}

        # templates of the current generator state their type and are held to all rules
        strict = "type" in message
        scenario_plan.normalise_scenario(message)
        errors = scenario_plan.validate_scenario(message, self.labels or [], self.profile, strict=strict)
        labels_found = set(scenario_plan.scenario_labels(message)) & self.taxonomy_labels

        is_valid = len(errors) == 0
        self._record_result(message, is_valid, errors, labels_found)

        return {
            "is_valid": is_valid,
            "errors": errors,
            "labels_found": list(labels_found),
            "scenario_id": scenario_id
        }

    def _record_result(self, scenario: dict, is_valid: bool, errors: list, labels: set):
        """Record validation result."""
        if is_valid:
            self.valid_scenarios.append(scenario)
            self.labels_covered.update(labels)
        else:
            self.invalid_scenarios.append({
                "scenario": scenario,
                "errors": errors
            })
            self.all_errors.extend(errors)
            for err in errors:
                logger.info(f"  [INVALID] {err}")

    async def finalize(self):
        """Finalize validation and write summary."""
        os.makedirs(self.out_folder, exist_ok=True)
        labels = list(self.labels or [])

        # Raise the weights of labels that would be planned too rarely (changes the valid scenarios)
        rebalancing = None
        if self.rebalance and self.sampler is not None and self.valid_scenarios:
            rebalancing = scenario_plan.rebalance_weights(
                self.valid_scenarios, self.sampler, self.requests_per_scenario, labels, self.profile
            )

        # Write valid scenarios as JSON array
        valid_path = os.path.join(self.out_folder, "scenarios_validated.json")
        with open(valid_path, "w", encoding="utf-8") as f:
            json.dump(self.valid_scenarios, f, indent=2, ensure_ascii=False)

        # Write valid scenarios as JSONL
        jsonl_path = os.path.join(self.out_folder, "scenarios_validated.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for scenario in self.valid_scenarios:
                f.write(json.dumps(scenario, ensure_ascii=False) + "\n")

        # Write invalid scenarios
        invalid_path = os.path.join(self.out_folder, "scenarios_invalid.json")
        if self.invalid_scenarios:
            with open(invalid_path, "w", encoding="utf-8") as f:
                json.dump(self.invalid_scenarios, f, indent=2, ensure_ascii=False)
        elif os.path.exists(invalid_path):
            os.remove(invalid_path)

        # Calculate coverage
        coverage_pct = (
            len(self.labels_covered) / len(self.taxonomy_labels) * 100
            if self.taxonomy_labels else 0
        )
        missing_labels = self.taxonomy_labels - self.labels_covered
        report = scenario_plan.set_report(self.valid_scenarios, labels, self.profile)

        # Write validation summary
        summary = {
            "total_scenarios": len(self.valid_scenarios) + len(self.invalid_scenarios),
            "valid_scenarios": len(self.valid_scenarios),
            "invalid_scenarios": len(self.invalid_scenarios),
            "validation_rate": len(self.valid_scenarios) / max(1, len(self.valid_scenarios) + len(self.invalid_scenarios)),
            "labels_covered": len(self.labels_covered),
            "total_labels": len(self.taxonomy_labels),
            "coverage_pct": coverage_pct,
            "missing_labels": sorted(missing_labels),
            "omitted_labels": self.omitted_labels,
            "error_count": len(self.all_errors),
            "set": report,
            "weight_rebalancing": rebalancing,
        }

        summary_path = os.path.join(self.out_folder, "validation_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        logger.info("\n[ScenarioValidator] Validation complete:")
        logger.info(f"  Valid: {len(self.valid_scenarios)}, Invalid: {len(self.invalid_scenarios)}")
        logger.info(f"  Label coverage: {coverage_pct:.1f}% ({len(self.labels_covered)}/{len(self.taxonomy_labels)})")
        if missing_labels:
            logger.info(f"  Missing labels: {sorted(missing_labels)}")
        if self.omitted_labels:
            logger.info(f"  Omitted labels (not part of the run): {self.omitted_labels}")
        per_label = report["scenarios_per_label"]
        logger.info(f"  Scenarios per label (min/median/max): {per_label['min']}/{per_label['median']}/{per_label['max']}, "
              f"labels in only one scenario: {report['labels_in_one_scenario']}")
        logger.info(f"  Types: {report['types']}, placements outside the body: {report['non_body_placement_share']:.0%}")
        for deviation in report["deviations"]:
            logger.info(f"  DEVIATION: {deviation[:200]}")

        if rebalancing and rebalancing["changes"]:
            logger.info(f"  Rebalanced {len(rebalancing['changes'])} weights of {len(rebalancing['planned_before'])} "
                  f"rarely planned labels (target: {rebalancing['target_planned_per_label']} per label): "
                  f"{rebalancing['planned_before']} -> {rebalancing['planned_after']}")
        if rebalancing and rebalancing["labels_below_target"]:
            logger.info(f"  Still below the target with all weights at 1.0 (add these labels to more scenarios): "
                  f"{rebalancing['labels_below_target']}")

        # Forecast of the planned annotations (no LLM needed)
        if self.sampler is not None and self.valid_scenarios:
            forecast = scenario_plan.forecast(
                self.valid_scenarios, self.sampler, self.requests_per_scenario, labels, self.profile
            )
            with open(os.path.join(self.out_folder, "coverage_forecast.json"), "w", encoding="utf-8") as f:
                json.dump(forecast, f, indent=2, ensure_ascii=False)
            logger.info(f"  Forecast for {forecast['requests']} requests: {forecast['planned_annotations']} planned "
                  f"annotations (median {forecast['median_per_label']} per label), placements {forecast['placements']}")
            if forecast["labels_never_planned"]:
                logger.info(f"  FORECAST: never planned: {forecast['labels_never_planned']}")
            if forecast["rare_labels"]:
                logger.info(f"  FORECAST: rarely planned: {forecast['rare_labels']}")
