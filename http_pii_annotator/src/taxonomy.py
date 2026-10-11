from dataclasses import dataclass
from pathlib import Path

from src.errors import ConfigError

LABEL_COLUMN = "PII Data type"


@dataclass(frozen=True)
class Taxonomy:
    """
    A label taxonomy, defined by a Markdown table with a column named 'PII Data type'.

    Attributes:
        name: Name of the taxonomy, i.e. the name of its file without extension
        text: The complete Markdown table, which is included in the prompts
        labels: The labels of the taxonomy, i.e. the values of the 'PII Data type' column
    """
    name: str
    text: str
    labels: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str) -> "Taxonomy":
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"Missing label taxonomy - expected Markdown table in {path}")

        text = path.read_text(encoding="utf-8")
        return cls(name=path.stem, text=text, labels=tuple(cls.parse_labels(text, source=str(path))))

    @staticmethod
    def parse_labels(text: str, source: str = "taxonomy") -> list[str]:
        """Extracts the values of the 'PII Data type' column from a Markdown table."""
        lines = text.strip().split('\n')
        if len(lines) < 2:
            raise ConfigError(f"Invalid taxonomy format - expected Markdown table in {source}")

        headers = [h.strip() for h in lines[0].split('|') if h.strip()]
        column_index = next((i for i, header in enumerate(headers) if header.lower() == LABEL_COLUMN.lower()), None)
        if column_index is None:
            raise ConfigError(f"Taxonomy does not contain a '{LABEL_COLUMN}' column in {source}")

        labels = []
        for line in lines[2:]:  # Skip header and separator rows
            if line.strip() and '|' in line:
                columns = [c.strip() for c in line.split('|') if c.strip()]
                if len(columns) > column_index and columns[column_index]:
                    labels.append(columns[column_index])
        return labels
