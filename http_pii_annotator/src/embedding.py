import os
import threading

import numpy as np

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DEFAULT_MODEL = 'all-MiniLM-L6-v2'


class EmbeddingModel:
    """
    Sentence embedding model shared by example retrieval and label correction.
    The underlying SBERT model is loaded on first use.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def encode(self, texts: list[str]) -> np.ndarray:
        """Returns one embedding per text."""
        with self._lock:
            if self._model is None:
                self._model = self._load()
            return self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)

    def _load(self):
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(self.model_name)

    def encode_normalized(self, texts: list[str]) -> np.ndarray:
        """Returns one embedding of unit length per text, such that dot products are cosine similarities."""
        embeddings = self.encode(texts)
        return embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)
