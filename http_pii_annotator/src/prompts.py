"""
Assembly of the prompts of the LLM tasks from the files in the 'prompts' folder of the package.
"""
import json
from pathlib import Path

from src.errors import ConfigError
from src.llm_connectors.api_base import Prompt

TASKS = ("classify", "annotate", "targeted_annotate", "review")


class PromptBuilder:
    """
    Builds the prompts of a task. The task's files are read once from 'prompts/<task>/':

    - system.*: system prompt
    - response_schema.json: JSON schema of the expected response
    - example_<index>_user.* and example_<index>_assistant.*: static few-shot examples (optional), which are
      only used if no examples are provided for a prompt
    """

    def __init__(self, task: str, prompts_folder=None):
        self.task = task
        folder = (prompts_folder or Path(__file__).resolve().parent / "prompts") / task
        if not folder.is_dir():
            raise ConfigError(f"No prompts found for task '{task}' in {folder}")

        self.system = None
        self.response_schema = None
        static_examples = {}

        for file in sorted(folder.iterdir(), key=lambda f: f.name):
            name = file.name
            if name.startswith("response_schema."):
                self.response_schema = json.loads(file.read_text(encoding="utf-8"))
            elif name.startswith("system."):
                self.system = self._read(file)
            elif name.startswith("example_"):
                parts = name.split("_")
                index, role = parts[1], parts[-1].split(".")[0]
                if role not in ("user", "assistant"):
                    raise ConfigError(f"Unknown example file {file}: expected '_user' or '_assistant'.")
                static_examples.setdefault(index, {})[role] = self._read(file)

        if self.system is None:
            raise ConfigError(f"No system prompt found for task '{task}' in {folder}")

        self.static_examples = [(example.get("user"), example.get("assistant"))
                                for example in static_examples.values()]

    @staticmethod
    def _read(file) -> str:
        text = file.read_text(encoding="utf-8")
        if file.name.endswith("json"):
            text = json.dumps(json.loads(text))
        return text

    def build(self, taxonomy: str, user_msgs: list[str], examples: list[tuple[str, str]] = None) -> Prompt:
        """
        Builds a prompt consisting of the system prompt, the taxonomy, the examples and the user messages.

        Args:
            taxonomy: Label taxonomy as a Markdown table, sent as the first user message
            user_msgs: User messages of the processed HTTP message
            examples: Pairs of user message and expected assistant message
        """
        messages = [{"role": "user", "content": taxonomy}]
        for example_user_msg, example_assistant_msg in examples or self.static_examples:
            messages.append({"role": "user", "content": example_user_msg})
            messages.append({"role": "assistant", "content": example_assistant_msg})
        for user_msg in user_msgs:
            messages.append({"role": "user", "content": user_msg})

        return Prompt(task=self.task, system=self.system, messages=tuple(messages), response_schema=self.response_schema)
