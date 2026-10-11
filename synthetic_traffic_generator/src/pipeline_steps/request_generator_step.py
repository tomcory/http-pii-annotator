"""
Pipeline step for generating synthetic HTTP requests from scenario templates.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from src import util
from src.blueprints import BlueprintSampler, output_token_limit, plan_batches
from src.diversity import DiversityTracker
from src.pipeline_steps import message_validation
from src.pipeline_steps.pipeline_step import PipelineStep
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)

NEGATIVE_CONTROL_NOTE = (
    "This scenario is a **negative control**: the requests MUST NOT contain any PII or data matching the taxonomy. "
    "Do not add cookies, authorization headers, session tokens, or user or device identifiers. "
    "`pii_annotations` and `data_types_present` MUST be empty arrays."
)


class RequestGeneratorStep(PipelineStep):
    """
    Pipeline step that generates synthetic HTTP requests from scenario templates.

    Input: Validated scenario templates
    Output: Synthetic HTTP requests with PII annotations

    Every request is generated from a blueprint (target size, real-world exemplar, PII to embed)
    that is sampled in code. Requests are generated in small batches to leave room for long bodies.

    If a validator is provided, generated requests are validated inline and rejected
    requests are regenerated (up to max_retries times per scenario).
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
            requests_per_scenario: int = 10,
            temperature: float = 0.8,
            max_tokens: int = 16000,
            timeout: float = 600.0,
            max_retries: int = 2,
            validator=None,
            sampler: BlueprintSampler = None,
            reasoning_tokens: int = None,
            auditor=None,
            batch_size: int = 5,
            prompts_dir: str = util.PROMPTS_DIR,
    ):
        super().__init__(
            run_id=run_id,
            task="request_generation",
            details="Generate synthetic HTTP requests from scenarios",
            skip=skip,
            is_llm_step=True,
            in_folder=in_folder,
            out_folder=out_folder,
            state_manager=state_manager,
            schema=schema,
            schema_name=schema_name,
            labels=labels
        )

        self.requests_per_scenario = requests_per_scenario
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.validator = validator
        self.sampler = sampler or BlueprintSampler()
        self.tracker = DiversityTracker()
        self.reasoning_tokens = reasoning_tokens
        self.auditor = auditor
        self.batch_size = max(1, batch_size)
        self.prompts_dir = prompts_dir

        # Load prompts
        self.system_prompt = util.read_from_file(
            os.path.join(prompts_dir, "request_generator_system.md")
        )
        self.user_prompt_template = util.read_from_file(
            os.path.join(prompts_dir, "request_generator_user.md")
        )

        # With an exemplar pool, every request carries its own real-world exemplar,
        # which replaces the static examples at the end of the system prompt
        if self.system_prompt and self.sampler.has_request_exemplars:
            self.system_prompt = self.system_prompt.split("### Examples")[0].rstrip() + "\n"

        # Track generated requests
        self.all_requests = []
        self.scenario_stats = []

    def start(self):
        """Reset state and remove outputs of a previous run with the same run ID."""
        self.all_requests = []
        self.scenario_stats = []
        self.tracker = DiversityTracker()
        if os.path.isdir(self.out_folder):
            for filename in os.listdir(self.out_folder):
                # requests.jsonl is appended to; per-scenario files carry the request count in their name
                if filename == "requests.jsonl" or re.fullmatch(r".+-\d+\.json", filename):
                    os.remove(os.path.join(self.out_folder, filename))

    def _render_user_prompt(
            self,
            scenario: dict[str, Any],
            blueprints: list[dict[str, Any]],
            feedback: dict[int, list[str]] = None
    ) -> tuple[str, str, str]:
        """Render prompts for a batch of requests of a single scenario."""
        scenario_id = scenario.get("scenario_id", "unknown")
        n_requests = len(blueprints)
        request_profiles_json = json.dumps([scenario], indent=2)
        scenario_config_json = json.dumps([{"scenario_id": scenario_id, "count": n_requests}], indent=2)
        blueprints_json = json.dumps(
            [self.sampler.describe(blueprint) for blueprint in blueprints], indent=2, ensure_ascii=False
        )

        # System prompt
        system_message = self.system_prompt

        # Taxonomy message
        taxonomy_message = "# PII Data Taxonomy\n\nUse ONLY the following data types (exact string match):\n\n" + self.schema

        # Scenario-specific notes
        notes = []
        if scenario.get("negative_control", False):
            notes.append(NEGATIVE_CONTROL_NOTE)
        errors = [
            f"- blueprint {blueprint['index']}: {error}"
            for blueprint in blueprints for error in (feedback or {}).get(blueprint["index"], [])
        ]
        if errors:
            notes.append(
                "Earlier requests for these blueprints were rejected by the validator. "
                "Generate new requests and avoid these errors:\n" + "\n".join(errors[:12])
            )

        # User prompt
        user_prompt = self.user_prompt_template.replace("{{N_REQUESTS}}", str(n_requests))
        user_prompt = user_prompt.replace("{{PER_SCENARIO}}", str(n_requests))
        user_prompt = user_prompt.replace("{{SCENARIO_ID_LIST_OR_COUNTS_JSON}}", scenario_config_json)
        user_prompt = user_prompt.replace("{{SCENARIO_NOTES}}", "\n\n".join(notes))
        user_prompt = user_prompt.replace("{{REQUEST_PROFILES_JSON}}", request_profiles_json)
        user_prompt = user_prompt.replace("{{BLUEPRINTS_JSON}}", blueprints_json)

        # memory of what was already generated for the scenario (the LLM does not see the other requests)
        digest = self.tracker.digest(scenario_id)
        already_used = ""
        if digest and not scenario.get("negative_control", False):
            already_used = (
                "The following was already used by other requests of this scenario. Do not reuse it:\n\n"
                "<ALREADY_USED_JSON>\n" + json.dumps(digest, indent=2, ensure_ascii=False) + "\n</ALREADY_USED_JSON>"
            )
        user_prompt = user_prompt.replace("{{ALREADY_USED}}", already_used)

        return system_message, taxonomy_message, user_prompt

    async def execute(self, message: dict) -> list[dict[str, Any]]:
        """
        Generate requests for a single scenario.
        
        Args:
            message: Scenario object to generate requests for
            
        Returns:
            List of generated request objects
        """
        scenario_id = message.get("scenario_id", "unknown")

        # The OMISSIONS entry is a bookkeeping entry, not a scenario
        if scenario_id == "OMISSIONS":
            logger.info(f"[RequestGenerator] Skipping {scenario_id} (bookkeeping entry)")
            return []

        negative_control = bool(message.get("negative_control", False))
        target = self.requests_per_scenario
        logger.info(f"\n[RequestGenerator] Generating {target} requests for {scenario_id}"
              f"{' (negative control)' if negative_control else ''}")

        # one blueprint per request; a blueprint stays pending until a valid request exists for it
        pending = {
            index: self.sampler.request_blueprint(message, index) for index in range(1, target + 1)
        }
        accepted = []
        rejected = []
        feedback = {}
        calls = 0
        failed_calls = 0
        last_error = None

        async def generate_batch(batch: list[dict], call: int, attempt: int, next_feedback: dict):
            """Generate, validate and record the requests of one LLM call."""
            nonlocal failed_calls, last_error
            system_prompt, taxonomy_message, user_prompt = self._render_user_prompt(message, batch, feedback)
            try:
                generated = await self.generate_json_array(
                    msg_id=f"{scenario_id}_{call}",
                    system_msg=system_prompt,
                    user_msgs=[user_prompt],
                    schema=taxonomy_message,
                    temperature=self.temperature,
                    max_tokens=output_token_limit(batch, self.max_tokens, self.reasoning_tokens),
                    timeout=self.timeout,
                    max_retries=0,  # failed calls are repeated by the regeneration loop
                )
            except Exception as e:
                # the blueprints of a failed call stay pending and are generated again
                failed_calls += 1
                last_error = str(e)
                logger.info(f"[RequestGenerator] Call {scenario_id}_{call} failed: {e}")
                return

            for position, request in enumerate(generated[:len(batch)]):
                if not isinstance(request, dict):
                    continue

                # requests refer to their blueprint by index; fall back to the position in the array
                blueprint = pending.get(request.get("blueprint")) if isinstance(request.get("blueprint"), int) else None
                if blueprint is None or blueprint not in batch:
                    blueprint = batch[position]
                index = blueprint["index"]
                if index not in pending:
                    continue

                # IDs and scenario metadata are assigned here to guarantee uniqueness and consistency
                request["id"] = f"{scenario_id}_{index:03d}"
                request["scenario_id"] = scenario_id
                request["negative_control"] = negative_control
                request["blueprint"] = blueprint

                message_validation.normalise_annotation_encoding(request, "http_request")
                errors = self.validator.validate(request)[0] if self.validator else []
                if not errors and not negative_control:
                    errors = self.tracker.check(scenario_id, request, f"Request {request['id']}")
                if not errors and self.auditor:
                    # completeness of the annotations; the audit adds what is missing
                    await self.auditor.audit(self, request, "http_request")
                    errors = self.validator.validate(request)[0] if self.validator else []
                    if index not in pending:
                        continue
                if errors:
                    request["id"] = f"{scenario_id}_{index:03d}_r{attempt + 1}"
                    request["rejection_errors"] = errors
                    rejected.append(request)
                    next_feedback.setdefault(index, []).extend(e.split(": ", 1)[-1] for e in errors[:4])
                else:
                    accepted.append(request)
                    self.tracker.add(scenario_id, request)
                    del pending[index]

        for attempt in range(self.max_retries + 1):
            if not pending:
                break
            if attempt > 0:
                logger.info(f"[RequestGenerator] Regenerating {len(pending)} requests for {scenario_id} "
                      f"(attempt {attempt + 1}/{self.max_retries + 1})")

            next_feedback = {}
            batches = plan_batches(list(pending.values()), self.batch_size)
            # the first request of a scenario is generated alone, so that the following ones know what exists
            if attempt == 0 and len(batches) > 1 and self.batch_size == 1:
                calls += 1
                await generate_batch(batches[0], calls, attempt, next_feedback)
                batches = batches[1:]
            tasks = []
            for batch in batches:
                calls += 1
                tasks.append(generate_batch(batch, calls, attempt, next_feedback))
            await asyncio.gather(*tasks)
            feedback = next_feedback

        error = last_error if pending and not accepted else None

        accepted.sort(key=lambda r: r["id"])
        requests = accepted + rejected
        logger.info(f"[RequestGenerator] Generated {len(accepted)}/{target} valid requests for {scenario_id} "
              f"({len(rejected)} rejected, {calls} calls, {failed_calls} failed)")

        # Save requests for this scenario
        if requests:
            self._save_scenario_requests(scenario_id, requests)

        # Track stats
        stats = {
            "scenario_id": scenario_id,
            "negative_control": negative_control,
            "requested": target,
            "generated": len(accepted),
            "rejected": len(rejected),
            "calls": calls,
            "failed_calls": failed_calls,
        }
        if error:
            stats["error"] = error
        self.scenario_stats.append(stats)
        self.all_requests.extend(requests)

        if error:
            logger.info(f"[RequestGenerator] Error generating for {scenario_id}: {error}")
            raise RuntimeError(error)

        return requests

    def _save_scenario_requests(self, scenario_id: str, requests: list[dict[str, Any]]):
        """Save requests for a scenario to file."""
        os.makedirs(self.out_folder, exist_ok=True)

        # Save as individual JSON file
        filename = f"{scenario_id}-{len(requests)}.json"
        output_path = os.path.join(self.out_folder, filename)

        output_data = {
            "scenario_id": scenario_id,
            "request_count": len(requests),
            "generated_at": datetime.now().isoformat(),
            "requests": requests
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)

        # Also append to JSONL
        jsonl_path = os.path.join(self.out_folder, "requests.jsonl")
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for req in requests:
                f.write(json.dumps(req, ensure_ascii=False) + "\n")

    async def finalize(self):
        """Finalize and write generation summary."""
        os.makedirs(self.out_folder, exist_ok=True)

        # Write all requests as single JSON
        all_requests_path = os.path.join(self.out_folder, "all_requests.json")
        with open(all_requests_path, "w", encoding="utf-8") as f:
            json.dump(self.all_requests, f, indent=2, ensure_ascii=False)

        # Write generation summary
        total_requested = sum(s["requested"] for s in self.scenario_stats)
        total_generated = sum(s["generated"] for s in self.scenario_stats)
        total_rejected = sum(s["rejected"] for s in self.scenario_stats)

        summary = {
            "total_scenarios": len(self.scenario_stats),
            "total_requested": total_requested,
            "total_generated": total_generated,
            "total_rejected": total_rejected,
            "generation_rate": total_generated / max(1, total_requested),
            "total_calls": sum(s.get("calls", 0) for s in self.scenario_stats),
            "failed_calls": sum(s.get("failed_calls", 0) for s in self.scenario_stats),
            "batch_size": self.batch_size,
            "audit": self.auditor.summary() if self.auditor else None,
            "seed": self.sampler.seed,
            "per_scenario": self.scenario_stats
        }

        summary_path = os.path.join(self.out_folder, "generation_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        logger.info("\n[RequestGenerator] Generation complete:")
        logger.info(f"  Scenarios processed: {len(self.scenario_stats)}")
        logger.info(f"  Valid requests: {total_generated}/{total_requested} ({total_rejected} rejected)")
