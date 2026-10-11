import logging

import numpy as np

from src.embedding import EmbeddingModel

logger = logging.getLogger(__name__)


class Validator:
    """
    Validates the results of the LLM steps: checks their structure and maps labels that are not part of the
    taxonomy to the most similar valid label, or rejects them if no valid label is similar enough.
    """

    def __init__(self, labels: list[str], embedding_model: EmbeddingModel, min_similarity: float = 0.4):
        """
        Args:
            labels: The labels of the taxonomy
            embedding_model: Model that embeds the labels
            min_similarity: Cosine similarity that the most similar valid label must reach for an invalid label
                to be replaced by it; below it, the label and its annotation are rejected
        """
        self.labels = list(labels)
        self.embedding_model = embedding_model
        self.min_similarity = min_similarity
        self._label_embeddings: np.ndarray = None

    def validate_labels(self, predicted_labels, rejected: list[str] = None) -> list[str]:
        """
        Validates the result of a classification.

        Entries that are not strings are removed, labels that are not part of the taxonomy are replaced by the
        closest valid label or removed if it is not similar enough, and duplicates are removed while preserving
        the order. The labels that were removed for this reason are appended to 'rejected'.
        """
        if not isinstance(predicted_labels, list):
            if predicted_labels is not None:
                logger.warning("Predicted labels are not a list, replacing them with an empty list")
            return []

        valid_labels = []
        for label in predicted_labels:
            if not isinstance(label, str):
                logger.warning("Label %r is not a string, skipping", label)
                continue

            valid_label = self._to_valid_label(label)
            if valid_label is None:
                if rejected is not None and label not in rejected:
                    rejected.append(label)
            elif valid_label not in valid_labels:
                valid_labels.append(valid_label)

        return valid_labels

    def validate_annotations(self, annotations, rejected: list[str] = None) -> list[dict]:
        """
        Validates the result of an annotation or a review.

        Entries that are not objects with string fields 'data_type' and 'value' are removed, labels that are not
        part of the taxonomy are replaced by the closest valid label, annotations whose label has no sufficiently
        similar valid label are removed, and all other fields are dropped. The labels of the annotations that were
        removed for this reason are appended to 'rejected'.
        """
        if not isinstance(annotations, list):
            if annotations is not None:
                logger.warning("Annotations are not a list, replacing them with an empty list")
            return []

        valid_annotations = []
        for annotation in annotations:
            if not isinstance(annotation, dict):
                logger.warning("Annotation %r is not an object, skipping", annotation)
                continue

            if not isinstance(annotation.get('data_type'), str) or not isinstance(annotation.get('value'), str):
                logger.warning("Annotation without string fields 'data_type' and 'value', skipping: %r", annotation)
                continue

            data_type = self._to_valid_label(annotation['data_type'])
            if data_type is None:
                if rejected is not None and annotation['data_type'] not in rejected:
                    rejected.append(annotation['data_type'])
                continue

            valid_annotations.append({'data_type': data_type, 'value': annotation['value']})

        return valid_annotations

    def _to_valid_label(self, label: str) -> str | None:
        """
        Returns the label if it is part of the taxonomy, or else the closest valid label. Returns None if the
        closest valid label is less similar than the minimum similarity.
        """
        if not self.labels or label in self.labels:
            return label
        return self.closest_label(label)

    def closest_label(self, label: str) -> str | None:
        """
        Identifies the valid label whose embedding has the highest cosine similarity to the given label. Returns
        None if that similarity is below the minimum similarity.
        """
        if not self.labels:
            return None

        if self._label_embeddings is None:
            self._label_embeddings = self.embedding_model.encode_normalized(self.labels)

        label_embedding = self.embedding_model.encode_normalized([label])[0]
        similarities = np.dot(self._label_embeddings, label_embedding)

        best_index = int(np.argmax(similarities))
        if similarities[best_index] < self.min_similarity:
            logger.warning("Rejected invalid label '%s': the closest valid label '%s' is not similar enough "
                           "(similarity: %.4f, minimum: %.2f)", label, self.labels[best_index],
                           similarities[best_index], self.min_similarity)
            return None
        logger.info("Corrected invalid label '%s' -> '%s' (similarity: %.4f)", label, self.labels[best_index],
                    similarities[best_index])
        return self.labels[best_index]
