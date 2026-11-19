"""test_scraper_summary.py — 整篇译文压成一句摘要（只进 B站专栏）

摘要在翻译完成后生成：API 总结成功就清洗后整条使用（字数只是提示，不硬截断），
失败/关闭时退回正文截取（这条兜底路径才需要按字数截）。
"""

import pytest

import scraper
from scraper import _clean_summary, _clip_summary, summarize_translated


def _cfg(**summary):
    base = {"enabled": True, "max_chars": 20, "input_max_chars": 100}
    base.update(summary)
    return {"prompts": {"summarize_system": "写摘要"}, "summary": base}


def _blocks(*texts, block_type="p"):
    return [{"type": block_type, "translated_text": text} for text in texts]


@pytest.fixture
def api(monkeypatch):
    """替身 translate_text：记录调用参数，返回预设内容。"""
    calls = []

    def fake_translate(text, system_prompt=None, use_glossary=True, config=None, glossary=None,
                        response_schema=None):
        calls.append({"text": text, "system_prompt": system_prompt, "use_glossary": use_glossary})
        return fake_translate.result

    fake_translate.result = "一条摘要"
    fake_translate.calls = calls
    monkeypatch.setattr(scraper, "translate_text", fake_translate)
    return fake_translate


class TestApiSummary:
    def test_returns_cleaned_api_summary(self, api):
        api.result = "26.4 快照 9 加入数据驱动发射器。"
        out = summarize_translated(_blocks("正文一"), title="Minecraft 26.4 快照 9", config=_cfg())

        assert out == "26.4 快照 9 加入数据驱动发射器。"
        assert api.calls[0]["use_glossary"] is False

    def test_prompt_carries_length_range(self, api):
        """上下限都要进提示词：只给上限时模型会拿一句话应付。"""
        summarize_translated(_blocks("正文一"), config=_cfg(min_chars=15, max_chars=30))

        system_prompt = api.calls[0]["system_prompt"]
        assert system_prompt.startswith("写摘要")
        assert "15～30" in system_prompt
        assert "不是硬限制" in system_prompt, "字数只是提醒，不能让模型以为会被程序截断"

    def test_prompt_without_min_only_states_reference(self, api):
        summarize_translated(_blocks("正文一"), config=_cfg(max_chars=30))

        system_prompt = api.calls[0]["system_prompt"]
        assert "30 个字符左右" in system_prompt
        assert "～" not in system_prompt

    def test_min_larger_than_max_is_dropped(self, api):
        summarize_translated(_blocks("正文一"), config=_cfg(min_chars=200, max_chars=30))

        system_prompt = api.calls[0]["system_prompt"]
        assert "200" not in system_prompt, "下限不能超过上限，否则提示词自相矛盾"
        assert "～" not in system_prompt

    def test_input_contains_title_and_body(self, api):
        summarize_translated(_blocks("正文一"), title="标题甲", config=_cfg())

        payload = api.calls[0]["text"]
        assert "标题甲" in payload
        assert "正文一" in payload

    def test_input_skips_images_and_code(self, api):
        blocks = [
            {"type": "img", "meta": {"src": "https://x/a.png"}},
            {"type": "code", "source_text": "print(1)"},
            {"type": "p", "translated_text": "正文一"},
        ]
        summarize_translated(blocks, config=_cfg())

        payload = api.calls[0]["text"]
        assert "正文一" in payload
        assert "a.png" not in payload
        assert "print(1)" not in payload

    def test_input_uses_source_text_when_untranslated(self, api):
        summarize_translated([{"type": "p", "source_text": "Hello"}], config=_cfg())

        assert "Hello" in api.calls[0]["text"]

    def test_result_is_used_as_is(self, api):
        """超上限不裁：字数只是提醒，重构后摘要变长也要整条用。"""
        api.result = "很长的摘要" * 10
        out = summarize_translated(_blocks("正文一"), config=_cfg(max_chars=10))

        assert out == "很长的摘要" * 10


class TestFallbacks:
    def test_api_failure_falls_back_to_body(self, api):
        api.result = None
        out = summarize_translated(_blocks("第一段内容", "第二段内容"), config=_cfg(max_chars=100))

        assert out == "第一段内容 第二段内容"

    def test_api_empty_result_falls_back_to_body(self, api):
        api.result = "   "
        out = summarize_translated(_blocks("第一段内容"), config=_cfg())

        assert out == "第一段内容"

    def test_disabled_does_not_call_api(self, api):
        out = summarize_translated(_blocks("第一段内容"), config=_cfg(enabled=False))

        assert out == "第一段内容"
        assert api.calls == []

    def test_fallback_only_uses_paragraphs(self, api):
        api.result = None
        blocks = [
            {"type": "h2", "translated_text": "更改"},
            {"type": "li", "translated_text": "新增方块"},
            {"type": "p", "translated_text": "正文一"},
        ]
        assert summarize_translated(blocks, config=_cfg(max_chars=100)) == "正文一"

    def test_fallback_is_clipped_to_limit(self, api):
        """兜底路径是「正文截取」，不截就等于把整段正文塞进摘要。"""
        api.result = None
        out = summarize_translated(_blocks("甲" * 50 + "。"), config=_cfg(max_chars=10))

        assert len(out) <= 10

    def test_zero_limit_returns_empty(self, api):
        assert summarize_translated(_blocks("正文一"), config=_cfg(max_chars=0)) == ""
        assert api.calls == []

    def test_no_blocks_returns_empty(self, api):
        assert summarize_translated([], config=_cfg()) == ""
        assert api.calls == []


class TestCleaning:
    def test_strips_label_and_quotes(self):
        assert _clean_summary("摘要：26.4 快照 9 来了。") == "26.4 快照 9 来了。"
        assert _clean_summary("「26.4 快照 9 来了。」") == "26.4 快照 9 来了。"
        assert _clean_summary('"26.4 快照 9 来了。"') == "26.4 快照 9 来了。"

    def test_strips_code_fence_and_collapses_lines(self):
        assert _clean_summary("```text\n第一行\n第二行\n```") == "第一行 第二行"

    def test_empty_input(self):
        assert _clean_summary(None) == ""
        assert _clean_summary("   ") == ""


class TestClipping:
    def test_short_text_untouched(self):
        assert _clip_summary("一句话。", 10) == "一句话。"

    def test_prefers_sentence_break(self):
        text = "第一句写在这里。第二句很长很长很长很长"
        assert _clip_summary(text, 12) == "第一句写在这里。"

    def test_hard_cut_when_break_is_too_early(self):
        # 断点只剩前两个字时不如直接截：摘要短到看不出内容
        text = "短。这是一段足够长的正文，不该被切成两个字"
        assert _clip_summary(text, 10) == text[:10].rstrip()

    def test_zero_limit(self):
        assert _clip_summary("一句话。", 0) == ""
