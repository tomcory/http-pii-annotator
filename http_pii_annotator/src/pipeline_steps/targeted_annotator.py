import json

from src.pipeline_steps.llm_step import LLMStep
from src.records import Message


class TargetedAnnotator(LLMStep):
    """Two-stage annotation: extracts the values of the PII data types predicted by the upstream classifier."""
    task = 'targeted_annotate'
    details = 'Annotates PII values within pre-classified HTTP messages'
    response_key = 'annotations'
    message_field = 'annotations'
    raw_file = 'annotated.jsonl'

    def build_user_msgs(self, message: Message) -> list[str]:
        # Two user messages: 1) predicted labels, 2) HTTP message body
        return [
            "Predicted labels:\n\n" + json.dumps(message.predicted_labels or []),
            json.dumps(message.body)
        ]

    def retrieve_examples(self, message: Message) -> list[tuple[str, str]]:
        """
        Retrieve examples using both payload similarity and label matching, such that the examples cover all
        labels predicted by the upstream classifier.
        """
        if not self.context.retriever:
            return []

        return self.context.retriever.get_combined_examples_for_api(
            body=message.body,
            target_labels=message.predicted_labels or [],
            task=self.task,
            max_similar_examples=self.context.max_examples,
            max_label_examples=self.context.max_examples,
            min_similarity=self.context.min_example_similarity
        )
