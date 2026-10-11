"""
Records that are passed between the pipeline steps and stored in their JSONL output files.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from src.errors import PipelineError

MESSAGE_ID = "message_id"


@dataclass(frozen=True)
class Annotation:
    """A PII value in a message body and its data type."""
    data_type: str
    value: str

    def to_dict(self) -> dict:
        return {"data_type": self.data_type, "value": self.value}


@dataclass
class Message:
    """
    An HTTP message on its way through the pipeline.

    Attributes:
        message_id: Identifier of the message within a run, assigned when the input is loaded
        fields: All fields of the HTTP message (method, path, headers, body, ...), passed through unchanged
        predicted_labels: Data types predicted by the classifier (None if the message was not classified)
        annotations: Annotations made by the latest annotation or review step (None if not annotated)
        error: Description of the error that prevented the latest step from processing the message
    """
    message_id: str
    fields: dict = field(default_factory=dict)
    predicted_labels: list | None = None
    annotations: list | None = None
    error: str | None = None

    @property
    def body(self) -> str:
        return self.fields.get('body', '')

    @classmethod
    def from_dict(cls, record: dict, message_id: str = None) -> "Message":
        fields = dict(record)
        return cls(
            message_id=fields.pop(MESSAGE_ID, None) or message_id,
            predicted_labels=fields.pop('predicted_labels', None),
            annotations=fields.pop('annotations', None),
            error=fields.pop('error', None),
            fields=fields
        )

    def to_dict(self) -> dict:
        record = {MESSAGE_ID: self.message_id, **self.fields}
        if self.predicted_labels is not None:
            record['predicted_labels'] = self.predicted_labels
        if self.annotations is not None:
            record['annotations'] = self.annotations
        if self.error is not None:
            record['error'] = self.error
        return record

    def copy(self, **changes) -> "Message":
        values = dict(message_id=self.message_id, fields=dict(self.fields), predicted_labels=self.predicted_labels,
                      annotations=self.annotations, error=self.error)
        values.update(changes)
        return Message(**values)


def positional_id(index: int, record: dict) -> str:
    """Identifier of the record at the given position of an input; includes the record's own 'id' if it has one."""
    own_id = record.get('id') if isinstance(record, dict) else None
    return f"{index:06d}_{own_id}" if own_id else f"{index:06d}"


def read_records(path: Path | str, encoding: str = "utf-8") -> list[dict]:
    """
    Loads records from a JSON/JSONL file or from all JSON/JSONL files in a folder (in alphabetical order).
    JSON files are expected to contain an array of records, JSONL files one record per line.
    """
    path = Path(path)
    if path.is_dir():
        files = sorted(f for f in path.iterdir() if f.is_file() and not f.name.startswith('.'))
    elif path.is_file():
        files = [path]
    else:
        return []

    records = []
    for file in files:
        if file.suffix == '.jsonl':
            with open(file, encoding=encoding) as f:
                for line_number, line in enumerate(f, 1):
                    if line.strip():
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError as e:
                            raise PipelineError(f"Invalid JSON on line {line_number} of {file}: {e}")
        elif file.suffix == '.json':
            with open(file, encoding=encoding) as f:
                content = json.load(f)
            if isinstance(content, list):
                records.extend(content)
    return records


def read_messages(path: Path | str, encoding: str = "utf-8") -> list[Message]:
    """
    Loads the messages of a pipeline input or of the output of a pipeline step.

    Input messages may be wrapped together with their ground truth annotations ('http_request'); only the
    message itself is returned. Messages without an identifier are identified by their position.
    """
    messages = []
    for index, record in enumerate(read_records(path, encoding)):
        if isinstance(record, str):
            record = json.loads(record)
        message_id = positional_id(index, record)
        if record.get('http_request') is not None:
            record = record['http_request']
        messages.append(Message.from_dict(record, message_id=message_id))
    return messages


def append_jsonl(path: Path | str, record: dict):
    with open(path, 'a', encoding='utf-8') as file:
        file.write(json.dumps(record) + '\n')


def write_jsonl(path: Path | str, records: list[dict]):
    """Replaces the content of a JSONL file atomically."""
    path = Path(path)
    temporary_path = path.with_name(path.name + '.tmp')
    with open(temporary_path, 'w', encoding='utf-8') as file:
        for record in records:
            file.write(json.dumps(record) + '\n')
    temporary_path.replace(path)
