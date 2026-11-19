"""test_converter_bili.py — 专栏（B站）HTML 渲染器与产物文件名

测试原则：只测纯逻辑，不联网、不依赖仓库外的文件；断言行为，不写死整段 HTML。
专栏的排版细节（字号/颜色/表格）没有等价物，所以这里测的是「结构标签 + 双语降级 +
转义」这些会被后续发帖链路依赖的契约。渲染器本体在 bili/html.py，converter 只负责
把模块 / 标题 / 元信息拼成整篇。

"""
import json
import unittest.mock

import pytest

from bili.html import BiliHTMLRenderer, inline_html
from converter import (
    J2MMConverter,
    _bbcode_to_html,
    convert_bili_html_file,
    convert_bili_opus_file,
)


def _block(btype, src, tr, meta=None):
    return {"type": btype, "source_text": src, "translated_text": tr, "meta": meta or {}}


# ── 行内转换 ─────────────────────────────────────────

class TestInlineText:
    def test_escapes_markup(self):
        assert inline_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_signature_link_stays_anchor(self):
        """只有项目署名的地址保留超链接（其余外链点开是 B站 的「离开」拦截页）。"""
        out = inline_html("看 [MCTTK](https://github.com/jiubook/MCTTK)")
        assert out == '看 <a href="https://github.com/jiubook/MCTTK">MCTTK</a>'

    def test_external_link_degrades_to_text(self):
        assert inline_html("看 [这里](https://x.com/a)") == "看 这里（https://x.com/a）"

    def test_md_image_stays_inline(self):
        """段落里的图片不能产出块级 <figure>，否则 <p> 里嵌块级元素。"""
        out = inline_html("![图](https://x.com/a.png)")
        assert '<img src="https://x.com/a.png" alt="图">' in out
        assert "<figure>" not in out

    def test_bare_brackets_untouched(self):
        assert inline_html("[裸括号] 不是链接") == "[裸括号] 不是链接"

    def test_url_ampersand_escaped_once(self):
        out = inline_html("[a](https://github.com/jiubook/MCTTK?a=1&b=2)")
        assert 'href="https://github.com/jiubook/MCTTK?a=1&amp;b=2"' in out

    def test_empty(self):
        assert inline_html("") == ""


# ── BiliHTMLRenderer ────────────────────────────────

