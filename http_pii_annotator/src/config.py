import argparse
import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from src.errors import ConfigError
from src.llm_connectors.registry import APIS, DEFAULT_API
from src.pipeline import LLM_TASKS, MODES
from src.pipeline_steps.preprocessor import NORMALIZATIONS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_DIR = Path(__file__).resolve().parent / "taxonomies"


@dataclass
class PipelineConfig:
    """All settings of a pipeline run, filled from the command line and/or a JSON configuration file."""
    run_id: str = None
    schema: str = None  # name of a bundled taxonomy or path to a Markdown taxonomy file
    model: str = None  # default model for all LLM steps
    models: dict = field(default_factory=dict)  # step-specific models, keyed by task name
    api: str = DEFAULT_API  # API that serves the model(s)
    hostname: str = None
    mode: str = "two-stage"
    input: str = None  # file or folder with HTTP messages (default: <root_dir>/<run_id>/input)
    input_encoding: str = "utf-8"
    normalization: str = "paper"  # "paper": pre-processing as evaluated in the paper; "strict": corrected
    decode_base64: bool = False  # also decode Base64-encoded substrings of the bodies
    examples: str = None  # JSONL file with annotated examples for RAG (default: data/scenarios/<schema>/examples.jsonl)
    root_dir: str = "./output"
    skip_preprocess: bool = False
    skip_classify: bool = False
    skip_annotate: bool = False
    skip_review: bool = False
    skip_evaluate: bool = False
    eval_only: bool = False
    restart: bool = False
    use_rag: bool = True
    max_examples: int = 3
    min_example_similarity: float = 0.3
    min_label_similarity: float = 0.4  # below it, a label that is not in the taxonomy is rejected, not corrected
    max_label_requeries: int = 1  # times the model is asked again after a response with rejected labels
    concurrency: int = 1
    max_retries: int = 3
    request_timeout: float = None  # seconds after which a request to the LLM API is aborted
    keep_model_loaded: bool = False
    verbose: bool = False
    quiet: bool = False

    def __post_init__(self):
        if not self.run_id:
            raise ConfigError("No run id provided (-run-id)")
        if not self.schema:
            raise ConfigError("No label taxonomy provided (-schema)")
        if self.mode not in MODES:
            raise ConfigError(f"Invalid mode '{self.mode}', expected one of {MODES}")
        if self.api not in APIS:
            raise ConfigError(f"Invalid API '{self.api}', expected one of {APIS}")
        if self.normalization not in NORMALIZATIONS:
            raise ConfigError(f"Invalid normalization '{self.normalization}', expected one of {NORMALIZATIONS}")
        if self.concurrency < 1:
            raise ConfigError("The number of concurrent requests (-concurrency) must be at least 1")
        if self.request_timeout is not None and self.request_timeout <= 0:
            raise ConfigError("The request timeout (-request-timeout) must be positive")
        if not -1 <= self.min_label_similarity <= 1:
            raise ConfigError("The minimum label similarity (-min-label-similarity) must be between -1 and 1")
        if self.max_label_requeries < 0:
            raise ConfigError("The number of re-queries (-max-label-requeries) must not be negative")
        if self.max_retries < 0:
            raise ConfigError("The number of retries (-max-retries) must not be negative")
        try:
            "".encode(self.input_encoding)
        except LookupError:
            raise ConfigError(f"Unknown input encoding '{self.input_encoding}'")

        self.models = {task: model for task, model in (self.models or {}).items() if model}
        unknown_tasks = set(self.models) - set(LLM_TASKS)
        if unknown_tasks:
            raise ConfigError(f"Step-specific models given for unknown steps {sorted(unknown_tasks)}, "
                              f"expected a subset of {LLM_TASKS}")

    @property
    def run_folder(self) -> Path:
        return Path(self.root_dir) / self.run_id

    @property
    def schema_path(self) -> Path:
        """Path of the Markdown file containing the label taxonomy."""
        if self.schema.endswith(".md") or "/" in self.schema or "\\" in self.schema:
            return Path(self.schema)
        return TAXONOMY_DIR / f"{self.schema}.md"

    @property
    def schema_name(self) -> str:
        return self.schema_path.stem

    @property
    def input_path(self) -> Path:
        return Path(self.input) if self.input else self.run_folder / "input"

    @property
    def examples_path(self) -> Path:
        """
        Path of the example pool: the given file or 'data/scenarios/<schema>/examples.jsonl' in the current directory or,
        if that does not exist, in the directory that contains the package.
        """
        if self.examples:
            return Path(self.examples)
        relative_path = Path("data") / "scenarios" / self.schema_name / "examples.jsonl"
        if not relative_path.exists() and (PROJECT_ROOT / relative_path).exists():
            return PROJECT_ROOT / relative_path
        return relative_path

    def model_for(self, task: str) -> str | None:
        return self.models.get(task) or self.model


