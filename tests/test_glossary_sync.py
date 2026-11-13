"""test_glossary_sync.py — 词表分层：同步脚本、快速匹配、注入策略"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scraper
from scraper import (
    _build_term_index,
    _merge_glossaries,
    _read_glossary_file,
    build_glossary_prompt,
    build_glossary_prompt_for,
    find_relevant_terms,
    select_relevant_terms,
    translate_blocks,
)
from tools.sync_glossary import build_auto, is_term_like, namespace_of, parse_tsv

# ── tools/sync_glossary.py ───────────────────────────

TSV_SAMPLE = (
    "key\ten_us\tzh_cn\tzh_tw\tzh_hk\tja_jp\n"
    "block.minecraft.sulfur\tSulfur\t硫黄\t硫磺\t硫磺\t硫黄\n"
    "item.minecraft.sulfur_cube\tSulfur Cube\t硫方怪\t硫方怪\t硫方怪\t硫方怪\n"
    "effect.minecraft.speed\tSpeed\t迅捷\t加速\t速度\t移動速度上昇\n"
    "block.minecraft.bedrock\tBedrock\t基岩\t基岩\t基岩\t岩盤\n"
    "gui.done\tDone\t完成\t完成\t完成\t完了\n"
    "death.attack.lava\t%1$s discovered the floor was lava\t%1$s发现了地板是熔岩做的\tx\ty\tz\n"
    "block.minecraft.fern\tFern\tFern\tFern\tFern\tシダ\n"
    "block.minecraft.air\tAir\t空气\t空氣\t空氣\t空気\n"
    "item.minecraft.string\tString\t线\t線\t線\t糸\n"
    "block.minecraft.old_name\tAncient Bricks\t远古砖块\tx\ty\tz\n"
    "item.minecraft.tooltip_complex\t%1$s includes only the required data.\t%1$s仅包含必要数据\tx\ty\tz\n"
)


@pytest.fixture
def rows():
    return parse_tsv(TSV_SAMPLE)


class TestParseTsv:
    def test_parses_rows(self, rows):
        assert len(rows) == 11
        assert rows[0]["key"] == "block.minecraft.sulfur"
        assert rows[0]["en_us"] == "Sulfur"

    def test_skips_empty_cells(self):
        assert parse_tsv("key\ten_us\tzh_cn\nx\t\t中文\n") == []

    def test_namespace_of(self):
        assert namespace_of("block.minecraft.sulfur") == "block"
        assert namespace_of("no_dot_key") == ""


class TestIsTermLike:
    @pytest.mark.parametrize("term", ["Sulfur", "Sulfur Cube", "Potent Sulfur Block"])
    def test_accepts_nouns(self, term):
        assert is_term_like(term)

    @pytest.mark.parametrize("term", [
        "%1$s died",                       # 格式占位符
        "Discovered the floor was lava.",  # 整句
        "One two three four",              # 超过 3 个词
        "Done",                            # 停用词
    ])
    def test_rejects_non_terms(self, term):
        assert not is_term_like(term)


class TestBuildAuto:
    def test_tiers(self, rows):
        auto, _, stats = build_auto(rows)
        assert auto["terms"]["Sulfur Cube"] == "硫方怪"
        assert auto["terms"]["Ancient Bricks"] == "远古砖块"
        assert auto["soft"]["Sulfur"] == "硫黄"
        assert auto["soft_strict"]["Speed"] == "迅捷"   # 效果名与常用词撞形
        assert stats["hard"] == 2
        assert stats["soft_strict"] == 1
        assert stats["rows"] == 11

    def test_drops(self, rows):
        auto, _, stats = build_auto(rows)
        assert "Done" not in auto["soft"]            # gui.* 不在白名单
        assert "Fern" not in auto["soft"]            # 未汉化
        assert stats["dropped_namespace"] >= 2       # gui.* / death.*
        assert stats["dropped_not_term"] >= 1
        assert stats["dropped_untranslated"] == 1    # Fern → Fern

    def test_ambiguous_recorded(self):
        tsv = (
            "key\ten_us\tzh_cn\n"
            "item.minecraft.wither_a\tWither\t凋灵\n"
            "entity.minecraft.wither_b\tWither\t凋零\n"
        )
        auto, _, _ = build_auto(parse_tsv(tsv))
        assert auto["ambiguous"]["Wither"]["others"] == ["凋零"] or \
               auto["ambiguous"]["Wither"]["others"] == ["凋灵"]

    def test_no_sources_in_term_layer(self, rows):
        """溯源信息不能混进词表层：它只属于索引文件"""
        auto, index, _ = build_auto(rows)
        assert "sources" not in auto
        assert index["sources"]["Sulfur"] == "block.minecraft.sulfur"
        assert index["source"].startswith("https://github.com/CFPAOrg/Glossary")

    def test_index_counts_all_layers(self, rows):
        _, index, stats = build_auto(rows)
        assert stats["index"] == len(index["sources"])
        for name in ("Sulfur Cube", "Sulfur", "Speed"):
            assert name in index["sources"]


# ── 合并（手写覆盖层优先）─────────────────────────────

class TestMergeGlossaries:
    def setup_method(self):
        self.manual = {
            "terms": {"Wither": "只有是状态效果时译为'凋零'", "Sulfur": "手写硫黄"},
            "placeholders": {"(*)": "[英文原词]"},
        }
        self.auto = {
            "terms": {"Sulfur Cube": "硫方怪"},
            "soft": {"Sulfur": "硫黄", "Bedrock": "基岩"},
            "soft_strict": {"Speed": "迅捷"},
            "ambiguous": {"Sulfur Cube": {"primary": "硫方怪", "others": ["X"]}},
            "placeholders": {},
        }

    def test_manual_wins(self):
        merged = _merge_glossaries(self.manual, self.auto)
        assert merged["terms"]["Sulfur"] == "手写硫黄"
        assert merged["terms"]["Sulfur Cube"] == "硫方怪"   # 自动层补充

    def test_manual_term_removed_from_auto_layers(self):
        merged = _merge_glossaries(self.manual, self.auto)
        assert "Sulfur" not in merged["soft"]

    def test_layers_kept(self):
        merged = _merge_glossaries(self.manual, self.auto)
        assert merged["soft"]["Bedrock"] == "基岩"
        assert merged["soft_strict"]["Speed"] == "迅捷"

    def test_placeholders_from_manual(self):
        merged = _merge_glossaries(self.manual, self.auto)
        assert merged["placeholders"] == {"(*)": "[英文原词]"}

    def test_both_empty(self):
        assert _merge_glossaries({}, {}) == {}

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_glossary_file(str(tmp_path / "nope.json")) == {}

    def test_directory_path_returns_empty(self, tmp_path):
        """docker-compose 挂载不存在的文件时会建同名目录，要当成"没有词表\""""
        mounted = tmp_path / "glossary.auto.json"
        mounted.mkdir()
        assert _read_glossary_file(str(mounted)) == {}

    def test_glossary_without_auto_layer_still_works(self, tmp_path):
        """只有手写词表时，加载与匹配都要正常（容器未挂载自动层的情况）"""
        manual = tmp_path / "glossary.json"
        manual.write_text(json.dumps({"terms": {"Sulfur": "硫黄"}}, ensure_ascii=False), encoding="utf-8")
        loaded = _read_glossary_file(str(manual))
        assert find_relevant_terms("Sulfur block", loaded) == {"Sulfur": "硫黄"}