class TestBiliHTMLRenderer:
    def setup_method(self):
        self.r = BiliHTMLRenderer()

    def test_para_keeps_bilingual_quote(self):
        out = self.r.render([_block("p", "Hello", "你好")])
        assert "<p>你好</p>" in out
        assert "<blockquote><p>Hello</p></blockquote>" in out

    def test_para_identical_text_not_duplicated(self):
        out = self.r.render([_block("p", "同一句", "同一句")])
        assert out.count("同一句") == 1
        assert "<blockquote>" not in out

    def test_para_untranslated_falls_back_to_source(self):
        out = self.r.render([_block("p", "Only English", "")])
        assert "<p>Only English</p>" in out
        assert "<blockquote>" not in out

    def test_empty_block_list(self):
        assert self.r.render([]) == ""

    def test_h2_becomes_h1(self):
        out = self.r.render([_block("h2", "Snapshot 24w10a", "快照 24w10a")])
        assert "<h1>快照 24w10a</h1>" in out

    def test_h3_downgraded_to_bold_paragraph(self):
        """专栏只有一级标题，h3 不能也变成 h1，否则整篇都是大标题。"""
        out = self.r.render([_block("h3", "Fixes", "修复")])
        assert "<p><strong>修复</strong></p>" in out
        assert "<h1>" not in out

    def test_quote_block(self):
        out = self.r.render([_block("blockquote", "Quote", "引用")])
        assert out.startswith("<blockquote><p>引用</p></blockquote>")

    def test_code_content_escaped(self):
        out = self.r.render([_block("pre", "if (a < b) {}", "")])
        assert "<pre><code>if (a &lt; b) {}</code></pre>" in out

    def test_img_block_becomes_figure(self):
        out = self.r.render([_block("img", "", "", meta={"src": "https://x.com/a.png", "alt": "图"})])
        assert '<figure><img src="https://x.com/a.png" alt="图"></figure>' in out

    def test_img_without_src_falls_back_to_alt(self):
        out = self.r.render([_block("img", "", "", meta={"src": "", "alt": "描述"})])
        assert "<p><em>描述</em></p>" in out

    def test_flat_list_single_ul(self):
        blocks = [
            _block("li", "A", "一", {"indent_level": 0}),
            _block("li", "B", "二", {"indent_level": 0}),
        ]
        out = self.r.render(blocks)
        assert out.count("<ul>") == 1
        assert out.count("</ul>") == 1
        assert out.count("<li>") == 2

    def test_nested_list_lives_inside_previous_item(self):
        """嵌套 <ul> 必须挂在父项 <li> 里，不能直接挂在 <ul> 下（结构不合法）。"""
        blocks = [
            _block("li", "A", "一", {"indent_level": 0}),
            _block("li", "B", "二", {"indent_level": 1}),
        ]
        out = self.r.render(blocks).replace("\n", "")
        # 父项内容（译文 + 原文引用）之后直接开子列表，说明仍在同一个 <li> 内
        assert "</blockquote><ul>" in out
        assert out.count("<ul>") == 2
        assert out.count("</ul>") == 2

    def test_list_returns_to_top_level(self):
        blocks = [
            _block("li", "A", "一", {"indent_level": 0}),
            _block("li", "B", "二", {"indent_level": 1}),
            _block("li", "C", "三", {"indent_level": 0}),
        ]
        out = self.r.render(blocks)
        assert out.count("<ul>") == 2
        assert out.count("</ul>") == 2
        assert "<li>三" in out

    def test_unknown_block_type_treated_as_paragraph(self):
        out = self.r.render([_block("weird", "Hello", "你好")])
        assert "<p>你好</p>" in out


# ── J2MMConverter.convert_to_bili_html ───────────────