def bundled_taxonomies() -> list[str]:
    return sorted(path.stem for path in TAXONOMY_DIR.glob("*.md"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python http_pii_annotator/main.py",
        description="Taxonomy-agnostic annotation of PII values in HTTP message bodies using LLMs.",
        allow_abbrev=False
    )

    parser.add_argument("--config", "-config", metavar="PATH",
                        help="JSON configuration file; command line arguments override its values")

    general = parser.add_argument_group("run")
    general.add_argument("-run-id", metavar="ID", help="unique identifier of this pipeline run (required)")
    general.add_argument("-schema", metavar="NAME|PATH",
                         help=f"label taxonomy: name of a bundled taxonomy ({', '.join(bundled_taxonomies())}) "
                              "or path to a Markdown taxonomy file (required)")
    general.add_argument("-input", metavar="PATH",
                         help="JSON/JSONL file or folder with the HTTP messages to annotate "
                              "(default: <root-dir>/<run-id>/input)")
    general.add_argument("-input-encoding", metavar="ENCODING",
                         help="character encoding of the input files (default: utf-8)")
    general.add_argument("-root-dir", metavar="PATH", help="folder in which run folders are created (default: ./output)")

    models = parser.add_argument_group("models")
    models.add_argument("-model", metavar="MODEL", help="default model for all LLM steps")
    for task in LLM_TASKS:
        flag = task.replace("_", "-")
        models.add_argument(f"-model-{flag}", metavar="MODEL", help=f"model for the {flag} step")
    models.add_argument("-api", choices=APIS,
                        help="API that serves the model(s) (default: openai, which also works with any "
                             "OpenAI-compatible endpoint given with -hostname)")
    models.add_argument("-hostname", metavar="URL",
                        help="base URL of the LLM API endpoint, e.g. of a llama.cpp or Ollama server")
    models.add_argument("-keep-model-loaded", action="store_true", default=None,
                        help="do not unload locally hosted models (Ollama) at the end of the run")

    pipeline = parser.add_argument_group("pipeline")
    pipeline.add_argument("-mode", choices=MODES,
                          help="two-stage: classify, then annotate the predicted labels (default); "
                               "single-stage: annotate directly; ablation: run both variants")
    pipeline.add_argument("-normalization", choices=NORMALIZATIONS,
                          help="paper: pre-processing as evaluated in the paper (default); strict: decode plus signs "
                               "only in form-encoded bodies and only character references that end with a semicolon")
    pipeline.add_argument("-decode-base64", action="store_true", default=None,
                          help="also decode substrings of the bodies that are Base64-encoded text")
    pipeline.add_argument("-skip-preprocess", action="store_true", default=None, help="skip the pre-processing step")
    pipeline.add_argument("-skip-classify", action="store_true", default=None, help="skip the classification step")
    pipeline.add_argument("-skip-annotate", action="store_true", default=None, help="skip the annotation step(s)")
    pipeline.add_argument("-skip-review", action="store_true", default=None, help="skip the review step(s)")
    pipeline.add_argument("-skip-evaluate", action="store_true", default=None,
                          help="skip the comparison with ground-truth annotations")
    pipeline.add_argument("-eval", dest="eval_only", action="store_true", default=None,
                          help="only evaluate the existing output of a previous run")
    pipeline.add_argument("-restart", action="store_true", default=None,
                          help="discard the existing output of the run instead of resuming it")

    execution = parser.add_argument_group("execution")
    execution.add_argument("-concurrency", type=int, metavar="N",
                           help="number of messages that are processed by an LLM step at the same time (default: 1)")
    execution.add_argument("-max-retries", type=int, metavar="N",
                           help="number of times a failed request to the LLM API is repeated (default: 3)")

    execution.add_argument("-request-timeout", type=float, metavar="SECONDS",
                           help="abort a request to the LLM API after this time (default: wait indefinitely)")

    rag = parser.add_argument_group("retrieval-augmented generation")
    rag.add_argument("-no-rag", dest="use_rag", action="store_false", default=None,
                     help="do not add retrieved examples to the prompts")
    rag.add_argument("-examples", metavar="PATH",
                     help="JSONL file with annotated example messages (default: data/scenarios/<schema>/examples.jsonl)")
    rag.add_argument("-max-examples", type=int, metavar="N",
                     help="number of examples retrieved by payload similarity (default: 3)")
    rag.add_argument("-min-example-similarity", type=float, metavar="FLOAT",
                     help="minimum cosine similarity of examples retrieved by payload similarity (default: 0.3)")

    validation = parser.add_argument_group("output validation")
    validation.add_argument("-min-label-similarity", type=float, metavar="FLOAT",
                            help="cosine similarity that the closest valid label must reach for a label outside "
                                 "the taxonomy to be replaced by it; below it, the label and its annotation are "
                                 "rejected (default: 0.4, -1 never rejects)")
    validation.add_argument("-max-label-requeries", type=int, metavar="N",
                            help="number of times the model is asked again after a response with rejected labels, "
                                 "before these are discarded (default: 1)")

    output = parser.add_argument_group("console output")
    output.add_argument("-verbose", action="store_true", default=None,
                        help="log the LLM responses and the evaluation metrics per label")
    output.add_argument("-quiet", action="store_true", default=None, help="only log warnings and errors")

    return parser


def load_config(argv: list[str] = None) -> PipelineConfig:
    """Builds the pipeline configuration from an optional JSON configuration file and the command line arguments."""
    parser = build_parser()
    args = vars(parser.parse_args(argv))

    settings = {}
    config_path = args.pop("config")
    if config_path:
        with open(config_path, encoding="utf-8") as file:
            settings = json.load(file)
        unknown_keys = set(settings) - {f.name for f in fields(PipelineConfig)}
        if unknown_keys:
            parser.error(f"unknown keys in {config_path}: {sorted(unknown_keys)}")
        # null values in the configuration file stand for the default values
        settings = {key: value for key, value in settings.items() if value is not None}

    step_models = dict(settings.get("models") or {})
    for task in LLM_TASKS:
        step_model = args.pop(f"model_{task}")
        if step_model is not None:
            step_models[task] = step_model
    settings["models"] = step_models

    # command line arguments that were not given are None and do not override the configuration file
    settings.update({key: value for key, value in args.items() if value is not None})

    try:
        return PipelineConfig(**settings)
    except ConfigError as e:
        parser.error(str(e))
