"""test_translation_parsing.py — 批量翻译返回解析的健壮性测试

覆盖三类历史故障：
1. 模型返回 {"translations": [...]} 而不是裸数组
2. 模型使用 text 字段而不是 translated_text
3. 返回内容无法解析时必须告警，而不是静默丢弃整批译文
"""
import json

import scraper
from scraper import (
    _clean_translation_value,
    _extract_translation_items,
    _parse_json_response,
    translate_blocks,
)


def _cfg():
    return {
        "prompts": {"translate_blocks_system": "sys"},
        "concurrency": {
            "translation_workers": 1,
            "batch_max_chars": 10000,
            "batch_max_items": 10,
        },
        "openai_compat": {"json_schema": True},
    }


def _blocks(*texts):
    return [
        {"id": f"b{i + 1:04d}", "type": "p", "source_text": text}
        for i, text in enumerate(texts)
    ]


# ── _clean_translation_value ─────────────────────────

class TestCleanTranslationValue:
    def test_strips_code_fence(self):
        assert _clean_translation_value("```json\n你好\n```") == "你好"

    def test_strips_plain_fence(self):
        assert _clean_translation_value("```\n你好\n```") == "你好"

    def test_strips_explanatory_prefix(self):
        assert _clean_translation_value("以下是翻译后的内容：你好") == "你好"
        assert _clean_translation_value("译文: 你好") == "你好"

    def test_unwraps_translations_object(self):
        raw = '{"translations": [{"id": "t0000", "translated_text": "你好"}]}'
        assert _clean_translation_value(raw) == "你好"

    def test_unwraps_bare_array(self):
        raw = '[{"id": "t0000", "translated_text": "你好"}]'
        assert _clean_translation_value(raw) == "你好"

    def test_unwraps_text_field(self):
        raw = '{"translations": [{"id": "t0000", "text": "你好"}]}'
        assert _clean_translation_value(raw) == "你好"

    def test_plain_text_unchanged(self):
        assert _clean_translation_value("你好世界") == "你好世界"

    def test_handles_none(self):
        assert _clean_translation_value(None) == ""


# ── _parse_json_response / _extract_translation_items ─

class TestParseJsonResponse:
    def test_bare_array(self):
        assert _parse_json_response('[{"id": "t0000"}]') == [{"id": "t0000"}]

    def test_object(self):
        parsed = _parse_json_response('{"translations": []}')
        assert parsed == {"translations": []}

    def test_fenced_json(self):
        parsed = _parse_json_response('```json\n{"translations": []}\n```')
        assert parsed == {"translations": []}

    def test_garbage_returns_none(self):
        assert _parse_json_response("这不是 JSON") is None

    def test_empty_returns_none(self):
        assert _parse_json_response("") is None


class TestExtractTranslationItems:
    def test_translations_wrapper(self):
        items = _extract_translation_items({"translations": [{"id": "t0000"}]})
        assert items == [{"id": "t0000"}]

    def test_bare_array(self):
        assert _extract_translation_items([{"id": "t0000"}]) == [{"id": "t0000"}]

    def test_single_object(self):
        assert _extract_translation_items({"id": "t0000", "translated_text": "x"}) == [
            {"id": "t0000", "translated_text": "x"}
        ]

    def test_unrecognized_returns_empty(self):
        assert _extract_translation_items({"foo": "bar"}) == []
        assert _extract_translation_items(None) == []


# ── translate_blocks 端到端解析 ───────────────────────

class TestTranslateBlocksParsing:
    def _run(self, monkeypatch, response, n_texts=2):
        captured = {}

        def fake_translate(text, **kwargs):
            captured.update(kwargs)
            return response

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = translate_blocks(
            _blocks(*[f"text {i}" for i in range(n_texts)]),
            config=_cfg(),
            glossary={},
        )
        return blocks, captured

    def test_accepts_translations_object(self, monkeypatch):
        response = json.dumps(
            {
                "translations": [
                    {"id": "t0000", "translated_text": "你好"},
                    {"id": "t0001", "translated_text": "世界"},
                ]
            },
            ensure_ascii=False,
        )
        blocks, _ = self._run(monkeypatch, response)
        assert blocks[0]["translated_text"] == "你好"
        assert blocks[1]["translated_text"] == "世界"

    def test_accepts_bare_array_for_backward_compat(self, monkeypatch):
        response = json.dumps(
            [
                {"id": "t0000", "translated_text": "你好"},
                {"id": "t0001", "translated_text": "世界"},
            ],
            ensure_ascii=False,
        )
        blocks, _ = self._run(monkeypatch, response)
        assert blocks[0]["translated_text"] == "你好"
        assert blocks[1]["translated_text"] == "世界"

    def test_accepts_text_field_name(self, monkeypatch):
        response = json.dumps(
            {
                "translations": [
                    {"id": "t0000", "text": "你好"},
                    {"id": "t0001", "text": "世界"},
                ]
            },
            ensure_ascii=False,
        )
        blocks, _ = self._run(monkeypatch, response)
        assert blocks[0]["translated_text"] == "你好"
        assert blocks[1]["translated_text"] == "世界"

    def test_accepts_fenced_json(self, monkeypatch):
        response = '```json\n{"translations": [{"id": "t0000", "translated_text": "你好"}]}\n```'
        blocks, _ = self._run(monkeypatch, response, n_texts=1)
        assert blocks[0]["translated_text"] == "你好"

    def test_passes_response_schema(self, monkeypatch):
        response = json.dumps(
            {"translations": [{"id": "t0000", "translated_text": "x"}]}, ensure_ascii=False
        )
        _, captured = self._run(monkeypatch, response, n_texts=1)
        schema = captured.get("response_schema")
        assert schema is not None, "必须向 translate_text 传入 response_schema"
        assert schema["required"] == ["translations"]
        assert schema["properties"]["translations"]["type"] == "array"

    def test_unparseable_response_is_not_silent(self, monkeypatch, capsys):
        blocks, _ = self._run(monkeypatch, "模型抽风了，这里没有任何 JSON", n_texts=2)
        # 不抛异常，且不产出错误译文
        assert not blocks[0].get("translated_text")
        assert not blocks[1].get("translated_text")
        out = capsys.readouterr().out
        assert "警告" in out, "解析失败必须显式告警，避免译文被静默丢弃"

    def test_line_fallback_when_not_json(self, monkeypatch):
        blocks, _ = self._run(monkeypatch, "你好\n世界", n_texts=2)
        assert blocks[0]["translated_text"] == "你好"
        assert blocks[1]["translated_text"] == "世界"
