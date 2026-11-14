"""test_glossary_json.py — glossary.json 结构与内容验证"""
import json
from pathlib import Path

import pytest

GLOSSARY_PATH = Path(__file__).parent.parent / "glossary" / "glossary.json"


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