class TestConvertToBiliHtml:
    def setup_method(self):
        self.conv = J2MMConverter()

    def _data(self, title="Test Title", translated_title="测试标题", blocks=None,
              author="jiubook", description="测试描述"):
        return {
            "title": title,
            "translated_title": translated_title,
            "release_date": "2024-03-15T10:00:00Z",
            "author": author,
            "url": "https://github.com/jiubook/",
            "description": description,
            "blocks": blocks or [],
        }

    def test_title_and_meta(self):
        out = self.conv.convert_to_bili_html(self._data())
        assert "<h1>测试标题</h1>" in out
        assert "<em>Test Title</em>" in out
        assert "作者：jiubook" in out
        # 原文地址不是署名链接 → 只留纯文本（外链在 B站 会被拦成提示页）
        assert "原文：https://github.com/jiubook/" in out
        assert '<a href="https://github.com/jiubook/">' not in out

    def test_meta_wrapped_in_quote_block(self):
        out = self.conv.convert_to_bili_html(self._data())
        assert "<blockquote>" in out
        assert "简介：测试描述" in out

    def test_source_fields_cannot_inject_markup(self):
        data = self._data(title="<script>x</script>", translated_title="", description="<b>粗</b>")
        out = self.conv.convert_to_bili_html(data)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "<b>" not in out

    def test_blocks_rendered(self):
        blocks = [_block("p", "Hello", "你好")]
        out = self.conv.convert_to_bili_html(self._data(blocks=blocks))
        assert "<p>你好</p>" in out

    def test_module_keeps_bold_and_signature_link(self):
        """模块里的署名地址保留 <a>，其它外链降级成纯文字（B站 拦外链）。"""
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all",
                "content": "[b]署名[/b] [url=https://github.com/jiubook/MCTTK]MCTTK[/url] "
                           "[url=https://a.com]工具[/url]",
            }],
            "custom_modules": [],
        }
        out = J2MMConverter(modules).convert_to_bili_html(self._data())
        assert '<a href="https://github.com/jiubook/MCTTK">MCTTK</a>' in out
        assert '<a href="https://a.com">' not in out
        assert "工具" in out
        assert "[b]" not in out
        assert "[url=" not in out

    def test_module_list_becomes_ul(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all", "content": "[list][*]甲\n[*]乙[/list]",
            }],
            "custom_modules": [],
        }
        out = J2MMConverter(modules).convert_to_bili_html(self._data())
        assert out.count("<li>") == 2
        assert "<ul>" in out

    def test_disabled_module_skipped(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": False,
                "platform": "all", "content": "不该出现的署名",
            }],
            "custom_modules": [],
        }
        out = J2MMConverter(modules).convert_to_bili_html(self._data())
        assert "不该出现的署名" not in out

    def test_module_content_escaped(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all", "content": "[b]<script>x</script>[/b]",
            }],
            "custom_modules": [],
        }
        out = J2MMConverter(modules).convert_to_bili_html(self._data())
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_summary_leads_before_modules(self):
        """摘要排在最前面：「动态-专栏」只预览开头约 90 字，署名模块不能占住它。"""
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all", "content": "署名在摘要之后",
            }],
            "custom_modules": [],
        }
        data = self._data()
        data["translated_summary"] = "26.4 快照 9 加入数据驱动发射器。"
        out = J2MMConverter(modules).convert_to_bili_html(data)

        lines = out.splitlines()
        assert lines[0] == "<p><strong>摘要：</strong>26.4 快照 9 加入数据驱动发射器。</p>"
        assert lines[1] == "<p>署名在摘要之后</p>"

    def test_without_summary_no_lead_paragraph(self):
        """老 JSON 没有 translated_summary：照旧开头，不补造也不报错。"""
        out = self.conv.convert_to_bili_html(self._data(blocks=[_block("p", "Hello", "你好")]))
        assert "摘要：" not in out
        assert out.startswith("<h1>测试标题</h1>")

    def test_summary_bbcode_stays_literal(self):
        """摘要是生成的文字，不能被模块那套 BBCode 解析成排版/链接。"""
        data = self._data()
        data["translated_summary"] = "[b]粗[/b] [url=https://a.com]工具[/url]"
        out = self.conv.convert_to_bili_html(data)

        assert "[b]粗[/b]" in out
        assert "[url=https://a.com]工具[/url]" in out
        assert '<a href="https://a.com">' not in out


# ── 模块内容 BBCode → HTML ──────────────────────────

class TestBbcodeToHtml:
    def test_signature_link_becomes_anchor(self):
        out = _bbcode_to_html("[b]署名[/b] [url=https://github.com/jiubook/MCTTK]工具[/url]")
        assert "<strong>署名</strong>" in out
        assert '<a href="https://github.com/jiubook/MCTTK">工具</a>' in out

    def test_external_link_becomes_text(self):
        out = _bbcode_to_html("[url=https://a.com]工具[/url]")
        assert "工具" in out
        assert "<a " not in out

    def test_list_becomes_unordered_list(self):
        out = _bbcode_to_html("[list][*]甲\n[*]乙[/list]")
        assert out.count("<li>") == 2

    def test_zero_size_hidden_text_dropped(self):
        """[size=0px] 是 Discuz 的隐藏文本，专栏没有隐藏样式，留着就会真显示。"""
        out = _bbcode_to_html("[size=0px]本帖来自www.mcbbs.co[/size]快照说明")
        assert "mcbbs" not in out
        assert "快照说明" in out

    def test_layout_icons_dropped(self):
        out = _bbcode_to_html("[img=32,32]https://x.com/a.webp[/img]转载须知")
        assert "<img" not in out
        assert "转载须知" in out

    def test_table_and_color_stripped_but_text_kept(self):
        out = _bbcode_to_html("[table=85%][tr=#fff][td][color=#8E2609]注意备份[/color][/td][/tr][/table]")
        assert "注意备份" in out
        assert "[table" not in out
        assert "color=" not in out

    def test_markup_escaped(self):
        assert "<script>" not in _bbcode_to_html("[b]<script>x</script>[/b]")

    def test_empty(self):
        assert _bbcode_to_html("") == ""


# ── 模块平台过滤 ────────────────────────────────────