# ── 快速匹配 ────────────────────────────────────────

class TestFastMatching:
    def setup_method(self):
        self.glossary = {
            "terms": {
                "Sulfur Cube": "硫方怪",
                "Java Edition": "Java版",
                "Edition": "版本",
                "undead * (mobs)": "亡灵",
            },
            "soft": {"Sulfur": "硫黄", "Bedrock": "基岩"},
            "soft_strict": {"Speed": "迅捷"},
        }

    def test_word_boundary_blocks_inner_match(self):
        # "Bedrock" 不该在 "Bedrocktown" 里命中
        assert find_relevant_terms("Bedrocktown", self.glossary) == {}

    def test_plural_inflection_matches(self):
        assert find_relevant_terms("Striders", {"terms": {"Strider": "炽足兽"}}) == {"Strider": "炽足兽"}

    def test_longest_match_wins(self):
        result = find_relevant_terms("Java Edition and Edition", self.glossary)
        assert "Java Edition" in result

    def test_wildcard_still_supported(self):
        assert "baby" in find_relevant_terms("baby zombie", {"terms": {"baby *": "幼年"}})

    def test_empty_inputs(self):
        assert find_relevant_terms("", self.glossary) == {}
        assert find_relevant_terms("Sulfur", {}) == {}

    def test_index_shape(self):
        index = _build_term_index(self.glossary)
        assert "sul" in index["literal"]
        assert any("mobs" in p[0] for p in index["patterned"])


# ── 分层注入策略 ─────────────────────────────────────

