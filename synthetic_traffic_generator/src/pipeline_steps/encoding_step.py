"""
Pipeline step that applies encodings and compression to the validated plaintext messages.
"""

import logging
import os
from typing import Any

from src import encoding
from src.pipeline_steps.pipeline_step import PipelineStep
from src.state_manager import BaseStateManager

logger = logging.getLogger(__name__)


class EncodingStep(PipelineStep):
    """
    Pipeline step that encodes the validated requests and responses.

    Input: Validated plaintext requests and responses
    Output: The same messages with encoded and compressed values and bodies (7_encoded/)

    The plaintext messages stay untouched in their folders; each encoded message records what
    was applied to it and can be decoded again (see src/encoding.py).
    """

    def __init__(
            self,
            run_id: str,
            state_manager: BaseStateManager,
            in_folder: str,
            out_folder: str,
            skip: bool = False,
            seed: int = 0,
            profile: dict = None,
    ):
        super().__init__(
            run_id=run_id,
            task="encoding",
            details="Apply encodings and compression to the validated messages",
            skip=skip,
            is_llm_step=False,
            in_folder=in_folder,
            out_folder=out_folder,
            state_manager=state_manager,
        )
        self.seed = seed
        self.profile = profile

    def prepare_input(self, messages: list) -> list:
        # the step reads the validated requests and responses itself
        return [{}]

    async def execute(self, message: dict) -> dict[str, Any]:
        summary = encoding.encode_run(os.path.dirname(self.out_folder), self.seed, self.profile)

        logger.info("\n[Encoding] Encoding complete:")
        for kind in ("requests", "responses"):
            if kind not in summary:
                continue
            report = summary[kind]
            logger.info(f"  {kind}: {report['body_kind_shares']}")
            logger.info(f"    annotations: {report['annotations_visible']} visible, {report['annotations_hidden']} hidden "
                  f"({report['annotations_with_encoded_value']} with encoded value)")
            if report["verification_errors"]:
                logger.info(f"    {len(report['verification_errors'])} verification errors (messages left in plaintext)")
        return summary
