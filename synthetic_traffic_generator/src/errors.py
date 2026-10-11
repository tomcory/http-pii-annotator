class PipelineError(Exception):
    """An error that makes it impossible to continue the pipeline run."""


class ConfigError(PipelineError):
    """An invalid or incomplete configuration of the pipeline run."""
