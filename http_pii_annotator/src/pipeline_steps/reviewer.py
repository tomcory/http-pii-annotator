import json

from src.pipeline_steps.llm_step import LLMStep
from src.records import Message


class Reviewer(LLMStep):
    """Post-annotation review: refines the annotations made by the (targeted) annotator."""
    task = 'review'
    details = 'Reviews and corrects annotations made by the Annotator'
    response_key = 'annotations'
    message_field = 'annotations'
    raw_file = 'reviewed.jsonl'

    def build_user_msgs(self, message: Message) -> list[str]:
        # Two user messages: 1) annotator's output to review, 2) HTTP message body
        annotator_output = json.dumps({"annotations": message.annotations or []})
        return [
            "Annotator output to review:\n\n" + annotator_output,
            "HTTP message body:\n\n" + json.dumps(message.body)
        ]

    @staticmethod
    def _assigned_labels(message: Message) -> list[str]:
        """
        The unique labels assigned to a message by the previous steps, i.e. the data types of its annotations
        and, if present, the labels predicted by the classifier.
        """
        labels = set()

        for annotation in message.annotations or []:
            if isinstance(annotation, dict) and 'data_type' in annotation:
                labels.add(annotation['data_type'])

        for label in message.predicted_labels or []:
            if isinstance(label, str):
                labels.add(label)

        return list(labels)

    def retrieve_examples(self, message: Message) -> list[tuple[str, str]]:
        """
        Retrieve examples using both payload similarity and label matching, such that the examples cover all
        labels assigned to the message by the previous steps.
        """
        if not self.context.retriever:
            return []

        return self.context.retriever.get_combined_examples_for_api(
            body=message.body,
            target_labels=self._assigned_labels(message),
            task=self.task,
            max_similar_examples=self.context.max_examples,
            min_similarity=self.context.min_example_similarity
        )
