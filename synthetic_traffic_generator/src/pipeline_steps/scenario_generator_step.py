"""
Pipeline step for generating scenario templates from a PII label taxonomy.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

from src import scenario_plan, util
from src.pipeline_steps.pipeline_step import PipelineStep
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)

NEGATIVE_CONTROL_NOTE = (
    "This scenario is a **negative control**: its requests and responses must not contain any PII or data "
    "that matches the taxonomy. `pii_candidates` and `response_pii_candidates` are empty arrays. State in the "
    "`notes` that requests carry no cookies, authorization headers, or user or device identifiers."
)


class ScenarioGeneratorStep(PipelineStep):
    """
    Pipeline step that generates scenario templates from a label taxonomy using an LLM.

    Input: Label taxonomy (from schema)
    Output: JSON array of scenario templates

    Generation takes two steps. First, the *outline* of the whole set is planned in one call:
    the scenarios with their types and labels. The outline is checked against the scenario
    profile (coverage and balance of the labels, mix of types) and repaired with feedback until
    it complies. Then the template of each scenario is written by a call of its own, validated,
    and regenerated individually if it is invalid.
    """

    # without scenarios, no subsequent step can run
    abort_on_error = True

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
            n_scenarios: int = 30,
            negative_controls: int = 2,
            profile: dict = None,
            temperature: float = 0.7,
            max_tokens: int = 32000,
            timeout: float = 600.0,
            max_retries: int = 2,
            prompts_dir: str = util.PROMPTS_DIR,
    ):
        super().__init__(
            run_id=run_id,
            task="scenario_generation",
            details="Generate scenario templates from label taxonomy",
            skip=skip,
            is_llm_step=True,
            in_folder=in_folder,
            out_folder=out_folder,
            state_manager=state_manager,
            schema=schema,
            schema_name=schema_name,
            labels=labels
        )

        self.n_scenarios = n_scenarios
        self.negative_controls = max(0, negative_controls)
        self.profile = profile or scenario_plan.DEFAULT_PROFILE
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.prompts_dir = prompts_dir

        # Load prompts
        self.outline_system_prompt = util.read_from_file(os.path.join(prompts_dir, "scenario_outline_system.md"))
        self.outline_user_template = util.read_from_file(os.path.join(prompts_dir, "scenario_outline_user.md"))
        self.system_prompt = util.read_from_file(os.path.join(prompts_dir, "scenario_generator_system.md"))
        self.user_prompt_template = util.read_from_file(os.path.join(prompts_dir, "scenario_generator_user.md"))

        # Generated scenarios will be stored here
        self.scenarios = []

    # ------------------------------------------------------------------ outline

    def _render_outline_prompt(self, feedback: list[str] = None, previous: list = None) -> str:
        limits, _ = scenario_plan.effective_limits(len(self.labels), self.n_scenarios, self.profile)
        targets = self._type_targets()

        prompt = self.outline_user_template.replace("{{LABELS_TAXONOMY}}", self.schema)
        prompt = prompt.replace("{{N_SCENARIOS}}", str(self.n_scenarios))
        prompt = prompt.replace("{{N_LABELS}}", str(len(self.labels)))
        prompt = prompt.replace("{{TYPE_TARGETS}}", ", ".join(f"about {n} `{t}`" for t, n in targets.items()))
        prompt = prompt.replace("{{MIN_PER_LABEL}}", str(limits["min_scenarios_per_label"]))
        prompt = prompt.replace("{{MAX_PER_LABEL}}", str(limits["max_scenarios_per_label"]))
        prompt = prompt.replace("{{MAX_LABELS}}", str(limits["max_labels_per_scenario"]))
        prompt = prompt.replace("{{MAX_RESPONSE_LABELS}}", str(self.profile["max_response_labels_per_scenario"]))

        feedback_text = ""
        if feedback and previous is not None:
            feedback_text = (
                "## Revision\n\nYour previous plan does not meet the requirements. Revise it: keep what is good, "
                "and fix the following problems.\n\n" + "\n".join(f"- {problem}" for problem in feedback)
                + "\n\n<PREVIOUS_PLAN_JSON>\n" + json.dumps(previous, indent=1, ensure_ascii=False)
                + "\n</PREVIOUS_PLAN_JSON>"
            )
        return prompt.replace("{{FEEDBACK}}", feedback_text)

    def _type_targets(self) -> dict[str, int]:
        """Number of scenarios per type that the profile asks for."""
        shares = self.profile["type_shares"]
        targets = {t: int(share * self.n_scenarios) for t, share in shares.items()}
        # distribute the remainder to the types with the largest shares
        for scenario_type in sorted(shares, key=shares.get, reverse=True):
            if sum(targets.values()) >= self.n_scenarios:
                break
            targets[scenario_type] += 1
        return targets

    async def _generate_outline(self) -> dict[str, Any]:
        """Plan the scenario set, and repair the plan until it meets the profile."""
        feedback, previous = None, None
        best = None
        rounds = 0

        for attempt in range(self.max_retries + 1):
            rounds = attempt + 1
            outline = await self.generate_json_array(
                msg_id=f"outline_{rounds}",
                system_msg=self.outline_system_prompt,
                user_msgs=[self._render_outline_prompt(feedback, previous)],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
            errors, warnings = scenario_plan.check_outline(outline, self.labels, self.n_scenarios, self.profile)
            logger.warning(f"[ScenarioGenerator] Outline {rounds}: {len(outline)} scenarios, "
                  f"{len(errors)} errors, {len(warnings)} deviations from the profile")
            for problem in errors + warnings:
                logger.info(f"  - {problem[:220]}")

            candidate = {"outline": outline, "errors": errors, "warnings": warnings}
            if best is None or (len(errors), len(warnings)) < (len(best["errors"]), len(best["warnings"])):
                best = candidate
            if not errors and not warnings:
                break
            feedback, previous = errors + warnings, outline

        best["rounds"] = rounds
        return best

    # ------------------------------------------------------------------ templates

    def _render_template_prompt(self, entry: dict, feedback: list[str] = None) -> str:
        plan = {
            key: entry[key] for key in ("scenario", "goal", "app", "type", "request_labels", "response_labels")
            if entry.get(key) is not None
        }
        notes = []
        if entry.get("negative_control"):
            notes.append(NEGATIVE_CONTROL_NOTE)
        if feedback:
            notes.append(
                "Your previous template was rejected by the validator. Avoid these errors:\n"
                + "\n".join(f"- {error}" for error in feedback[:12])
            )
        prompt = self.user_prompt_template.replace("{{LABELS_TAXONOMY}}", self.schema)
        prompt = prompt.replace("{{SCENARIO_PLAN_JSON}}", json.dumps(plan, indent=2, ensure_ascii=False))
        return prompt.replace("{{NOTES}}", "\n\n".join(notes))

    async def _generate_template(self, entry: dict) -> dict[str, Any]:
        """Write the template of one scenario; regenerate it with feedback if it is invalid."""
        scenario_id = entry["scenario_id"]
        feedback, last_error = None, None

        for attempt in range(self.max_retries + 1):
            try:
                generated = await self.generate_json_array(
                    msg_id=f"{scenario_id}_{attempt + 1}",
                    system_msg=self.system_prompt,
                    user_msgs=[self._render_template_prompt(entry, feedback)],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                    max_retries=0,  # failed calls are repeated by this loop
                )
            except Exception as e:
                last_error = str(e)
                logger.info(f"[ScenarioGenerator] Call for {scenario_id} failed: {e}")
                continue

            scenario = generated[0] if generated and isinstance(generated[0], dict) else None
            if scenario is None:
                feedback = ["The output must be a JSON array with one scenario object"]
                continue

            # what the plan fixed is set here, so that the template cannot deviate from it
            scenario = {
                "scenario_id": scenario_id,
                "scenario": entry["scenario"],
                "goal": scenario.get("goal") or entry["goal"],
                "type": entry["type"],
                **{k: v for k, v in scenario.items() if k not in ("scenario_id", "scenario", "goal", "type")},
            }
            if entry.get("app"):
                scenario["app"] = entry["app"]
            if entry.get("negative_control"):
                scenario["negative_control"] = True
            scenario_plan.normalise_scenario(scenario)

            errors = scenario_plan.validate_scenario(scenario, self.labels, self.profile, entry, strict=True)
            if not errors:
                return {"scenario": scenario, "attempts": attempt + 1}
            logger.info(f"[ScenarioGenerator] Template of {scenario_id} rejected ({len(errors)} errors): {errors[0][:160]}")
            feedback, last_error = [e.split(": ", 1)[-1] for e in errors], errors[0]

        return {"scenario": None, "attempts": self.max_retries + 1, "error": last_error}

    async def execute(self, message: dict) -> list[dict[str, Any]]:
        """
        Execute scenario generation.

        This step doesn't process individual messages - it generates scenarios
        from the taxonomy schema. The message parameter is ignored.
        """
        if not all((self.outline_system_prompt, self.outline_user_template,
                    self.system_prompt, self.user_prompt_template)):
            raise ValueError("Scenario generator prompts not loaded")
        if not self.schema or not self.labels:
            raise ValueError("No label schema provided for scenario generation")

        limits, notes = scenario_plan.effective_limits(len(self.labels), self.n_scenarios, self.profile)
        logger.info(f"\n[ScenarioGenerator] Planning {self.n_scenarios} scenarios "
              f"(+ {self.negative_controls} negative controls) for {len(self.labels)} labels...")
        logger.info(f"  Model: {self.model}")
        logger.info(f"  Temperature: {self.temperature}")
        for note in notes:
            logger.info(f"  NOTE: {note}")

        # Step A: the outline of the set
        plan = await self._generate_outline()
        if plan["errors"]:
            raise RuntimeError(
                f"No usable scenario outline after {plan['rounds']} round(s): " + "; ".join(plan["errors"][:3])
            )
        outline = plan["outline"]
        for index, entry in enumerate(outline, start=1):
            entry["scenario_id"] = f"S{index:02d}"
        entries = outline + scenario_plan.negative_control_entries(self.negative_controls)

        # Step B: one template per scenario
        logger.info(f"[ScenarioGenerator] Writing {len(entries)} scenario templates...")
        results = await asyncio.gather(*(self._generate_template(entry) for entry in entries))
        scenarios = [result["scenario"] for result in results if result["scenario"]]
        failed = [entry["scenario_id"] for entry, result in zip(entries, results) if not result["scenario"]]

        # labels that are only planned for a scenario that could not be written are lost
        covered = {label for scenario in scenarios for label in scenario_plan.scenario_labels(scenario)}
        lost = [label for label in self.labels if label not in covered]
        logger.info(f"[ScenarioGenerator] Generated {len(scenarios)}/{len(entries)} scenarios"
              f"{' (failed: ' + ', '.join(failed) + ')' if failed else ''}")
        if not scenarios:
            raise RuntimeError("No scenario template could be generated")

        # Save scenarios to output folder
        os.makedirs(self.out_folder, exist_ok=True)
        with open(os.path.join(self.out_folder, "scenarios.json"), "w", encoding="utf-8") as f:
            json.dump(scenarios, f, indent=2, ensure_ascii=False)
        with open(os.path.join(self.out_folder, "outline.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)

        # Also save metadata
        metadata = {
            "generated_at": datetime.now().isoformat(),
            "model": self.model,
            "temperature": self.temperature,
            "n_scenarios": self.n_scenarios,
            "negative_controls": self.negative_controls,
            "schema_name": self.schema_name,
            "scenario_count": len(scenarios),
            "profile": self.profile,
            "limits": limits,
            "notes": notes,
            "outline_rounds": plan["rounds"],
            "outline_deviations": plan["warnings"],
            "template_attempts": {
                entry["scenario_id"]: result["attempts"] for entry, result in zip(entries, results)
            },
            "failed_scenarios": failed,
            "labels_lost_with_failed_scenarios": lost,
        }
        with open(os.path.join(self.out_folder, "scenarios.meta.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        self.scenarios = scenarios
        return scenarios
