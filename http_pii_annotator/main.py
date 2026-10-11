"""
Command line interface of the PII annotation pipeline.

Annotates PII values in the bodies of HTTP messages using a label taxonomy that is provided at runtime.
"""
import logging
import sys

from dotenv import find_dotenv, load_dotenv
from tqdm.contrib.logging import logging_redirect_tqdm

from src.config import load_config
from src.errors import PipelineError
from src.executor import PipelineExecutor
from src.progress import ConsoleProgress, Progress

# the modules of the pipeline log under the name of their package
logger = logging.getLogger("src")


def setup_logging(verbose: bool = False, quiet: bool = False):
    """Logs the messages of the package to the console; other libraries only log warnings."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logger.setLevel(logging.WARNING if quiet else logging.DEBUG if verbose else logging.INFO)


def main(argv: list[str] = None) -> int:
    # load environment variables (API keys) from the .env file in the current directory or one of its parents
    load_dotenv(find_dotenv(usecwd=True))

    # show the help message if no arguments are given
    config = load_config(argv if argv is not None else (sys.argv[1:] or ["-h"]))
    setup_logging(config.verbose, config.quiet)

    executor = PipelineExecutor(config, Progress() if config.quiet else ConsoleProgress())
    try:
        with logging_redirect_tqdm():
            summary = executor.execute()
    except PipelineError as e:
        logger.error("Pipeline aborted: %s", e)
        return 1
    except KeyboardInterrupt:
        logger.error("Pipeline interrupted. Run the same command again to resume.")
        return 130

    if summary.failed_messages:
        logger.warning("Pipeline completed, but %d messages could not be processed. They are marked with an "
                       "'error' field in the output; run the same command again to retry them.",
                       summary.failed_messages)
    else:
        logger.info("Pipeline completed successfully")
    return 0


if __name__ == '__main__':
    sys.exit(main())