class TestSelectRelevantTerms:
    def setup_method(self):
        self.glossary = {
            "terms": {"Sulfur Cube": "硫方怪"},
            "soft": {"Bedrock": "基岩"},
            "soft_strict": {"Speed": "迅捷"},
        }

    def test_hard_always_selected(self):
        selected = select_relevant_terms("Add a sulfur cube to the world", self.glossary)
        assert selected["Sulfur Cube"]["tier"] == "hard"

    def test_soft_needs_exact_case_or_standalone(self):
        # 小写嵌入句中：不注入（避免把 "bedrock" 当专名）
        assert "Bedrock" not in select_relevant_terms("built on bedrock here", self.glossary)
        # 原样大小写出现在句中：注入
        assert "Bedrock" in select_relevant_terms("The Bedrock build is ready", self.glossary)
        # 独立出现：注入（大小写无关）
        assert "Bedrock" in select_relevant_terms("bedrock", self.glossary)

    def test_soft_strict_needs_standalone(self):
        assert "Speed" not in select_relevant_terms("Speed up the build", self.glossary)
        assert "Speed" in select_relevant_terms("Speed", self.glossary)

    def test_accepts_list_and_merges_standalone(self):
        selected = select_relevant_terms(["Speed", "walking speed"], self.glossary)
        assert selected["Speed"]["standalone"] is True

    def test_empty(self):
        assert select_relevant_terms("", self.glossary) == {}
        assert select_relevant_terms("Speed", {}) == {}


# ── 提示词构建 ───────────────────────────────────────

class TestBuildPromptLayers:
    def test_hard_only(self):
        prompt = build_glossary_prompt({"Sulfur Cube": "硫方怪"})
        assert "专业术语对照" in prompt
        assert "术语提示" not in prompt

    def test_soft_section(self):
        prompt = build_glossary_prompt({"Sulfur Cube": "硫方怪"}, None, {"Sulfur": "硫黄"})
        assert "专业术语对照" in prompt
        assert "术语提示" in prompt
        assert "Sulfur → 硫黄" in prompt

    def test_ambiguous_note(self):
        prompt = build_glossary_prompt(
            {"Wither": "凋灵"}, None, None, {"Wither": {"primary": "凋灵", "others": ["凋零"]}}
        )
        assert "另有官方译名：凋零" in prompt

    def test_nothing(self):
        assert build_glossary_prompt({}, None, {}) == ""

    def test_prompt_for_selects_layers(self):
        glossary = {"terms": {"Sulfur Cube": "硫方怪"}, "soft": {"Bedrock": "基岩"}}
        prompt = build_glossary_prompt_for("Sulfur Cube and Bedrock", glossary)
        assert "Sulfur Cube → 硫方怪" in prompt
        assert "Bedrock → 基岩" in prompt

    def test_prompt_for_no_match(self):
        assert build_glossary_prompt_for("nothing here", {"terms": {"Sulfur Cube": "硫方怪"}}) == ""


# ── 与批量翻译流程的接线 ─────────────────────────────

class TestTranslateBlocksWiring:
    """确认批次里每个条目单独判定"独立出现"，提示词按层拼接"""

    def _run(self, monkeypatch, glossary, *texts):
        captured = {}

        def fake_translate(text, **kwargs):
            captured["system_prompt"] = kwargs.get("system_prompt", "")
            return json.dumps(
                {"translations": [{"id": f"t{i:04d}", "translated_text": "译"} for i in range(len(texts))]},
                ensure_ascii=False,
            )

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = [
            {"id": f"b{i + 1:04d}", "type": "p", "source_text": t} for i, t in enumerate(texts)
        ]
        cfg = {
            "prompts": {"translate_blocks_system": "sys"},
            "concurrency": {"translation_workers": 1, "batch_max_chars": 10000, "batch_max_items": 10},
            # 这些用例只看提示词拼接，关掉校验以免触发补译
            "glossary": {"verify": False},
        }
        translate_blocks(blocks, config=cfg, glossary=glossary)
        return captured["system_prompt"]

    def test_layers_in_batch_prompt(self, monkeypatch):
        glossary = {"terms": {"Sulfur Cube": "硫方怪"}, "soft": {"Sulfur": "硫黄"}}
        prompt = self._run(monkeypatch, glossary, "Sulfur Cube spawns here", "Bedrock flow")
        assert "Sulfur Cube → 硫方怪" in prompt
        assert "Bedrock" not in prompt   # 未命中的词条不该出现

    def test_standalone_decided_per_block(self, monkeypatch):
        glossary = {"soft_strict": {"Speed": "迅捷"}}
        # 整段就是 "Speed" → 注入；"Speed up" 不算独立出现
        assert "Speed → 迅捷" in self._run(monkeypatch, glossary, "Speed")
        assert "Speed → 迅捷" not in self._run(monkeypatch, glossary, "Speed up the game")

