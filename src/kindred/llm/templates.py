"""共享的 user prompt 模板加载器（jinja2）。

收拢动机（skedush 6/21）：user prompt 原本散在两处——sense 用 ``prompts/*.j2``
模板，act / dream×3 在各自 Python 文件里 inline 拼字符串。文案与逻辑混在一起，
改文案要碰 Python、diff 不清晰、打包路径分散。

收拢原则（**只收文案，不收逻辑**）：
- **固定文案** → ``prompts/<role>_user.md.j2`` 模板（人类可读、改文案不碰 Python）。
- **数据加工**（dict/list → 文本行、effect resolve、step 分支）→ 留在 Python，
  算好 context 丢给模板。j2 不塞业务逻辑。
- **system prompt**（机制 + schema 契约）→ 留在 ``real_client.py``（契约层，与
  ``_with_contract`` / schema 绑定，不属本次收拢范围）。

所有 user prompt 模板共用同一个 Environment（trim_blocks/lstrip_blocks + 关
autoescape——输出是 prompt 文本不是 HTML）。模板缺失即启动炸（打包问题早暴露）。
"""

from __future__ import annotations

from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jinja2 import Environment
    from jinja2 import Template as JinjaTemplate

# prompt 模板目录（与本模块同包：kindred/llm/prompts/）。
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=1)
def _prompt_env() -> Environment:
    """共享 jinja2 Environment（缓存单例）。

    trim_blocks/lstrip_blocks 让 ``{%- ... -%}`` 控制空白更干净；autoescape 关闭
    （prompt 文本非 HTML）；keep_trailing_newline 保留模板末尾换行。
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@cache
def get_prompt_template(name: str) -> JinjaTemplate:
    """按文件名加载并缓存某个 user prompt 模板。

    ``name`` 如 ``"sense_user.md.j2"``。模板缺失 → ``TemplateNotFound`` 启动即炸
    （打包遗漏应早暴露，不静默降级）。
    """
    return _prompt_env().get_template(name)


def render_prompt(name: str, /, **context: Any) -> str:
    """加载模板并渲染——大多数调用方的便捷入口。"""
    return get_prompt_template(name).render(**context)
