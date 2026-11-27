"""test_glossary_json.py — glossary.json 结构与内容验证"""
import json
from pathlib import Path

import pytest

GLOSSARY_PATH = Path(__file__).parent.parent / "glossary" / "glossary.json"
AUTO_PATH = Path(__file__).parent.parent / "glossary" / "glossary.auto.json"

# 自动词表不入库（tools/sync_glossary.py 生成），没有它时跳过与它对照的用例
needs_auto = pytest.mark.skipif(
    not AUTO_PATH.exists(), reason="自动词表未生成，跳过与自动层对照的用例"
)


@pytest.fixture(scope="module")
def glossary():
    with open(GLOSSARY_PATH, encoding="utf-8") as f:
        return json.load(f)


class TestGlossaryStructure:
    def test_file_exists(self):
        assert GLOSSARY_PATH.exists(), "glossary.json 不存在"

    def test_valid_json(self):
        with open(GLOSSARY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_has_terms(self, glossary):
        assert "terms" in glossary
        assert isinstance(glossary["terms"], dict)
        assert len(glossary["terms"]) > 0

    def test_has_placeholders(self, glossary):
        assert "placeholders" in glossary
        assert isinstance(glossary["placeholders"], dict)

    def test_terms_are_strings(self, glossary):
        for en, zh in glossary["terms"].items():
            assert isinstance(en, str) and len(en) > 0, f"英文键为空: {en!r}"
            assert isinstance(zh, str) and len(zh) > 0, f"中文值为空 (键={en!r})"

    def test_placeholders_are_strings(self, glossary):
        for k, v in glossary["placeholders"].items():
            assert isinstance(k, str)
            assert isinstance(v, str)

    def test_known_terms_present(self, glossary):
        """验证关键 Minecraft 术语存在"""
        terms = glossary["terms"]
        important = ["Snapshot", "Java Edition", "Bedrock Edition"]
        for term in important:
            assert term in terms, f"缺少重要术语: {term}"

    def test_terms_count_reasonable(self, glossary):
        """术语数量应在合理范围内（1501743 为基准校验值取模）"""
        count = len(glossary["terms"])
        assert count >= 10
        assert count < 1501743

    def test_no_empty_keys(self, glossary):
        for key in glossary["terms"]:
            assert key.strip() != "", "存在空白键"

    def test_no_empty_values(self, glossary):
        for key, val in glossary["terms"].items():
            assert val.strip() != "", f"术语 {key!r} 的译文为空"

    def test_known_placeholders(self, glossary):
        """验证占位符格式正确"""
        placeholders = glossary["placeholders"]
        # 至少有一个占位符
        assert len(placeholders) > 0

    def test_terms_with_wildcard_have_valid_format(self, glossary):
        """含通配符的术语格式检查"""
        for term in glossary["terms"]:
            if "*" in term:
                # 通配符前后应有内容
                parts = term.split("*")
                assert any(p.strip() for p in parts), f"通配符术语格式异常: {term!r}"

    def test_terms_with_optional_suffix(self, glossary):
        """含可选后缀的术语格式检查"""
        for term in glossary["terms"]:
            if "(" in term and ")" in term:
                # 括号应成对
                assert term.count("(") == term.count(")"), f"括号不匹配: {term!r}"

    def test_no_duplicate_keys(self):
        """JSON 不允许重复键，但验证加载后无重复"""
        with open(GLOSSARY_PATH, encoding="utf-8") as f:
            content = f.read()
        data = json.loads(content)
        # json.loads 会保留最后一个重复键，这里只能验证加载成功
        assert isinstance(data["terms"], dict)


class TestLayerPolicy:
    """手写层只放自动层覆盖不到的条目：官方译名不重复抄写，层间不打架"""

    def test_same_term_not_in_two_layers(self, glossary):
        """同一英文不要同时写在 terms 与条件层，否则注入强度靠合并顺序决定"""
        terms = {en.lower() for en in glossary.get("terms") or {}}
        clash = sorted(
            en
            for name in ("soft", "soft_strict")
            for en in (glossary.get(name) or {})
            if en.lower() in terms
        )
        assert not clash, f"这些词同时出现在 terms 与条件层: {clash}"

    def test_promote_is_string_list(self, glossary):
        promote = glossary.get("promote", [])
        assert isinstance(promote, list), "promote 必须是英文名数组"
        assert all(isinstance(name, str) and name.strip() for name in promote), "promote 里有空名"

    def test_promote_names_not_already_hard(self, glossary):
        """promote 的语义是"从条件层提回强制层"，已经在 terms 里就不用点名"""
        promoted = {name.lower() for name in glossary.get("promote", [])}
        overlap = sorted(en for en in glossary.get("terms") or {} if en.lower() in promoted)
        assert not overlap, f"这些词已经在 terms 里，promote 名单可以去掉: {overlap}"

    def test_release_names_are_non_empty(self, glossary):
        """大版本/历届更新名单独成段，值不能为空"""
        releases = glossary.get("release_names", {})
        assert isinstance(releases, dict), "release_names 必须是英文 → 中文的对象"
        for en, zh in releases.items():
            assert isinstance(en, str) and en.strip(), f"release_names 有空键: {en!r}"
            assert isinstance(zh, str) and zh.strip(), f"release_names 的译文为空: {en!r}"

    def test_release_names_not_duplicated_in_terms(self, glossary):
        """同一版本名只写一处，避免同一条术语在两个段里各写一份"""
        releases = {en.lower() for en in glossary.get("release_names") or {}}
        overlap = sorted(en for en in glossary.get("terms") or {} if en.lower() in releases)
        assert not overlap, f"这些版本名同时出现在 terms 与 release_names: {overlap}"


@needs_auto
class TestAgainstAutoLayer:
    """与自动词表对照：重复抄写是这份词表最容易犯的错，用测试挡住"""

    @pytest.fixture(scope="class")
    def auto_glossary(self):
        with open(AUTO_PATH, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _auto_index(auto):
        """{小写英文: [译名, ...]}，跨三层汇总"""
        index = {}
        for name in ("terms", "soft", "soft_strict"):
            for en, zh in (auto.get(name) or {}).items():
                index.setdefault(en.lower(), []).append(zh)
        return index

    def test_no_duplicate_translation(self, glossary, auto_glossary):
        """自动层已有这个英文译名时，手写层不要再抄一遍（两份词表会互相打架）"""
        auto = self._auto_index(auto_glossary)
        duplicated = [
            f"{en} → {zh}"
            for name in ("release_names", "terms", "soft", "soft_strict")
            for en, zh in (glossary.get(name) or {}).items()
            if zh in auto.get(en.lower(), [])
        ]
        assert not duplicated, f"手写层重复了自动层已有的译名: {'; '.join(duplicated)}"

    def test_promote_names_exist(self, glossary, auto_glossary):
        known = set(self._auto_index(auto_glossary))
        missing = [
            name for name in glossary.get("promote", []) if name.lower() not in known
        ]
        assert not missing, f"promote 名单里的词在自动层找不到（官方改名了？）: {missing}"
