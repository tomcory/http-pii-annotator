import json

from src.pipeline_steps.llm_step import LLMStep
from src.records import Message


class Annotator(LLMStep):
    """Single-stage annotation: jointly detects PII data types and extracts their values from a message body."""
    task = 'annotate'
    details = 'Annotates PII values within HTTP messages'
    response_key = 'annotations'
    message_field = 'annotations'
    raw_file = 'annotated.jsonl'

    def build_user_msgs(self, message: Message) -> list[str]:
        return [json.dumps(message.body)]
