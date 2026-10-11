import json
import logging
import os
import re
import shutil

import chardet

logger = logging.getLogger(__name__)

SRC_DIR = os.path.dirname(os.path.realpath(__file__))
TAXONOMY_DIR = os.path.join(SRC_DIR, "taxonomies")
PROMPTS_DIR = os.path.join(SRC_DIR, "prompts")

def setup_logging(verbose: bool = False, quiet: bool = False):
    """Logs the messages of the package to the console; other libraries only log warnings."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    level = logging.WARNING if quiet else logging.DEBUG if verbose else logging.INFO
    # a module that is run with python -m logs under the name __main__
    for name in ("src", "__main__"):
        logging.getLogger(name).setLevel(level)


def bundled_taxonomies() -> list:
    return sorted(name[:-3] for name in os.listdir(TAXONOMY_DIR) if name.endswith(".md"))


def schema_path(schema: str) -> str:
    """Path of the Markdown file of a taxonomy, given as the name of a bundled taxonomy or as a path."""
    if schema.endswith(".md") or "/" in schema or "\\" in schema:
        return schema
    return os.path.join(TAXONOMY_DIR, f"{schema}.md")


def schema_name(schema: str) -> str:
    return os.path.splitext(os.path.basename(schema_path(schema)))[0]


def default_hostname(api: str):
    """Endpoint of the API from the environment: OLLAMA_HOSTNAME for Ollama, otherwise <API>_API_BASE."""
    return (os.getenv("OLLAMA_HOSTNAME") if api == "ollama" else None) or os.getenv(f"{api.upper()}_API_BASE")


def _detect_encoding(file_path: str) -> str:
    """
    Detects the encoding of a file.
    Args:
        file_path (str): The path to the file.
    Returns:
        str: The detected encoding of the file.
    """
    with open(file_path, 'rb') as file:
        raw_data = file.read()
        result = chardet.detect(raw_data)
        return result['encoding']

def write_to_file(file_name: str, text: str):
    """
    Writes text to a file, creating the file if it does not exist.
    Args:
        file_name (str): The name of the file to write to.
        text (str): The text to write to the file.
    """
    with open(file_name, 'w', encoding='utf-8') as file:
        file.write(text)


def read_from_file(file_path: str):
    """
    Reads a file and returns its content as a string, detecting the encoding automatically.
    Args:
        file_path (str): The path to the file to read.
    Returns:
        str | None: The content of the file as a string or None if the file does not exist.
    """
    if not os.path.exists(file_path):
        logger.info(f"File '{file_path}' does not exist.")
        return None

    # Read the file with the detected encoding
    content = ''
    with open(file_path, encoding=_detect_encoding(file_path)) as file:
        for line in file:
            content += line
        return content


def prepare_output(root_folder: str, overwrite: bool = True):
    if overwrite and os.path.exists(f'{root_folder}/output'):
        shutil.rmtree(f'{root_folder}/output')
    if overwrite and os.path.exists(f'{root_folder}/log'):
        shutil.rmtree(f'{root_folder}/log')

    path_list = [
        f'{root_folder}',
        f'{root_folder}/log',
        f'{root_folder}/input',
        f'{root_folder}/output',
        f'{root_folder}/output/1_scenarios',
        f'{root_folder}/output/2_scenarios_validated',
        f'{root_folder}/output/3_requests',
        f'{root_folder}/output/4_requests_validated',
        f'{root_folder}/output/5_responses',
        f'{root_folder}/output/6_responses_validated',
        f'{root_folder}/output/7_encoded',
        f'{root_folder}/schema',
    ]

    for path in path_list:
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)


def load_json(file_path: str):
    try:
        with open(file_path) as file:
            return json.load(file)
    except FileNotFoundError:
        logger.info(f"The file {file_path} was not found.")
        return None
    except json.JSONDecodeError:
        logger.info(f"The file {file_path} does not contain valid JSON.")
        return None


def load_jsonl(file_path: str):
    """
    Reads a JSONL file and returns its content as a list of dictionaries.
    Args:
        file_path (str): The path to the JSONL file.
    Returns:
        list | None: The content of the JSONL file as a list of dictionaries or None if the file does not exist.
    """
    if not os.path.exists(file_path):
        logger.info(f"File '{file_path}' does not exist.")
        return None

    try:
        items = []
        with open(file_path, encoding=_detect_encoding(file_path)) as file:
            for line_num, line in enumerate(file, 1):
                line = line.strip()
                if line:  # Skip empty lines
                    try:
                        item = json.loads(line)
                        items.append(item)
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing JSON on line {line_num} in {file_path}: {e}")
                        continue
        return items
    except Exception as e:
        logger.error(f"Error reading JSONL file {file_path}: {e}")
        return None


def log_prompt_result(
        log_dir: str,
        task: str,
        pkg: str,
        model_name: str,
        output_format: str,
        processing_time: float,
        outputs: list
):
    # model and message names may contain path separators (e.g. 'unsloth/GLM-5-GGUF:Q4_K_M')
    model_name = re.sub(r'[^A-Za-z0-9._-]+', '_', str(model_name))
    pkg = re.sub(r'[^A-Za-z0-9._-]+', '_', str(pkg))

    # create the output folder if it does not exist
    folder_path = os.path.join(log_dir, f"{model_name}_responses")
    os.makedirs(f"{folder_path}/{task}", exist_ok=True)

    # log the processing time and response
    with open(f"{folder_path}/times_{task}.csv", "a") as f:
        f.write(f"{pkg},{processing_time}\n")

    if len(outputs) > 1:
        for i, output in enumerate(outputs):
            with open(f"{folder_path}/{task}/{pkg}_{i}.{output_format}", "a", encoding="utf-8") as f:
                f.write(output + '\n')
    else:
        with open(f"{folder_path}/{task}/{pkg}.{output_format}", "a", encoding="utf-8") as f:
            f.write(outputs[0] + '\n')
