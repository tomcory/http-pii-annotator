from abc import ABC, abstractmethod

from src.records import Message


class PipelineStep(ABC):
    """
    A step of the pipeline, which processes one message at a time.

    Attributes:
        task: Name of the step's task
        details: Description of the step
        output_files: Names of the JSONL files the step writes to its output folder. A step returns one message
                      per file; the last file holds the result that is passed on to subsequent steps.
    """
    task: str
    details: str
    output_files: tuple[str, ...]

    @property
    def result_file(self) -> str:
        return self.output_files[-1]

    @abstractmethod
    def process(self, message: Message) -> tuple[Message, ...]:
        """Processes a message and returns one message per output file. May be called from several threads."""
        pass
