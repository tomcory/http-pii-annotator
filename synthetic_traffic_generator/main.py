#!/usr/bin/env python3
"""
Synthetic HTTP Message Generation Pipeline

Generates synthetic HTTP messages with PII annotations through a pipeline:
1. Generate scenario templates from a PII label taxonomy
2. Validate scenarios against taxonomy
3. Generate HTTP requests from scenarios
4. Validate generated requests
5. Generate HTTP responses for the validated requests (optional)
6. Validate generated responses (optional)
7. Apply encodings and compression to the validated messages

Usage:
    # Full pipeline
    python synthetic_traffic_generator/main.py -run-id my_run -model gpt-4 -schema playstore

    # Full pipeline including responses
    python synthetic_traffic_generator/main.py -run-id my_run -model gpt-4 -schema playstore -responses

    # Use existing scenarios
    python synthetic_traffic_generator/main.py -run-id my_run -model gpt-4 -scenarios scenarios.json

    # Generate scenarios only
    python synthetic_traffic_generator/main.py -run-id my_run -model gpt-4 -scenarios-only
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from dotenv import find_dotenv, load_dotenv

from src import scenario_plan, util
from src.encoding import load_profile
from src.executor import PipelineExecutor
from src.exemplars import ExemplarPool
from src.llm import SUPPORTED_APIS
from src.state_manager import ConsoleStateManager

logger = logging.getLogger("src")


EXAMPLES = """
examples:
  # Full pipeline with the Google Play taxonomy
  python synthetic_traffic_generator/main.py -model gpt-4 -schema playstore -n-scenarios 20 -requests-per-scenario 5

  # AI4Privacy taxonomy, also generating responses
  python synthetic_traffic_generator/main.py -model gpt-4 -schema ai4privacy -n-scenarios 30 -responses

  # Local llama.cpp server
  python synthetic_traffic_generator/main.py -model my-model -hostname http://localhost:8080/v1

  # Ollama server (set OLLAMA_HOSTNAME, and OLLAMA_API_KEY if it requires authentication)
  python synthetic_traffic_generator/main.py -api ollama -model qwen3:235b -schema playstore

  # Anthropic API (requires ANTHROPIC_API_KEY)
  python synthetic_traffic_generator/main.py -api anthropic -model claude-sonnet-5-5 -schema mhealth

  # Scenarios only
  python synthetic_traffic_generator/main.py -model gpt-4 -scenarios-only -n-scenarios 50

  # Requests and responses from existing scenarios
  python synthetic_traffic_generator/main.py -model gpt-4 -scenarios scenarios.json -messages-only -requests-per-scenario 10
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python synthetic_traffic_generator/main.py",
        description="Generates synthetic HTTP messages with ground-truth PII annotations for a label taxonomy.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )

    general = parser.add_argument_group("run")
    general.add_argument("-run-id", metavar="ID", help="unique identifier of this run (default: synth_<timestamp>)")
    general.add_argument("-schema", metavar="NAME|PATH", default="playstore",
                         help=f"label taxonomy: one of {util.bundled_taxonomies()} or the path of a Markdown file "
                              f"(default: playstore)")
    general.add_argument("-root-dir", metavar="PATH", default="./output/",
                         help="folder in which run folders are created (default: ./output)")
    general.add_argument("-prompts-dir", metavar="PATH", default=util.PROMPTS_DIR,
                         help="folder with the prompts of the steps (default: src/prompts)")

    models = parser.add_argument_group("models")
    models.add_argument("-model", metavar="MODEL", required=True, help="model for all LLM steps (required)")
    models.add_argument("-api", choices=SUPPORTED_APIS, default="openai",
                        help="API of the model (default: openai, which works with any OpenAI-compatible server "
                             "such as llama.cpp or vLLM)")
    models.add_argument("-hostname", metavar="URL",
                        help="base URL of a custom API endpoint (default: OPENAI_API_BASE, or OLLAMA_HOSTNAME for "
                             "-api ollama)")
    thinking = models.add_mutually_exclusive_group()
    thinking.add_argument("-thinking", dest="thinking", action="store_true", default=None,
                          help="switch the reasoning of thinking-capable models on (only -api ollama)")
    thinking.add_argument("-no-thinking", dest="thinking", action="store_false", default=None,
                          help="switch the reasoning of thinking-capable models off (only -api ollama)")

    scenarios = parser.add_argument_group("scenarios")
    scenarios.add_argument("-scenarios", dest="existing_scenarios", metavar="PATH",
                           help="use an existing scenario set instead of generating one")
    scenarios.add_argument("-n-scenarios", type=int, metavar="N", default=30,
                           help="number of scenarios with PII to generate (default: 30)")
    scenarios.add_argument("-negative-controls", type=int, metavar="N", default=2,
                           help="number of additional scenarios without any PII (default: 2)")
    scenarios.add_argument("-scenario-profile", metavar="PATH",
                           help="JSON file with the requirements for the scenario set: mix of scenario types, "
                                "scenarios per label, labels per scenario (default: DEFAULT_PROFILE in "
                                "src/scenario_plan.py)")
    scenarios.add_argument("-omit-labels", metavar="LABELS",
                           type=lambda value: [label.strip() for label in value.split(",") if label.strip()],
                           help="comma-separated labels that are never generated and are removed from the taxonomy "
                                "(default: the labels of the schema in src/taxonomies/omissions.json)")
    scenarios.add_argument("-no-rebalance", action="store_true",
                           help="do not raise the candidate weights of labels that the coverage forecast shows as "
                                "rarely planned")
    scenarios.add_argument("-scenarios-only", action="store_true",
                           help="generate scenarios only, skip message generation")

    messages = parser.add_argument_group("messages")
    messages.add_argument("-requests-per-scenario", type=int, metavar="N", default=10,
                          help="requests per scenario (default: 10)")
    messages.add_argument("-responses", action="store_true",
                          help="also generate an HTTP response for each validated request")
    messages.add_argument("-requests-only", action="store_true", help="generate requests only (requires -scenarios)")
    messages.add_argument("-messages-only", action="store_true",
                          help="generate requests and responses only (requires -scenarios)")
    messages.add_argument("-exemplars", metavar="PATH",
                          help="exemplar pool of real-world messages used as structural templates (default: "
                               "real_data/ if it contains a pool); build one from within "
                               "synthetic_traffic_generator/ with: python -m src.exemplars build <traffic.db>")
    messages.add_argument("-no-exemplars", action="store_true",
                          help="do not use an exemplar pool (built-in size targets and filler profiles)")
    messages.add_argument("-batch-size", type=int, metavar="N", default=1,
                          help="maximum number of messages per LLM call (default: 1)")
    messages.add_argument("-max-body-chars", type=int, metavar="N", default=8000,
                          help="upper bound for the target body length of a message (default: 8000)")
    messages.add_argument("-seed", type=int, metavar="N", help="seed for sampling message blueprints (default: random)")

    audit = parser.add_argument_group(
        "annotation audit",
        "Every valid message is audited for PII values that match the taxonomy but are not annotated. Existing runs "
        "can be audited from within synthetic_traffic_generator/ with: python -m src.audit <run folder> -schema <name>")
    audit.add_argument("-no-audit", action="store_true", help="skip the audit")
    audit.add_argument("-audit-model", metavar="MODEL", help="model for the audit (default: the generation model)")
    audit.add_argument("-audit-rounds", type=int, metavar="N", default=2,
                       help="maximum audit rounds per message (default: 2)")

    encoding = parser.add_argument_group(
        "encoding",
        "A final step applies encodings and compression to values and bodies and writes the result to 7_encoded/. "
        "Existing runs can be (re-)encoded from within synthetic_traffic_generator/ with: "
        "python -m src.encoding <run folder>")
    encoding.add_argument("-no-encoding", action="store_true", help="skip the encoding step")
    encoding.add_argument("-encoding-profile", metavar="PATH",
                          help="JSON file with the shares of the encodings (default: DEFAULT_PROFILE in "
                               "src/encoding.py)")

    execution = parser.add_argument_group("execution")
    execution.add_argument("-temperature", type=float, metavar="FLOAT", default=0.8,
                           help="sampling temperature (default: 0.8)")
    execution.add_argument("-request-timeout", "-timeout", dest="timeout", type=float, metavar="SECONDS",
                           default=600.0, help="seconds after which a request to the API is aborted (default: 600)")
    execution.add_argument("-max-retries", type=int, metavar="N", default=2,
                           help="regeneration attempts for failed or rejected outputs (default: 2)")
    execution.add_argument("-concurrency", type=int, metavar="N", default=1,
                           help="number of parallel LLM calls (default: 1)")
    execution.add_argument("-skip-validation", action="store_true",
                           help="skip all validation steps (also disables regeneration of rejected outputs)")

    output = parser.add_argument_group("console output")
    output.add_argument("-verbose", action="store_true", help="also log the set-up of the pipeline steps")
    output.add_argument("-quiet", action="store_true", help="only log warnings and errors")
    return parser


