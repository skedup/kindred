"""Dream Step 5 使用的窄 SOUL excerpt compiler。"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from kindred.llm.client import LlmClient
from kindred.state.dream import SoulExcerptResponse

ExcerptCompiler = Callable[[str, str], str]


class ExcerptContractError(ValueError):
    """摘录编译调用或输出不符合窄合同。"""


def make_excerpt_compiler(client: LlmClient) -> ExcerptCompiler:
    """复用 Dream client；输入仅最终候选 SOUL 与旧摘录。"""

    def compile_excerpt(final_soul: str, previous_excerpt: str) -> str:
        prompt = (
            "## 最终候选 SOUL\n"
            f"{final_soul.strip() or '（空）'}\n\n"
            "## 旧摘录\n"
            f"{previous_excerpt.strip() or '（缺失）'}\n"
        )
        try:
            output = client.complete(prompt, role="dream.excerpt")
            return SoulExcerptResponse.model_validate(output).soul_excerpt
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            raise ExcerptContractError("SOUL excerpt output violates contract") from exc

    return compile_excerpt


__all__ = ["ExcerptCompiler", "ExcerptContractError", "make_excerpt_compiler"]
