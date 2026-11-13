"""test_glossary_verify.py — 译名一致性校验：可校验判定、分层过滤、收敛性"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scraper
from scraper import (
    _verification_core,
    find_missing_terms,
    repair_glossary_misses,
    select_verifiable_terms,
    translate_blocks,
)

# ── 译名一致性校验（方案 C）───────────────────────────

class TestVerificationCore:
    def test_accepts_plain_names(self):
        for zh in ("硫方怪", "烈性硫黄", "下界之星", "恶魂"):
            assert _verification_core(zh) == zh

    @pytest.mark.parametrize("zh", [
        "只有是状态效果时译为'凋零'，否则译为'凋灵'",   # 说明文字
        "混沌立方(*)",                              # 占位符
        "Minecraft: 基岩版",                        # 含 ASCII，没法做包含判断
        "更新(如Drop 1 2026 译为2026年第一次更新)",
        "",                                         # 空
    ])
    def test_rejects_unverifiable(self, zh):
        assert _verification_core(zh) is None


class TestSelectVerifiable:
    def setup_method(self):
        self.glossary = {
            "terms": {"Sulfur Cube": "硫方怪", "Minecraft World": "MC乐园(*)"},
            "soft": {"Sulfur": "硫黄"},
            "ambiguous": {"Wither": {"primary": "凋灵", "others": ["凋零"]}},
        }

    def test_only_hard_tier(self):
        selected = select_verifiable_terms("Sulfur Cube and Sulfur", self.glossary)
        assert selected == {"Sulfur Cube": "硫方怪"}

    def test_skips_unverifiable_values(self):
        assert "Minecraft World" not in select_verifiable_terms("Minecraft World", self.glossary)

    def test_skips_ambiguous(self):
        glossary = {"terms": {"Wither": "凋灵"}, "ambiguous": {"Wither": {"others": ["凋零"]}}}
        assert select_verifiable_terms("Wither", glossary) == {}

    def test_find_missing(self):
        assert find_missing_terms("Sulfur Cube", "硫方怪出现了", self.glossary) == {}
        assert find_missing_terms("Sulfur Cube", "方块出现了", self.glossary) == {"Sulfur Cube": "硫方怪"}
        assert find_missing_terms("Sulfur Cube", "", self.glossary) == {}


class TestRepairGlossaryMisses:
    """收敛性是重点：不配合的模型必须被有限次放弃，不能循环"""

    GLOSSARY = {"terms": {"Sulfur Cube": "硫方怪"}}
    ITEMS = [{"id": "t0000", "text": "Sulfur Cube spawns"}]
    PROMPTS = {"translate_blocks_system": "sys"}

    def _run(self, replies, max_rounds=1):
        calls = []

        def run_batch(batch, system_prompt):
            calls.append([i["id"] for i in batch])
            return replies.pop(0) if replies else {}

        out = repair_glossary_misses(
            self.ITEMS, {0: "方块出现了"}, self.GLOSSARY, run_batch, self.PROMPTS,
            max_rounds=max_rounds,
        )
        return out, calls

    def test_fixes_when_model_complies(self):
        out, calls = self._run([{0: "硫方怪生成了"}])
        assert out[0] == "硫方怪生成了"
        assert len(calls) == 1

    def test_gives_up_after_budget(self):
        # 模型始终不配合 → 只调用 max_rounds 次，保留原译，不抛异常
        out, calls = self._run([{0: "还是方块"}, {0: "依然是方块"}], max_rounds=3)
        assert out[0] == "方块出现了"
        assert len(calls) == 1, "修复数为 0 时必须立刻停止，不能把剩余轮数烧完"

    def test_keeps_original_when_retry_still_bad(self):
        out, calls = self._run([{0: "还是方块"}])
        assert out[0] == "方块出现了", "未通过校验的重译必须丢弃，避免来回横跳"
        assert len(calls) == 1

    def test_zero_rounds_makes_no_calls(self):
        out, calls = self._run([], max_rounds=0)
        assert out[0] == "方块出现了"
        assert calls == []

    def test_no_misses_makes_no_calls(self):
        calls = []

        def run_batch(batch, system_prompt):
            calls.append(batch)
            return {}

        out = repair_glossary_misses(
            [{"id": "t0000", "text": "Sulfur Cube"}], {0: "硫方怪"},
            self.GLOSSARY, run_batch, self.PROMPTS,
        )
        assert out[0] == "硫方怪"
        assert calls == []

    def test_repair_prompt_carries_expected_name(self):
        seen = {}

        def run_batch(batch, system_prompt):
            seen["prompt"] = system_prompt
            return {0: "硫方怪生成了"}

        repair_glossary_misses(
            self.ITEMS, {0: "方块出现了"}, self.GLOSSARY, run_batch, self.PROMPTS,
        )
        assert "Sulfur Cube 必须译为「硫方怪」" in seen["prompt"]


class TestVerifyInTranslateBlocks:
    """端到端：主翻译漏用译名 → 补一轮；模型死不悔改 → 有界退出"""

    def _cfg(self, max_rounds=1):
        return {
            "prompts": {"translate_blocks_system": "sys"},
            "concurrency": {"translation_workers": 1, "batch_max_chars": 10000, "batch_max_items": 10},
            "glossary": {"verify": True, "verify_max_rounds": max_rounds, "verify_max_items": 20},
        }

    def _patch(self, monkeypatch, replies):
        calls = []

        def fake_translate(text, **kwargs):
            calls.append(kwargs.get("system_prompt", ""))
            batch = json.loads(text)
            reply = replies.pop(0) if replies else "方块"
            return json.dumps(
                {"translations": [{"id": i["id"], "translated_text": reply} for i in batch]},
                ensure_ascii=False,
            )

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        return calls

    def _blocks(self):
        return [{"id": "b0001", "type": "p", "source_text": "Sulfur Cube spawns"}]

    def test_repairs_missing_name(self, monkeypatch):
        calls = self._patch(monkeypatch, ["方块出现了", "硫方怪生成了"])
        blocks = translate_blocks(self._blocks(), config=self._cfg(),
                                  glossary={"terms": {"Sulfur Cube": "硫方怪"}})
        assert blocks[0]["translated_text"] == "硫方怪生成了"
        assert len(calls) == 2

    def test_bounded_when_model_never_complies(self, monkeypatch):
        calls = self._patch(monkeypatch, ["方块出现了"])
        blocks = translate_blocks(self._blocks(), config=self._cfg(max_rounds=1),
                                  glossary={"terms": {"Sulfur Cube": "硫方怪"}})
        assert blocks[0]["translated_text"] == "方块出现了"   # 保留原译
        assert len(calls) == 2, "主翻译 1 次 + 补译 1 次，绝不无限重试"
