"""
Pipeline step for generating synthetic HTTP responses for previously generated requests.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from src import util
from src.blueprints import BlueprintSampler, output_token_limit, plan_batches
from src.diversity import DiversityTracker
from src.pipeline_steps import message_validation
from src.pipeline_steps.pipeline_step import PipelineStep
from src.pipeline_steps.request_generator_step import NEGATIVE_CONTROL_NOTE
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)


class ResponseGeneratorStep(PipelineStep):
    """
    Pipeline step that generates a synthetic HTTP response for each generated request.

    Input: Synthetic HTTP requests with PII annotations (grouped by scenario into batches)
    Output: Synthetic HTTP responses with PII annotations

    If a validator is provided, generated responses are validated inline and rejected
    or missing responses are regenerated (up to max_retries times per batch).
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
            scenarios_path: str = None,
            batch_size: int = 10,
            temperature: float = 0.8,
            max_tokens: int = 16000,
            timeout: float = 600.0,
            max_retries: int = 2,
            validator=None,
            sampler: BlueprintSampler = None,
            reasoning_tokens: int = None,
            auditor=None,
            prompts_dir: str = util.PROMPTS_DIR,
    ):
        super().__init__(
            run_id=run_id,
            task="response_generation",
            details="Generate synthetic HTTP responses for requests",
            skip=skip,
            is_llm_step=True,
            in_folder=in_folder,
            out_folder=out_folder,
            state_manager=state_manager,
            schema=schema,
            schema_name=schema_name,
            labels=labels
        )

        self.scenarios_path = scenarios_path
        self.batch_size = max(1, batch_size)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.validator = validator
        self.sampler = sampler or BlueprintSampler()
        self.tracker = DiversityTracker()
        self.reasoning_tokens = reasoning_tokens
        self.auditor = auditor
        self.prompts_dir = prompts_dir

        # Load prompts (only needed if the step is active)
        self.system_prompt = None
        self.user_prompt_template = None
        if not skip:
            self.system_prompt = util.read_from_file(
                os.path.join(prompts_dir, "response_generator_system.md")
            )
            self.user_prompt_template = util.read_from_file(
                os.path.join(prompts_dir, "response_generator_user.md")
            )

        self.scenarios = {}
        self.all_responses = []
        self.batch_stats = []

    def start(self):
        """Reset state, remove outputs of a previous run and load the scenario templates."""
        self.all_responses = []
        self.batch_stats = []
        self.tracker = DiversityTracker()

        jsonl_path = os.path.join(self.out_folder, "responses.jsonl")
        if os.path.exists(jsonl_path):
            os.remove(jsonl_path)

        self.scenarios = {}
        for scenario in self._load_scenarios():
            if isinstance(scenario, dict) and "scenario_id" in scenario:
                self.scenarios[scenario["scenario_id"]] = scenario

    def _load_scenarios(self) -> list:
        """Load the scenario templates the requests were generated from."""
        path = self.scenarios_path
        if not path:
            return []
        if os.path.isdir(path):
            for filename in ("scenarios_validated.json", "scenarios.json"):
                candidate = os.path.join(path, filename)
                if os.path.exists(candidate):
                    path = candidate
                    break
            else:
                return []
        scenarios = util.load_json(path)
        return scenarios if isinstance(scenarios, list) else []

    def prepare_input(self, messages: list) -> list:
        """Group the requests by scenario and split them into batches."""
        groups = {}
        for request in messages:
            if not isinstance(request, dict) or not isinstance(request.get("id"), str):
                continue
            key = request.get("scenario_id") or request.get("scenario") or "unknown"
            groups.setdefault(key, []).append(request)
            # the validator checks each response against its request
            if self.validator is not None:
                self.validator.requests[request["id"]] = request

        # every request gets a response blueprint; batches are sized to leave room for long bodies
        batches = []
        for key, requests in groups.items():
            blueprints = {
                request["id"]: self.sampler.response_blueprint(request, self.scenarios.get(key)) for request in requests
            }
            planned = plan_batches(
                [{"request": request, **blueprints[request["id"]]} for request in requests], self.batch_size
            )
            for batch_index, batch in enumerate(planned, start=1):
                batches.append({
                    "scenario_id": key,
                    "batch_index": batch_index,
                    "requests": [item["request"] for item in batch],
                    "blueprints": {item["request"]["id"]: blueprints[item["request"]["id"]] for item in batch},
                })
        return batches

    def _render_user_prompt(
            self,
            scenario: dict,
            requests: list[dict[str, Any]],
            blueprints: dict[str, dict],
            negative_control: bool,
            feedback: list[str] = None
    ) -> tuple[str, str]:
        """Render the taxonomy message and the user prompt for a batch of requests."""
        taxonomy_message = "# PII Data Taxonomy\n\nUse ONLY the following data types (exact string match):\n\n" + self.schema

        notes = []
        if negative_control:
            notes.append(NEGATIVE_CONTROL_NOTE.replace("requests", "responses"))
        if feedback:
            notes.append(
                "Earlier responses for these requests were missing or rejected by the validator. "
                "Avoid these errors:\n" + "\n".join(f"- {error}" for error in feedback)
            )

        requests_json = json.dumps([
            {
                "id": request["id"],
                "scenario": request.get("scenario", ""),
                "http_request": request.get("http_request", {}),
                "pii_annotations": request.get("pii_annotations", []),
                "response_blueprint": self.sampler.describe(blueprints[request["id"]], kind="response"),
            }
            for request in requests
        ], indent=2, ensure_ascii=False)

        user_prompt = self.user_prompt_template.replace("{{N_RESPONSES}}", str(len(requests)))
        user_prompt = user_prompt.replace("{{SCENARIO_NOTES}}", "\n\n".join(notes))
        user_prompt = user_prompt.replace(
            "{{SCENARIO_TEMPLATE_JSON}}",
            json.dumps(scenario, indent=2, ensure_ascii=False) if scenario else "(not available)"
        )
        user_prompt = user_prompt.replace("{{REQUESTS_JSON}}", requests_json)

        # memory of what was already generated for the scenario (the LLM does not see the other responses)
        scenario_id = requests[0].get("scenario_id") if requests else None
        layouts = self.tracker.digest(scenario_id).get("body_layouts_already_used") if scenario_id else None
        already_used = ""
        if layouts and not negative_control:
            already_used = (
                "Other responses of this scenario already use the following body layouts (top-level keys). "
                "Prefer a different layout:\n" + "\n".join(f"- {layout}" for layout in layouts)
            )
        user_prompt = user_prompt.replace("{{ALREADY_USED}}", already_used)

        return taxonomy_message, user_prompt

    async def execute(self, message: dict) -> list[dict[str, Any]]:
        """
        Generate responses for a batch of requests of one scenario.

        Args:
            message: Batch object with 'scenario_id', 'batch_index' and 'requests'

        Returns:
            List of generated response objects
        """
        if not self.system_prompt or not self.user_prompt_template:
            raise ValueError("Response generator prompts not loaded")

        scenario_id = message["scenario_id"]
        batch_id = f"{scenario_id}_b{message['batch_index']}"
        requests = message["requests"]
        blueprints = message.get("blueprints") or {
            request["id"]: self.sampler.response_blueprint(request, self.scenarios.get(scenario_id))
            for request in requests
        }
        scenario = self.scenarios.get(scenario_id)
        negative_control = any(r.get("negative_control", False) for r in requests) or bool(
            scenario and scenario.get("negative_control", False)
        )

        logger.info(f"\n[ResponseGenerator] Generating {len(requests)} responses for {batch_id}")

        pending = {request["id"]: request for request in requests}
        accepted = []
        rejected = {}
        feedback = []
        error = None
        failed_calls = 0

        for attempt in range(self.max_retries + 1):
            if not pending:
                break
            if attempt > 0:
                logger.info(f"[ResponseGenerator] Regenerating {len(pending)} responses for {batch_id} "
                      f"(attempt {attempt + 1}/{self.max_retries + 1})")

            taxonomy_message, user_prompt = self._render_user_prompt(
                scenario, list(pending.values()), blueprints, negative_control, feedback
            )

            try:
                batch = await self.generate_json_array(
                    msg_id=f"{batch_id}_{attempt + 1}",
                    system_msg=self.system_prompt,
                    user_msgs=[user_prompt],
                    schema=taxonomy_message,
                    temperature=self.temperature,
                    max_tokens=output_token_limit(
                        [blueprints[request_id] for request_id in pending], self.max_tokens,
                        self.reasoning_tokens
                    ),
                    timeout=self.timeout,
                    max_retries=0,  # failed calls are repeated by the regeneration loop
                )
            except Exception as e:
                # the requests of a failed call stay pending and are answered again
                failed_calls += 1
                error = str(e)
                logger.info(f"[ResponseGenerator] Call {batch_id}_{attempt + 1} failed: {e}")
                continue

            feedback = []
            for response in batch:
                if not isinstance(response, dict):
                    feedback.append("Every array element must be a JSON object")
                    continue

                request = pending.get(response.get("id"))
                if request is None:
                    feedback.append(f"Response id '{response.get('id')}' does not match a request id")
                    continue

                # scenario metadata is taken from the request to guarantee consistency
                response["scenario"] = request.get("scenario", response.get("scenario", ""))
                response["scenario_id"] = request.get("scenario_id", scenario_id)
                response["negative_control"] = bool(request.get("negative_control", negative_control))
                response["blueprint"] = blueprints[request["id"]]

                message_validation.normalise_annotation_encoding(response, "http_response")
                errors = self.validator.validate(response)[0] if self.validator else []
                if not errors and not response["negative_control"]:
                    own_values = [a.get("value") for a in request.get("pii_annotations", [])]
                    errors = self.tracker.check(
                        response["scenario_id"], response, f"Response {response.get('id')}", own_values
                    )
                if not errors and self.auditor:
                    # completeness of the annotations; the audit adds what is missing
                    await self.auditor.audit(self, response, "http_response")
                    errors = self.validator.validate(response)[0] if self.validator else []
                if errors:
                    response["rejection_errors"] = errors
                    rejected[request["id"]] = response
                    feedback.extend(errors)
                else:
                    accepted.append(response)
                    self.tracker.add(response["scenario_id"], response)
                    rejected.pop(request["id"], None)
                    del pending[request["id"]]

            if pending and not feedback:
                feedback.append("Some requests did not receive a response")
            feedback = feedback[:10]

        # keep the last rejected attempt of each request so that the validation step can report it
        responses = accepted + list(rejected.values())
        missing = [request_id for request_id in pending if request_id not in rejected]
        logger.info(f"[ResponseGenerator] Generated {len(accepted)}/{len(requests)} valid responses for {batch_id} "
              f"({len(rejected)} rejected, {len(missing)} missing)")

        if responses:
            self._save_responses(responses)

        stats = {
            "scenario_id": scenario_id,
            "batch_index": message["batch_index"],
            "requested": len(requests),
            "generated": len(accepted),
            "rejected": len(rejected),
            "missing": missing,
            "failed_calls": failed_calls,
        }
        # a failed call is only an error if requests remain without response
        if not pending:
            error = None
        if error:
            stats["error"] = error
        self.batch_stats.append(stats)
        self.all_responses.extend(responses)

        if error:
            logger.info(f"[ResponseGenerator] Error generating for {batch_id}: {error}")
            raise RuntimeError(error)

        return responses

    def _save_responses(self, responses: list[dict[str, Any]]):
        """Append responses to the JSONL output."""
        os.makedirs(self.out_folder, exist_ok=True)
        jsonl_path = os.path.join(self.out_folder, "responses.jsonl")
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for res in responses:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")

    async def finalize(self):
        """Finalize and write generation summary."""
        os.makedirs(self.out_folder, exist_ok=True)

        # Write all responses as single JSON
        all_responses_path = os.path.join(self.out_folder, "all_responses.json")
        with open(all_responses_path, "w", encoding="utf-8") as f:
            json.dump(self.all_responses, f, indent=2, ensure_ascii=False)

        total_requested = sum(s["requested"] for s in self.batch_stats)
        total_generated = sum(s["generated"] for s in self.batch_stats)
        total_rejected = sum(s["rejected"] for s in self.batch_stats)

        summary = {
            "generated_at": datetime.now().isoformat(),
            "total_batches": len(self.batch_stats),
            "total_requested": total_requested,
            "total_generated": total_generated,
            "total_rejected": total_rejected,
            "generation_rate": total_generated / max(1, total_requested),
            "failed_calls": sum(s.get("failed_calls", 0) for s in self.batch_stats),
            "batch_size": self.batch_size,
            "audit": self.auditor.summary() if self.auditor else None,
            "per_batch": self.batch_stats
        }

        summary_path = os.path.join(self.out_folder, "generation_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        logger.info("\n[ResponseGenerator] Generation complete:")
        logger.info(f"  Batches processed: {len(self.batch_stats)}")
        logger.info(f"  Valid responses: {total_generated}/{total_requested} ({total_rejected} rejected)")
