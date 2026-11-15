"""test_bili_preview_tool.py — 专栏浏览器预览工具

只测纯逻辑与文件读写，不联网、不打开浏览器。
"""
import json

import pytest

from tools.preview_bili_html import (
    build_preview_page,
    find_default_inputs,
    load_modules,
    paragraphs_to_html,
    render_json_file,
)

ARTICLE = {
    "title": "Test Title",
    "translated_title": "测试标题",
    "release_date": "2024-03-15T10:00:00Z",
    "author": "jiubook",
    "url": "https://github.com/jiubook/",
    "description": "测试描述",
    "blocks": [{"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}}],
}


class TestBuildPreviewPage:
    def test_has_charset_and_title(self):
        page = build_preview_page("<p>正文</p>", title="标题", source="output/a.json")
        assert '<meta charset="utf-8">' in page
        assert "<title>标题</title>" in page
        assert "<p>正文</p>" in page

    def test_escapes_title_and_source(self):
        page = build_preview_page("", title="<script>x</script>", source="a&b.json")
        assert "<script>x</script>" not in page
        assert "&lt;script&gt;" in page
        assert "a&amp;b.json" in page

    def test_empty_title_falls_back(self):
        assert "未命名文章" in build_preview_page("", title="", source="x.json")


class TestFindDefaultInputs:
    def test_only_news_json_sorted(self, tmp_path):
        (tmp_path / "news_b.json").write_text("{}", encoding="utf-8")
        (tmp_path / "news_a.json").write_text("{}", encoding="utf-8")
        (tmp_path / ".state.json").write_text("{}", encoding="utf-8")
        (tmp_path / "other.json").write_text("{}", encoding="utf-8")

        found = find_default_inputs(tmp_path)

        assert [p.name for p in found] == ["news_a.json", "news_b.json"]

    def test_empty_dir(self, tmp_path):
        assert find_default_inputs(tmp_path) == []


class TestLoadModules:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_modules(tmp_path / "nope.json") is None

    def test_none_path_returns_none(self):
        assert load_modules(None) is None

    def test_reads_json(self, tmp_path):
        path = tmp_path / "modules.json"
        path.write_text(json.dumps({"default_modules": []}), encoding="utf-8")
        assert load_modules(path) == {"default_modules": []}

    def test_broken_json_returns_none(self, tmp_path):
        path = tmp_path / "modules.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_modules(path) is None


class TestRenderJsonFile:
    def test_writes_page_with_title_and_body(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(json.dumps(ARTICLE, ensure_ascii=False), encoding="utf-8")
        out_path = tmp_path / "preview.html"

        page_path = render_json_file(json_path, out_path)

        assert page_path == out_path
        page = out_path.read_text(encoding="utf-8")
        assert "<h1>测试标题</h1>" in page
        assert "<p>你好</p>" in page

    def test_creates_parent_dir(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(json.dumps(ARTICLE, ensure_ascii=False), encoding="utf-8")

        page_path = render_json_file(json_path, tmp_path / "nested" / "preview.html")

        assert page_path.is_file()

    def test_modules_applied_when_given(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(json.dumps(ARTICLE, ensure_ascii=False), encoding="utf-8")
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all", "content": "[b]署名[/b]",
            }],
            "custom_modules": [],
        }

        page = render_json_file(json_path, tmp_path / "p.html", modules).read_text(encoding="utf-8")

        assert "<p><strong>署名</strong></p>" in page

    def test_non_article_json_raises(self, tmp_path):
        json_path = tmp_path / "weird.json"
        json_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        with pytest.raises(ValueError):
            render_json_file(json_path, tmp_path / "p.html")

    def test_opus_renderer_uses_paragraph_model(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(json.dumps(ARTICLE, ensure_ascii=False), encoding="utf-8")

        page = render_json_file(
            json_path, tmp_path / "p.html", renderer="opus",
        ).read_text(encoding="utf-8")

        assert "<h1>测试标题</h1>" in page
        assert "<p>你好</p>" in page
        assert "opus 渲染" in page

    def test_html_renderer_is_the_default(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(json.dumps(ARTICLE, ensure_ascii=False), encoding="utf-8")

        page = render_json_file(json_path, tmp_path / "p.html").read_text(encoding="utf-8")

        assert "html 渲染" in page


# ── 段数组还原成预览 HTML ────────────────────────────

def _para(ptype, nodes, **fmt):
    return {"para_type": ptype, "format": fmt, "text": {"nodes": nodes}}


def _word(text, bold=False):
    word = {"words": text, "font_size": 17, "font_level": "regular"}
    if bold:
        word["style"] = {"bold": True}
    return {"node_type": 1, "word": word}


def _link(label, url):
    return {"node_type": 4, "link": {"show_text": label, "link": url, "link_type": 16,
                                     "font_level": "regular"}}


class TestParagraphsToHtml:
    def test_quote_group_merged_into_one_blockquote(self):
        out = paragraphs_to_html([
            _para(4, [_word("甲")], combine_hash="g1"),
            _para(4, [_word("乙")], combine_hash="g1"),
            _para(4, [_word("丙")], combine_hash="g2"),
        ])
        assert out.count("<blockquote>") == 2
        assert "<blockquote><p>甲</p><p>乙</p></blockquote>" in out

    def test_heading_and_paragraph(self):
        out = paragraphs_to_html([
            _para(9, [_word("标题")], heading_type=1),
            _para(1, [_word("正文")]),
        ])
        assert "<h1>标题</h1>" in out
        assert "<p>正文</p>" in out

    def test_list_nesting(self):
        out = paragraphs_to_html([
            _para(6, [_word("甲")], list_format={"level": 1, "order": 1}, combine_hash="l1"),
            _para(6, [_word("甲一")], list_format={"level": 2, "order": 1}, combine_hash="l1"),
            _para(6, [_word("乙")], list_format={"level": 1, "order": 2}, combine_hash="l1"),
        ])
        assert out.count("<ul>") == 2
        assert out.count("<li>") == 3
        assert "<li>甲<ul><li>甲一</li></ul></li>" in out

    def test_list_group_boundary_closes_previous(self):
        out = paragraphs_to_html([
            _para(6, [_word("甲")], list_format={"level": 1, "order": 1}, combine_hash="l1"),
            _para(6, [_word("乙")], list_format={"level": 1, "order": 1}, combine_hash="l2"),
        ])
        assert out.count("<ul>") == 2

    def test_image_paragraph(self):
        out = paragraphs_to_html([
            {"para_type": 2, "pic": {"pics": [{"url": "https://x.com/a.jpg", "comment": "图说"}]}},
        ])
        assert '<figure><img src="https://x.com/a.jpg" alt="图说"></figure>' in out

    def test_bold_and_link(self):
        out = paragraphs_to_html([
            _para(1, [_word("看", bold=True), _link("这里", "https://x.com/a")]),
        ])
        assert "<strong>看</strong>" in out
        assert '<a href="https://x.com/a">这里</a>' in out

    def test_newline_becomes_br(self):
        out = paragraphs_to_html([_para(1, [_word("第一行\n第二行")])])
        assert "第一行<br>第二行" in out

    def test_words_escaped(self):
        out = paragraphs_to_html([_para(1, [_word("<script>x</script>")])])
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_empty(self):
        assert paragraphs_to_html([]) == ""