def parse_args(argv: list[str]) -> dict:
    """Parse the command line arguments into the configuration of a run."""
    parser = build_parser()
    config = vars(parser.parse_args(argv))

    if not config["run_id"]:
        config["run_id"] = f"synth_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not config["hostname"]:
        config["hostname"] = util.default_hostname(config["api"])
    if config.pop("no_exemplars"):
        config["exemplars"] = None
    elif not config["exemplars"]:
        config["exemplars"] = "real_data" if ExemplarPool.exists("real_data") else None
    config["responses"] = config["responses"] or config["messages_only"]
    return config


async def run_pipeline(config: dict):
    """Run the pipeline with the given configuration."""

    # Create state manager
    state_manager = ConsoleStateManager()

    # Determine skip flags based on mode
    skip_scenario_gen = (
        config["existing_scenarios"] is not None or config["requests_only"] or config["messages_only"]
    )
    skip_request_gen = config["scenarios_only"]
    skip_scenario_val = config["skip_validation"]
    skip_request_val = config["skip_validation"] or config["scenarios_only"]
    skip_response_gen = not config["responses"] or config["scenarios_only"] or config["requests_only"]
    skip_response_val = config["skip_validation"] or skip_response_gen

    # Validate configuration
    for flag in ("requests_only", "messages_only"):
        if config[flag] and not config["existing_scenarios"]:
            logger.error(f"Error: -{flag.replace('_', '-')} requires -scenarios to specify existing scenarios")
            return 1
    if config["exemplars"] and not ExemplarPool.exists(config["exemplars"]):
        logger.error(f"Error: no exemplar pool found in '{config['exemplars']}'. "
              f"Build one with: python -m src.exemplars build <traffic.db> -out {config['exemplars']}")
        return 1
    if config["api"] not in SUPPORTED_APIS:
        logger.error(f"Error: unknown -api '{config['api']}'. Supported: {', '.join(SUPPORTED_APIS)}")
        return 1

    # Create executor
    executor = PipelineExecutor(
        run_id=config["run_id"],
        model=config["model"],
        state_manager=state_manager,
        schema_name=config["schema"],
        api=config["api"],
        hostname=config["hostname"],
        root_dir=config["root_dir"],
        prompts_dir=config["prompts_dir"],
        skip_scenario_generation=skip_scenario_gen,
        skip_scenario_validation=skip_scenario_val,
        skip_request_generation=skip_request_gen,
        skip_request_validation=skip_request_val,
        skip_response_generation=skip_response_gen,
        skip_response_validation=skip_response_val,
        existing_scenarios_path=config["existing_scenarios"],
        n_scenarios=config["n_scenarios"],
        requests_per_scenario=config["requests_per_scenario"],
        temperature=config["temperature"],
        timeout=config["timeout"],
        max_retries=config["max_retries"],
        concurrency=config["concurrency"],
        exemplars_dir=config["exemplars"],
        batch_size=config["batch_size"],
        max_body_chars=config["max_body_chars"],
        seed=config["seed"],
        thinking=config["thinking"],
        negative_controls=config["negative_controls"],
        scenario_profile=scenario_plan.load_profile(config["scenario_profile"]),
        omit_labels=config["omit_labels"],
        rebalance_weights=not config["no_rebalance"],
        skip_audit=config["no_audit"] or config["skip_validation"],
        audit_model=config["audit_model"],
        audit_rounds=config["audit_rounds"],
        skip_encoding=config["no_encoding"] or config["skip_validation"] or config["scenarios_only"],
        encoding_profile=load_profile(config["encoding_profile"]),
    )

    # Execute pipeline
    await executor.execute()

    return 0


