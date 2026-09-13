"""外部插件示例：敏感词遮蔽清洗器 —— 真正的「拖进来就能用」。

使用方式
--------
1. 把本文件复制/修改后放进 ``ext_plugins/`` 目录
2. 重启服务（或调用 ``POST /api/reload``）
3. 在 ``config/pipeline.yaml`` 的 cleaner 槽位把 ``use`` 改成 ``sensitive_word_cleaner``

注意：外部插件只依赖平台的公共 API（``app.core``），不依赖任何内部实现细节。
"""

from __future__ import annotations

import re

from app.core.context import CLEAN_TEXT, TEXT, PipelineContext
from app.core.skill import Skill, skill


@skill
class SensitiveWordCleaner(Skill):
    """把命中的敏感词替换为等长掩码，适合合规场景。"""

    name = "sensitive_word_cleaner"
    version = "1.0.0"
    slot = "cleaner"  # 与 basic_cleaner 同槽位 → 可直接替换
    description = "命中敏感词后以 * 掩码替换，支持自定义词表与大小写不敏感匹配。"
    consumes = (TEXT,)
    produces = (CLEAN_TEXT,)
    param_schema = {
        "words": {
            "type": "list",
            "default": ["内部机密", "secret", "confidential"],
            "label": "敏感词表",
        },
        "mask_char": {"type": "str", "default": "*", "label": "掩码字符"},
        "ignore_case": {"type": "bool", "default": True, "label": "忽略大小写"},
    }

    def configure(self, options: dict) -> None:
        self.words: list[str] = list(options.get("words", ["内部机密", "secret", "confidential"]))
        self.mask_char = options.get("mask_char", "*")
        self.ignore_case = bool(options.get("ignore_case", True))

    def run(self, ctx: PipelineContext) -> None:
        text: str = ctx.require(TEXT)
        hits: dict[str, int] = {}

        for word in self.words:
            if not word:
                continue
            pattern = re.compile(re.escape(word), re.IGNORECASE if self.ignore_case else 0)
            text, count = pattern.subn(self.mask_char * len(word), text)
            if count:
                hits[word] = count

        ctx.put(CLEAN_TEXT, text, producer=self.name, masked_words=hits)
