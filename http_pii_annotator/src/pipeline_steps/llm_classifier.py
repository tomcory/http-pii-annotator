import json

from src.pipeline_steps.llm_step import LLMStep
from src.records import Message


class LLMClassifier(LLMStep):
    """Label-level classification: predicts which PII data types of the taxonomy occur in a message body."""
    task = 'classify'
    details = 'Classifies HTTP messages w.r.t. PII data types they contain'
    response_key = 'data_types'
    message_field = 'predicted_labels'
    raw_file = 'classified.jsonl'

    def build_user_msgs(self, message: Message) -> list[str]:
        return [json.dumps(message.body)]