class TestModulePlatform:
    def _conv(self, module):
        return J2MMConverter({"default_modules": [module], "custom_modules": []})

    def _data(self):
        return {"title": "Test Title", "translated_title": "测试标题", "blocks": []}

    def test_missing_platform_defaults_to_mcbbs(self):
        """老配置里没有 platform 的模块一律按 MCBBS 处理，不能漏到专栏。"""
        conv = self._conv({"id": "m", "position": "end", "order": 1, "enabled": True, "content": "仅论坛"})
        assert "仅论坛" in conv.convert_to_markdown(self._data())
        assert "仅论坛" not in conv.convert_to_bili_html(self._data())

    def test_bilibili_only_module_not_in_forum_output(self):
        conv = self._conv({
            "id": "m", "position": "end", "order": 1, "enabled": True,
            "platform": "bilibili", "content": "仅专栏",
        })
        assert "仅专栏" in conv.convert_to_bili_html(self._data())
        assert "仅专栏" not in conv.convert_to_markdown(self._data())
        assert "仅专栏" not in conv.convert_to_bbcode(self._data())

    def test_shared_module_in_both_outputs(self):
        conv = self._conv({
            "id": "m", "position": "end", "order": 1, "enabled": True,
            "platform": "all", "content": "共用",
        })
        assert "共用" in conv.convert_to_bili_html(self._data())
        assert "共用" in conv.convert_to_markdown(self._data())

    def test_invalid_platform_value_treated_as_mcbbs(self):
        conv = self._conv({
            "id": "m", "position": "end", "order": 1, "enabled": True,
            "platform": "BiliBili-Pro", "content": "拼错平台",
        })
        assert "拼错平台" not in conv.convert_to_bili_html(self._data())

    def test_custom_module_respects_platform(self):
        conv = J2MMConverter({
            "default_modules": [],
            "custom_modules": [
                {"id": "c1", "enabled": True, "platform": "bilibili", "content": "专栏自定义"},
                {"id": "c2", "enabled": True, "content": "论坛自定义"},
            ],
        })
        bili = conv.convert_to_bili_html(self._data())
        markdown = conv.convert_to_markdown(self._data())
        assert "专栏自定义" in bili and "论坛自定义" not in bili
        assert "论坛自定义" in markdown and "专栏自定义" not in markdown


# ── convert_bili_html_file ──────────────────────────

class TestConvertBiliHtmlFile:
    def test_writes_html_file(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(
            json.dumps({"title": "T", "translated_title": "标题",
                        "blocks": [_block("p", "Hello", "你好")]}, ensure_ascii=False),
            encoding="utf-8",
        )
        with unittest.mock.patch("converter.log_debug"):
            out = convert_bili_html_file(str(json_path), output_prefix=str(tmp_path / "news_x"))

        assert out == str(tmp_path / "news_x.html")
        content = (tmp_path / "news_x.html").read_text(encoding="utf-8")
        assert "<h1>标题</h1>" in content
        assert "<p>你好</p>" in content

    def test_missing_json_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            convert_bili_html_file(str(tmp_path / "nope.json"))


class TestConvertBiliOpusFile:
    def test_writes_paragraph_artifact(self, tmp_path):
        json_path = tmp_path / "news_x.json"
        json_path.write_text(
            json.dumps({"title": "T", "translated_title": "标题",
                        "blocks": [_block("h2", "New", "新特性"), _block("p", "Hello", "你好")]},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        with unittest.mock.patch("converter.log_debug"):
            out = convert_bili_opus_file(str(json_path), output_prefix=str(tmp_path / "news_x"))

        assert out == str(tmp_path / "news_x.opus.json")
        artifact = json.loads((tmp_path / "news_x.opus.json").read_text(encoding="utf-8"))
        # 标题段 → 英文标题段 → h2 标题段 + 原文引用段 → 正文段 + 原文引用段
        assert [p["para_type"] for p in artifact["paragraphs"]] == [9, 1, 9, 4, 1, 4]

    def test_missing_json_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            convert_bili_opus_file(str(tmp_path / "nope.json"))
