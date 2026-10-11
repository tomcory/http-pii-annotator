"""
Pipeline executor for synthetic HTTP request generation.

Orchestrates the pipeline:
  1. Scenario Generation (LLM) - Generate scenario templates from label taxonomy
  2. Scenario Validation - Validate scenarios against taxonomy
  3. Request Generation (LLM) - Generate HTTP requests from scenarios
  4. Request Validation - Validate generated requests
  5. Response Generation (LLM, optional) - Generate HTTP responses for validated requests
  6. Response Validation (optional) - Validate generated responses
  7. Encoding - Apply encodings and compression to the validated plaintext messages
"""

import asyncio
import json
import logging
import os

from src import scenario_plan, util
from src.audit import AnnotationAuditor
from src.blueprints import REASONING_TOKENS, REASONING_TOKENS_OFF, BlueprintSampler
from src.exemplars import ExemplarPool
from src.llm import LLM, create_client
from src.pipeline_steps.encoding_step import EncodingStep
from src.pipeline_steps.pipeline_step import PipelineStep
from src.pipeline_steps.request_generator_step import RequestGeneratorStep
from src.pipeline_steps.request_validator_step import RequestValidatorStep
from src.pipeline_steps.response_generator_step import ResponseGeneratorStep
from src.pipeline_steps.response_validator_step import ResponseValidatorStep
from src.pipeline_steps.scenario_generator_step import ScenarioGeneratorStep
from src.pipeline_steps.scenario_validator_step import ScenarioValidatorStep
from src.state_manager import BaseStateManager, PipelineStatus

logger = logging.getLogger(__name__)


