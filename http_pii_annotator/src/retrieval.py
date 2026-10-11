"""
Retrieval of annotated example messages for few-shot prompting (RAG).

Examples are selected by the similarity of their payloads (body field) to a given message and such that
they cover a given set of labels.
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from src.embedding import EmbeddingModel

logger = logging.getLogger(__name__)


@dataclass
class AnnotatedExample:
    """Represents an annotated HTTP request example."""
    id: str
    scenario: str
    body: str
    http_request: dict[str, Any]
    annotations: list[dict[str, str]]
    data_types_present: list[str] = field(default_factory=list)
    similarity_score: float = 0.0


class ExampleRetriever:
    """
    Loads annotated HTTP request examples from a JSONL file and retrieves examples based on payload (body)
    similarity, using sentence embeddings, and based on label coverage.
    """

    def __init__(self, examples_file: Path, embedding_model: EmbeddingModel):
        """
        Args:
            examples_file: JSONL file containing one annotated HTTP request per line.
            embedding_model: Model used to embed the bodies of the examples and of the processed messages.
        """
        self.examples_file = Path(examples_file)
        self.embedding_model = embedding_model

        self.examples: list[AnnotatedExample] = []
        self.embeddings: np.ndarray | None = None

        self._load_examples()
        if self.examples:
            # Normalized embeddings, such that dot products are cosine similarities
            self.embeddings = self.embedding_model.encode_normalized([example.body for example in self.examples])

    def _load_examples(self):
        """Load annotated examples from the JSONL file."""
        if not self.examples_file.exists():
            logger.warning("Examples file not found: %s", self.examples_file)
            return

        with open(self.examples_file, encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    self._add_example_from_request(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse line %d of %s: %s", line_num, self.examples_file, e)

        logger.info("Loaded %d examples from %s", len(self.examples), self.examples_file)

    def _add_example_from_request(self, request: dict[str, Any]) -> None:
        """Add an example from a request dictionary."""
        http_request = request.get('http_request', {})
        body = http_request.get('body', '')

        # Skip examples without a body
        if not body:
            return

        self.examples.append(AnnotatedExample(
            id=request.get('id', ''),
            scenario=request.get('scenario', ''),
            body=body,
            http_request=http_request,
            annotations=request.get('pii_annotations', []),
            data_types_present=request.get('data_types_present', [])
        ))

    def get_similar_examples(
            self,
            body: str,
            top_k: int = 5,
            min_similarity: float = 0.0
    ) -> list[AnnotatedExample]:
        """
        Retrieve the examples whose bodies are most similar to a given message body.

        Args:
            body: The body of the processed message.
            top_k: Maximum number of examples to return.
            min_similarity: Minimum similarity threshold (0.0 to 1.0).

        Returns:
            List of AnnotatedExample objects sorted by similarity (highest first).
        """
        if self.embeddings is None or not body:
            return []

        query_embedding = self.embedding_model.encode_normalized([body])[0]
        similarities = np.dot(self.embeddings, query_embedding)
        top_indices = np.argsort(similarities)[::-1][:top_k]

        results = []
        for idx in top_indices:
            similarity = float(similarities[idx])
            if similarity < min_similarity:
                continue

            example = self.examples[idx]
            results.append(AnnotatedExample(
                id=example.id,
                scenario=example.scenario,
                body=example.body,
                http_request=example.http_request,
                annotations=example.annotations,
                data_types_present=example.data_types_present,
                similarity_score=similarity
            ))

        return results

    def get_examples_by_labels(
            self,
            target_labels: list[str],
            max_examples: int = 10
    ) -> list[AnnotatedExample]:
        """
        Retrieve the minimum number of examples needed to cover all specified labels.

        Uses a greedy set-cover algorithm:
        1. Find the example that covers the most uncovered labels
        2. Add it to the result and mark those labels as covered
        3. Repeat with remaining uncovered labels until all are covered

        If full coverage is impossible (some labels have no examples),
        stops when no more progress can be made or max_examples is reached.

        Args:
            target_labels: List of labels that need to be covered.
            max_examples: Maximum number of examples if full coverage is impossible.

        Returns:
            List of AnnotatedExample objects that together cover all (or maximum possible) target labels.
        """
        if not target_labels or not self.examples:
            return []

        uncovered_labels = set(target_labels)
        selected_examples = []
        used_indices = set()

        # Greedy selection: pick examples that cover the most uncovered labels
        while uncovered_labels and len(selected_examples) < max_examples:
            best_example = None
            best_example_idx = -1
            best_coverage = 0

            for idx, example in enumerate(self.examples):
                if idx in used_indices:
                    continue

                coverage = len(uncovered_labels & set(example.data_types_present))
                if coverage > best_coverage:
                    best_coverage = coverage
                    best_example = example
                    best_example_idx = idx

            # If no example covers any uncovered labels, stop
            if best_coverage == 0:
                break

            selected_examples.append(best_example)
            used_indices.add(best_example_idx)
            uncovered_labels -= set(best_example.data_types_present)

        return selected_examples

    def get_combined_examples_for_api(
            self,
            body: str,
            target_labels: list[str],
            task: str = 'annotate',
            max_similar_examples: int = 3,
            max_label_examples: int = 10,
            min_similarity: float = 0.0
    ) -> list[tuple[str, str]]:
        """
        Retrieve examples using both payload similarity AND label matching.

        Combines:
        1. Up to max_similar_examples with similar payloads
        2. Minimum examples needed to cover all target labels (greedy set-cover)
           - If full coverage impossible, limited to max_label_examples

        Returns:
            List of (user_message, assistant_message) tuples for few-shot prompting.
        """
        similar_examples = self.get_similar_examples(body, max_similar_examples, min_similarity)
        label_examples = self.get_examples_by_labels(target_labels, max_label_examples)

        # Combine and deduplicate (prefer similar examples, then add label examples)
        seen_ids = set()
        combined_examples = []
        for example in similar_examples + label_examples:
            if example.id not in seen_ids:
                seen_ids.add(example.id)
                combined_examples.append(example)

        return self._format_examples_for_api(combined_examples, task)

    def get_similar_examples_for_api(
            self,
            body: str,
            task: str = 'annotate',
            top_k: int = 5,
            min_similarity: float = 0.0
    ) -> list[tuple[str, str]]:
        """
        Retrieve similar examples formatted as (user_msg, assistant_msg) tuples
        for use as few-shot examples in LLM API calls.
        """
        return self._format_examples_for_api(self.get_similar_examples(body, top_k, min_similarity), task)

    @staticmethod
    def _format_examples_for_api(examples: list[AnnotatedExample], task: str) -> list[tuple[str, str]]:
        """
        Format examples as (user_msg, assistant_msg) tuples, where the user message is the HTTP body and the
        assistant message adheres to the response schema of the given task.
        """
        api_examples = []
        for example in examples:
            if task == 'classify':
                # Classifier expects {"data_types": ["type1", "type2", ...]}
                assistant_msg = json.dumps({
                    "data_types": example.data_types_present
                })
            else:
                # Annotator/Reviewer/TargetedAnnotator expect {"annotations": [{"data_type": ..., "value": ...}, ...]}
                assistant_msg = json.dumps({
                    "annotations": [
                        {
                            "data_type": annotation.get("data_type", ""),
                            "value": annotation.get("value", "")
                        }
                        for annotation in example.annotations
                    ]
                })

            api_examples.append((example.body, assistant_msg))

        return api_examples