def main():
    """Main entry point."""

    # load environment variables (API keys) from the .env file in the current directory or one of its parents
    load_dotenv(find_dotenv(usecwd=True))

    # show the help message if no arguments are given
    config = parse_args(sys.argv[1:] or ["-h"])
    util.setup_logging(config["verbose"], config["quiet"])

    # Print configuration
    logger.info("\n" + "=" * 60)
    logger.info("SYNTHETIC DATA GENERATION PIPELINE")
    logger.info("=" * 60)
    logger.info(f"  Run ID: {config['run_id']}")
    logger.info(f"  Model: {config['model']}")
    logger.info(f"  Schema: {config['schema']}")
    logger.info(f"  API: {config['api']}")
    logger.info(f"  Hostname: {config['hostname'] or 'default'}")
    logger.info(f"  Output: {config['root_dir']}")
    if config['existing_scenarios']:
        logger.info(f"  Existing scenarios: {config['existing_scenarios']}")
    else:
        logger.info(f"  Scenarios to generate: {config['n_scenarios']}")
    if not config['scenarios_only']:
        logger.info(f"  Requests per scenario: {config['requests_per_scenario']}")
        logger.info(f"  Exemplars: {config['exemplars'] or 'none (built-in profiles)'}")
        logger.info(f"  Responses: {'yes' if config['responses'] and not config['requests_only'] else 'no'}")
    logger.info("=" * 60 + "\n")

    # Run pipeline
    try:
        return asyncio.run(run_pipeline(config))
    except KeyboardInterrupt:
        logger.info("\n[Interrupted by user]")
        return 130
    except Exception as e:
        logger.exception(f"\n[ERROR] Pipeline failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