class PipelineExecutor:
    """
    Executes the synthetic data generation pipeline.
    
    Pipeline stages:
    1. scenario_generation - Generate scenario templates from taxonomy (LLM)
    2. scenario_validation - Validate scenarios against taxonomy
    3. request_generation - Generate HTTP requests from scenarios (LLM)
    4. request_validation - Validate generated requests
    5. response_generation - Generate HTTP responses for requests (LLM, optional)
    6. response_validation - Validate generated responses (optional)
    """

    def __init__(
            self,
            run_id: str,
            model: str,
            state_manager: BaseStateManager,
            schema_name: str = "playstore",
            api: str = "openai",
            hostname: str = None,
            root_dir: str = "./output/",
            prompts_dir: str = util.PROMPTS_DIR,
            # Pipeline control
            skip_scenario_generation: bool = False,
            skip_scenario_validation: bool = False,
            skip_request_generation: bool = False,
            skip_request_validation: bool = False,
            skip_response_generation: bool = True,
            skip_response_validation: bool = True,
            # Existing scenarios input (skip generation)
            existing_scenarios_path: str = None,
            # Generation parameters
            n_scenarios: int = 30,
            requests_per_scenario: int = 10,
            temperature: float = 0.8,
            max_tokens: int = 48000,
            timeout: float = 600.0,
            max_retries: int = 2,
            concurrency: int = 1,
            # Realism: exemplar pool and per-message blueprints
            exemplars_dir: str = None,
            batch_size: int = 1,
            max_body_chars: int = 8000,
            seed: int = None,
            thinking: bool = None,
            # Scenario planning
            negative_controls: int = 2,
            scenario_profile: dict = None,
            omit_labels: list = None,
            rebalance_weights: bool = True,
            # Audit of the completeness of the annotations
            skip_audit: bool = False,
            audit_model: str = None,
            audit_rounds: int = 2,
            # Encoding layer
            skip_encoding: bool = False,
            encoding_profile: dict = None,
    ):
        if not run_id:
            raise ValueError("No run_id provided")

        self.run_id = run_id
        self.model = model
        self.state_manager = state_manager
        self.schema_source = schema_name  # name of a bundled taxonomy or path of a Markdown file
        self.schema_name = util.schema_name(schema_name)
        self.api = api
        self.hostname = hostname
        self.prompts_dir = prompts_dir

        # Pipeline control flags
        self.skip_scenario_generation = skip_scenario_generation or (existing_scenarios_path is not None)
        self.skip_scenario_validation = skip_scenario_validation
        self.skip_request_generation = skip_request_generation
        self.skip_request_validation = skip_request_validation
        self.skip_response_generation = skip_response_generation
        self.skip_response_validation = skip_response_validation or skip_response_generation
        self.existing_scenarios_path = existing_scenarios_path

        # Generation parameters
        self.n_scenarios = n_scenarios
        self.requests_per_scenario = requests_per_scenario
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.concurrency = max(1, concurrency)
        self.batch_size = max(1, batch_size)
        self.thinking = thinking
        self.negative_controls = negative_controls
        self.scenario_profile = scenario_profile or scenario_plan.DEFAULT_PROFILE
        self.omit_labels = omit_labels  # None: the omissions of the schema in src/taxonomies/omissions.json
        self.omitted_labels = []
        self.rebalance_weights = rebalance_weights
        self.skip_audit = skip_audit
        self.audit_model = audit_model
        self.audit_rounds = audit_rounds
        self.audit_client: LLM | None = None
        # encoding needs validated messages
        self.skip_encoding = skip_encoding or skip_request_generation or skip_request_validation
        self.encoding_profile = encoding_profile
        # room for reasoning in the token limit of a call; small if reasoning is switched off
        self.reasoning_tokens = REASONING_TOKENS_OFF if thinking is False else REASONING_TOKENS

        # Exemplar pool of real-world messages (optional) and blueprint sampler
        self.pool = ExemplarPool.load(exemplars_dir) if exemplars_dir else ExemplarPool()
        self.sampler = BlueprintSampler(pool=self.pool, seed=seed, max_body_chars=max_body_chars)

        # Create output folder structure
        self.root_dir = root_dir
        self.root_folder = os.path.join(self.root_dir, self.run_id)
        self.input_folder = os.path.join(self.root_folder, "input")
        self.output_folder = os.path.join(self.root_folder, "output")
        self.schema_folder = os.path.join(self.root_folder, "schema")
        self.log_folder = os.path.join(self.root_folder, "log")

        self.schema = ""
        self.labels = []
        self.setup_performed = False
        self.pipeline_length = 0
        self.pipeline_steps = []
        self.client: LLM | None = None

    async def setup(self):
        """Set up the pipeline steps."""
        if self.setup_performed:
            return
        self.setup_performed = True

        await self.state_manager.start()

        # Prepare output folders
        util.prepare_output(self.root_folder, overwrite=False)

        # Load schema/taxonomy
        await self._setup_schema()

        # Set up API client
        await self._setup_client()

        # Set up pipeline steps
        await self._setup_pipeline_steps()

        # Count non-skipped steps
        self.pipeline_length = len([s for s in self.pipeline_steps if not s.skip])

        logger.info("\n[PipelineExecutor] Pipeline setup complete")
        logger.info(f"  Run ID: {self.run_id}")
        logger.info(f"  Model: {self.model}")
        logger.info(f"  Schema: {self.schema_name} ({len(self.labels)} labels)")
        logger.info(f"  Exemplars: {len(self.pool.requests)} requests, {len(self.pool.responses)} responses"
              f" (seed: {self.sampler.seed})")
        logger.info(f"  Steps: {self.pipeline_length} active")

    async def execute(self):
        """Execute the pipeline."""
        await self.setup()

        logger.info("\n" + "=" * 60)
        logger.info("SYNTHETIC DATA GENERATION PIPELINE")
        logger.info("=" * 60)

        for step_index, step in enumerate([s for s in self.pipeline_steps if not s.skip]):
            await self.state_manager.update_state(
                status=PipelineStatus.RUNNING,
                current_step=step.task,
                progress=step_index / self.pipeline_length,
                step_progress=0,
                step_details=step.details
            )

            logger.info(f"\n[{step_index + 1}/{self.pipeline_length}] {step.task}: {step.details}")
            logger.info("-" * 50)

            # Load input messages for this step
            step.start()
            messages = step.prepare_input(await self._load_step_input(step))
            message_count = len(messages)
            completed = 0

            async def process(message):
                nonlocal completed
                try:
                    await step.execute(message)
                except Exception as e:
                    logger.error(f"[ERROR] Processing failed: {e}")
                    if step.abort_on_error:
                        await self.state_manager.raise_error(str(e), abort=True)
                        raise
                    await self.state_manager.raise_error(str(e), abort=False)
                completed += 1
                await self.state_manager.update_state(
                    step_progress=completed / max(1, message_count),
                    current_file=str(completed),
                    total_files=message_count
                )

            if step.is_llm_step:
                # LLM calls run in worker threads; the number of calls in flight is limited by the
                # semaphore of the step, also for the calls a step makes for one message
                step.llm_semaphore = asyncio.Semaphore(self.concurrency)
                await asyncio.gather(*(process(message) for message in messages))
            else:
                for message in messages:
                    await process(message)

            # Call finalize for steps that need post-processing
            if hasattr(step, 'finalize') and callable(step.finalize):
                await step.finalize()

            await self.state_manager.update_state(step_progress=1, current_file="")

        # Close client
        if self.client:
            self.client.close()
        if self.audit_client:
            self.audit_client.close()

        await self.state_manager.update_state(
            status=PipelineStatus.COMPLETED,
            current_step="",
            progress=1,
            step_progress=1,
            message="Pipeline completed successfully"
        )

        logger.info("\n" + "=" * 60)
        logger.info("PIPELINE COMPLETE")
        logger.info("=" * 60)

    async def _setup_schema(self):
        """Load the label taxonomy from src/taxonomies/."""
        schema_path = None

        # Search paths in order of priority
        search_paths = [
            # Primary: a bundled taxonomy (src/taxonomies/<schema>.md) or the given file
            util.schema_path(self.schema_source),
            # Run-specific schema folder
            os.path.join(self.schema_folder, f"{self.schema_name}.md"),
        ]

        for path in search_paths:
            if os.path.exists(path):
                schema_path = path
                break

        if not schema_path:
            available = []
            if os.path.exists(util.TAXONOMY_DIR):
                available = sorted(f.replace(".md", "") for f in os.listdir(util.TAXONOMY_DIR) if f.endswith(".md"))
            raise ValueError(
                f"Could not find schema '{self.schema_name}'. "
                f"Available schemas: {available or 'none found in src/taxonomies/'}"
            )

        self.schema = util.read_from_file(schema_path)

        if not self.schema:
            raise ValueError(f"Could not load schema from {schema_path}")

        # Parse labels from schema
        self.labels = self._extract_labels_from_schema(self.schema)
        logger.info(f"[PipelineExecutor] Loaded {len(self.labels)} labels from {schema_path}")

        # Labels that are never generated (e.g. binary media) are not part of the run at all:
        # they are removed from the label list and from the taxonomy that the LLMs see
        omit = self.omit_labels
        if omit is None:
            omissions_path = os.path.join(os.path.dirname(schema_path), "omissions.json")
            if os.path.exists(omissions_path):
                with open(omissions_path, encoding="utf-8") as f:
                    omit = json.load(f).get(self.schema_name, [])
        unknown = [label for label in omit or [] if label not in self.labels]
        if unknown:
            raise ValueError(f"Labels to omit are not in the schema '{self.schema_name}': {unknown}")
        self.omitted_labels = [label for label in self.labels if label in set(omit or [])]
        if self.omitted_labels:
            self.schema = self._remove_labels_from_schema(self.schema, self.omitted_labels)
            self.labels = [label for label in self.labels if label not in self.omitted_labels]
            logger.info(f"[PipelineExecutor] Omitted {len(self.omitted_labels)} labels: {self.omitted_labels} "
                  f"({len(self.labels)} labels remain)")

    def _remove_labels_from_schema(self, schema_content: str, omitted: list) -> str:
        """Remove the table rows of the given labels from a markdown table schema."""
        kept = []
        for line in schema_content.split('\n'):
            cells = [c.strip() for c in line.split('|')] if '|' in line else []
            if any(cell in omitted for cell in cells) and self._extract_labels_from_schema(
                    self._schema_header(schema_content) + '\n' + line):
                continue
            kept.append(line)
        return '\n'.join(kept)

    @staticmethod
    def _schema_header(schema_content: str) -> str:
        """The header and separator line of a markdown table schema."""
        lines = [line for line in schema_content.strip().split('\n') if '|' in line]
        return '\n'.join(lines[:2])

    def _extract_labels_from_schema(self, schema_content: str) -> list:
        """Extract label names from markdown table schema."""
        labels = []
        lines = schema_content.strip().split('\n')

        # Find the PII Data type column
        pii_column_index = None
        for i, line in enumerate(lines):
            if '|' in line:
                headers = [h.strip().lower() for h in line.split('|')]
                for idx, header in enumerate(headers):
                    if header in ("pii data type", "data type", "data_type"):
                        pii_column_index = idx
                        break
                if pii_column_index is not None:
                    # Skip header and separator, process data rows
                    for data_line in lines[i+2:]:
                        if '|' in data_line and not data_line.strip().startswith('|--'):
                            columns = [c.strip() for c in data_line.split('|')]
                            if len(columns) > pii_column_index:
                                label = columns[pii_column_index].strip()
                                if label and label != "---":
                                    labels.append(label)
                    break

        return labels

    async def _setup_client(self):
        """Set up the LLM API client."""
        if (self.skip_scenario_generation and self.skip_request_generation
                and self.skip_response_generation):
            return  # no LLM step is active

        self.client = create_client(
            api=self.api,
            run_id=self.run_id,
            model=self.model,
            hostname=self.hostname,
            log_dir=self.log_folder,
            thinking=self.thinking,
        )
        logger.info(f"[PipelineExecutor] Initialized {self.api} client (hostname: {self.hostname or 'default'})")

        # a different model for the audit gives an independent second opinion
        if self.audit_model and self.audit_model != self.model and not self.skip_audit:
            self.audit_client = create_client(
                api=self.api,
                run_id=self.run_id,
                model=self.audit_model,
                hostname=self.hostname,
                log_dir=self.log_folder,
                thinking=self.thinking,
            )
            logger.info(f"[PipelineExecutor] Initialized audit client (model: {self.audit_model})")

    def _create_auditor(self, skip: bool) -> AnnotationAuditor | None:
        """Create the auditor of a generation step (each step has its own, for separate statistics)."""
        if skip or self.skip_audit:
            return None
        return AnnotationAuditor(
            schema=self.schema,
            labels=self.labels,
            prompts_dir=self.prompts_dir,
            client=self.audit_client,
            rounds=self.audit_rounds,
            timeout=self.timeout,
            thinking=self.thinking,
        )

    async def _setup_pipeline_steps(self):
        """Set up the pipeline steps."""
        self.pipeline_steps = []

        # Step 1: Scenario Generation
        self.pipeline_steps.append(
            ScenarioGeneratorStep(
                run_id=self.run_id,
                state_manager=self.state_manager,
                in_folder=self.input_folder,
                out_folder=os.path.join(self.output_folder, "1_scenarios"),
                skip=self.skip_scenario_generation,
                schema=self.schema,
                schema_name=self.schema_name,
                labels=self.labels,
                n_scenarios=self.n_scenarios,
                negative_controls=self.negative_controls,
                profile=self.scenario_profile,
                temperature=self.temperature,
                max_tokens=32000,
                timeout=self.timeout,
                max_retries=self.max_retries,
                prompts_dir=self.prompts_dir,
            )
        )
        # Assign client to LLM step
        if not self.skip_scenario_generation:
            self.pipeline_steps[-1].client = self.client
            self.pipeline_steps[-1].model = self.model

        # Step 2: Scenario Validation
        scenario_input_folder = (
            self.existing_scenarios_path if self.existing_scenarios_path
            else os.path.join(self.output_folder, "1_scenarios")
        )
        self.pipeline_steps.append(
            ScenarioValidatorStep(
                run_id=self.run_id,
                state_manager=self.state_manager,
                in_folder=scenario_input_folder,
                out_folder=os.path.join(self.output_folder, "2_scenarios_validated"),
                skip=self.skip_scenario_validation,
                schema=self.schema,
                schema_name=self.schema_name,
                labels=self.labels,
                profile=self.scenario_profile,
                sampler=self.sampler,
                requests_per_scenario=self.requests_per_scenario,
                omitted_labels=self.omitted_labels,
                rebalance=self.rebalance_weights,
            )
        )

        # Scenarios used for message generation: validated ones unless validation is skipped
        if not self.skip_scenario_validation:
            scenarios_folder = os.path.join(self.output_folder, "2_scenarios_validated")
        else:
            scenarios_folder = scenario_input_folder

        # Step 4: Request Validation (created first, as the generator validates inline to regenerate rejects)
        request_validator = RequestValidatorStep(
            run_id=self.run_id,
            state_manager=self.state_manager,
            in_folder=os.path.join(self.output_folder, "3_requests"),
            out_folder=os.path.join(self.output_folder, "4_requests_validated"),
            skip=self.skip_request_validation,
            schema=self.schema,
            schema_name=self.schema_name,
            labels=self.labels,
            pool=self.pool,
        )

        # Step 3: Request Generation
        self.pipeline_steps.append(
            RequestGeneratorStep(
                run_id=self.run_id,
                state_manager=self.state_manager,
                in_folder=scenarios_folder,
                out_folder=os.path.join(self.output_folder, "3_requests"),
                skip=self.skip_request_generation,
                schema=self.schema,
                schema_name=self.schema_name,
                labels=self.labels,
                requests_per_scenario=self.requests_per_scenario,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                max_retries=self.max_retries,
                validator=None if self.skip_request_validation else request_validator,
                sampler=self.sampler,
                reasoning_tokens=self.reasoning_tokens,
                auditor=self._create_auditor(self.skip_request_generation or self.skip_request_validation),
                batch_size=self.batch_size,
                prompts_dir=self.prompts_dir,
            )
        )
        # Assign client to LLM step
        if not self.skip_request_generation:
            self.pipeline_steps[-1].client = self.client
            self.pipeline_steps[-1].model = self.model

        self.pipeline_steps.append(request_validator)

        # Requests used for response generation: validated ones unless validation is skipped
        requests_folder = os.path.join(
            self.output_folder, "3_requests" if self.skip_request_validation else "4_requests_validated"
        )

        # Step 6: Response Validation (created first, as the generator validates inline to regenerate rejects)
        response_validator = ResponseValidatorStep(
            run_id=self.run_id,
            state_manager=self.state_manager,
            in_folder=os.path.join(self.output_folder, "5_responses"),
            out_folder=os.path.join(self.output_folder, "6_responses_validated"),
            skip=self.skip_response_validation,
            schema=self.schema,
            schema_name=self.schema_name,
            labels=self.labels,
            pool=self.pool,
            requests_path=requests_folder,
        )

        # Step 5: Response Generation
        self.pipeline_steps.append(
            ResponseGeneratorStep(
                run_id=self.run_id,
                state_manager=self.state_manager,
                in_folder=requests_folder,
                out_folder=os.path.join(self.output_folder, "5_responses"),
                skip=self.skip_response_generation,
                schema=self.schema,
                schema_name=self.schema_name,
                labels=self.labels,
                scenarios_path=scenarios_folder,
                batch_size=self.batch_size,
                sampler=self.sampler,
                reasoning_tokens=self.reasoning_tokens,
                auditor=self._create_auditor(self.skip_response_generation or self.skip_response_validation),
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                max_retries=self.max_retries,
                validator=None if self.skip_response_validation else response_validator,
                prompts_dir=self.prompts_dir,
            )
        )
        # Assign client to LLM step
        if not self.skip_response_generation:
            self.pipeline_steps[-1].client = self.client
            self.pipeline_steps[-1].model = self.model

        self.pipeline_steps.append(response_validator)

        # Step 7: Encoding (applies encodings and compression to the validated plaintext messages)
        self.pipeline_steps.append(
            EncodingStep(
                run_id=self.run_id,
                state_manager=self.state_manager,
                in_folder=os.path.join(self.output_folder, "4_requests_validated"),
                out_folder=os.path.join(self.output_folder, "7_encoded"),
                skip=self.skip_encoding,
                seed=self.sampler.seed,
                profile=self.encoding_profile,
            )
        )

    async def _load_step_input(self, step: PipelineStep) -> list:
        """Load input messages for a pipeline step."""
        messages = []

        # Special handling for scenario generation - no input needed
        if step.task == "scenario_generation":
            return [{}]  # Single dummy message to trigger generation

        # Load from input folder
        if os.path.isdir(step.in_folder):
            # Priority: Look for specific files
            priority_files = [
                "scenarios_validated.json",
                "scenarios.json",
                "requests_validated.json",
                "requests.jsonl",
                "all_requests.json",
                "responses.jsonl",
                "all_responses.json",
            ]

            for pf in priority_files:
                path = os.path.join(step.in_folder, pf)
                if os.path.exists(path):
                    if pf.endswith(".jsonl"):
                        content = util.load_jsonl(path)
                    else:
                        content = util.load_json(path)
                    if content and isinstance(content, list):
                        messages = content
                        break

            # Fall back to loading all JSON/JSONL files
            if not messages:
                for filename in os.listdir(step.in_folder):
                    if filename.startswith('.'):
                        continue
                    filepath = os.path.join(step.in_folder, filename)
                    if filename.endswith('.jsonl'):
                        content = util.load_jsonl(filepath)
                    elif filename.endswith('.json'):
                        content = util.load_json(filepath)
                    else:
                        continue
                    if content and isinstance(content, list):
                        messages.extend(content)

        # Handle case where in_folder is a file path
        elif os.path.isfile(step.in_folder):
            if step.in_folder.endswith('.jsonl'):
                messages = util.load_jsonl(step.in_folder) or []
            elif step.in_folder.endswith('.json'):
                messages = util.load_json(step.in_folder) or []

        logger.info(f"  Loaded {len(messages)} input items from {step.in_folder}")
        return messages
