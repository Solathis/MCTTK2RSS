"""test_converter.py — converter.py 单元测试

测试原则：只测纯逻辑函数，不依赖文件系统或网络。
"""
import pytest

from converter import (
    BBCodeRenderer,
    J2MMConverter,
    MarkdownRenderer,
    _bbcode_to_markdown,
    _detect_article_type,
    _md_links_to_bbcode,
    _parse_date,
)

# ── _md_links_to_bbcode ──────────────────────────────

class TestMdLinksToBBCode:
    def test_basic(self):
        assert _md_links_to_bbcode("[文字](https://example.com)") == "[url=https://example.com]文字[/url]"

    def test_no_link(self):
        assert _md_links_to_bbcode("普通文字") == "普通文字"

    def test_multiple(self):
        result = _md_links_to_bbcode("[A](http://a.com) and [B](http://b.com)")
        assert "[url=http://a.com]A[/url]" in result
        assert "[url=http://b.com]B[/url]" in result

    def test_empty(self):
        assert _md_links_to_bbcode("") == ""

    def test_nested_brackets_in_label(self):
        """链接文字含方括号时也要转换（如官网文章的 [预览版/测试版] 后缀）。"""
        src = ("[9月30日：生物及更多内容！[预览版/测试版]]"
               "(https://www.minecraft.net/zh-hans/article/drop-4-2026-testing#Mobs)")
        assert _md_links_to_bbcode(src) == (
            "[url=https://www.minecraft.net/zh-hans/article/drop-4-2026-testing#Mobs]"
            "9月30日：生物及更多内容！[预览版/测试版][/url]"
        )

    def test_nested_brackets_english(self):
        src = ("[Sept ember 30: Mobs and more! [Preview/beta]]"
               "(https://www.minecraft.net/zh-hans/article/drop-4-2026-testing#Mobs)")
        out = _md_links_to_bbcode(src)
        assert out.startswith("[url=https://www.minecraft.net/zh-hans/article/drop-4-2026-testing#Mobs]")
        assert out.endswith("[/url]")
        assert "Mobs and more! [Preview/beta]" in out

    def test_parentheses_in_url(self):
        src = "[Wiki](https://en.wikipedia.org/wiki/Function_(mathematics)) 词条"
        assert _md_links_to_bbcode(src) == (
            "[url=https://en.wikipedia.org/wiki/Function_(mathematics)]Wiki[/url] 词条"
        )

    def test_markdown_image_becomes_img_tag(self):
        """![alt](url) 不能退化成 ![url=url]alt[/url]，应转成 [img]url[/img]。"""
        assert _md_links_to_bbcode("![图片](https://x.com/a.png)") == "[img]https://x.com/a.png[/img]"

    def test_image_and_link_mixed(self):
        result = _md_links_to_bbcode("![图片](https://x.com/a.png) 与 [链接](https://x.com/b)")
        assert "[img]https://x.com/a.png[/img]" in result
        assert "[url=https://x.com/b]链接[/url]" in result
        assert "![url=" not in result

    def test_inline_image_isolated_on_own_line(self):
        assert _md_links_to_bbcode("看这张图 ![示意图](https://x.com/a.png) 很清楚") == (
            "看这张图\n[img]https://x.com/a.png[/img]\n很清楚"
        )

    def test_plain_brackets_untouched(self):
        assert _md_links_to_bbcode("无 URL 的 [裸括号] 保持原样") == "无 URL 的 [裸括号] 保持原样"

    def test_unclosed_markdown_untouched(self):
        assert _md_links_to_bbcode("[未闭合](http://x.com") == "[未闭合](http://x.com"
        assert _md_links_to_bbcode("[未闭合文字(http://x.com)") == "[未闭合文字(http://x.com)"

    def test_existing_bbcode_link_untouched(self):
        src = "已转好的 [url=http://x.com]文字[/url] 不受影响"
        assert _md_links_to_bbcode(src) == src


# ── _parse_date ──────────────────────────────────────

class TestParseDate:
    def test_iso_utc(self):
        result = _parse_date("2024-03-15T10:30:00Z")
        assert "2024" in result
        assert "18" in result  # UTC+8: 10+8=18

    def test_iso_with_offset(self):
        result = _parse_date("2024-03-15T10:30:00+00:00")
        assert "2024" in result

    def test_day_month_year(self):
        result = _parse_date("15 March 2024")
        assert "2024" in result
        assert "3" in result
        assert "15" in result

    def test_month_day_year(self):
        result = _parse_date("March 15, 2024")
        assert "2024" in result

    def test_empty(self):
        assert _parse_date("") == ""

    def test_invalid_returns_original(self):
        assert _parse_date("not-a-date") == "not-a-date"


# ── _bbcode_to_markdown ──────────────────────────────

class TestBBCodeToMarkdown:
    def test_bold(self):
        assert _bbcode_to_markdown("[b]粗体[/b]") == "**粗体**"

    def test_italic(self):
        assert _bbcode_to_markdown("[i]斜体[/i]") == "*斜体*"

    def test_url(self):
        result = _bbcode_to_markdown("[url=http://example.com]链接[/url]")
        assert "[链接](http://example.com)" in result

    def test_img(self):
        result = _bbcode_to_markdown("[img]http://example.com/a.png[/img]")
        assert "http://example.com/a.png" in result

    def test_list(self):
        result = _bbcode_to_markdown("[list][*]项目1[*]项目2[/list]")
        assert "- 项目1" in result
        assert "- 项目2" in result

    def test_strip_size_color(self):
        result = _bbcode_to_markdown("[size=5][color=red]文字[/color][/size]")
        assert "文字" in result
        assert "[size" not in result
        assert "[color" not in result

    def test_nested_quote(self):
        """嵌套引用要分行并保留层级，不能挤成 '> 外层> 内层'。"""
        assert _bbcode_to_markdown("[quote]外层[quote]内层[/quote]结尾[/quote]") == (
            "> 外层\n> > 内层\n> 结尾"
        )

    def test_quote_multiline_nested(self):
        assert _bbcode_to_markdown("[quote]A\n[quote]B1\nB2[/quote]\nC[/quote]") == (
            "> A\n> > B1\n> > B2\n> \n> C"
        )

    def test_quote_is_block_level(self):
        assert _bbcode_to_markdown("前文\n[quote]引用[/quote]\n后文") == "前文\n> 引用\n\n后文"

    def test_unclosed_quote_kept(self):
        assert _bbcode_to_markdown("[quote]没有闭合") == "[quote]没有闭合"

    def test_code_block_content_untouched(self):
        """[code] 内的方括号文本不能被当成样式标签。"""
        assert _bbcode_to_markdown("[code][b]not bold[/b][i]x[/i][/code]") == "[b]not bold[/b][i]x[/i]"

    def test_list_items_one_per_line(self):
        assert _bbcode_to_markdown("[list][*]A\n[*]B\n[*]C[/list]") == "- A\n- B\n- C"

    def test_nested_list_indented(self):
        assert _bbcode_to_markdown("[list][*]外1[list][*]内1[*]内2[/list][*]外2[/list]") == (
            "- 外1\n    - 内1\n    - 内2\n- 外2"
        )

    def test_list_with_param(self):
        assert _bbcode_to_markdown("[list=1][*]A[*]B[/list]") == "- A\n- B"

    def test_literal_star_outside_list_kept(self):
        """正文里作为字面量出现的 [*] 不应被改成列表项。"""
        assert _bbcode_to_markdown("某处的 [*] 只是普通字符") == "某处的 [*] 只是普通字符"

    def test_unclosed_list_kept(self):
        assert _bbcode_to_markdown("[list][*]A") == "[list][*]A"

    def test_list_item_with_link(self):
        assert _bbcode_to_markdown("[list][*][url=https://x.com]链接[/url] 说明[/list]") == (
            "- [链接](https://x.com) 说明"
        )

    def test_list_after_quote_separated(self):
        """引用块紧跟列表时要补空行，否则列表会被 Markdown 当成引用内容。"""
        assert _bbcode_to_markdown("[quote]引用[/quote][list][*]A[/list]") == "> 引用\n\n- A"


# ── _detect_article_type ─────────────────────────────

class TestDetectArticleType:
    @pytest.mark.parametrize("title,expected", [
        ("Minecraft Java Edition Snapshot 24w10a", "java_snapshot"),
        ("Minecraft Java Edition 1.21 Pre-Release 1", "java_prerelease"),
        ("Minecraft Java Edition 1.21 Release Candidate 1", "java_rc"),
        ("Minecraft Java Edition 1.21", "java_release"),
        ("Minecraft Beta & Preview 1.21.0.20", "bedrock_beta"),
        ("Minecraft Preview 1.21.0.20", "bedrock_beta"),
        ("Minecraft Bedrock Edition 1.21", "bedrock_release"),
        ("普通新闻标题", "normal"),
        ("", "normal"),
    ])
    def test_types(self, title, expected):
        assert _detect_article_type(title) == expected

    def test_prerelease_variants(self):
        assert _detect_article_type("1.21 Pre Release 1") == "java_prerelease"
        assert _detect_article_type("1.21 prerelease 1") == "java_prerelease"


# ── BBCodeRenderer ───────────────────────────────────

class TestBBCodeRenderer:
    def setup_method(self):
        self.r = BBCodeRenderer()

    def _block(self, btype, src, tr="", meta=None):
        return {"type": btype, "source_text": src, "translated_text": tr, "meta": meta or {}}

    def test_para_bilingual(self):
        result = self.r.render([self._block("p", "Hello", "你好")])
        assert "你好" in result
        assert "Hello" in result
        assert "[color=#bcbcbc]" in result

    def test_para_same_text(self):
        result = self.r.render([self._block("p", "Hello", "Hello")])
        assert result.count("Hello") == 1

    def test_para_no_translation(self):
        result = self.r.render([self._block("p", "Hello", "")])
        assert "Hello" in result

    def test_para_link_with_nested_brackets(self):
        """正文里的嵌套方括号链接也要转成 BBCode，不能残留 Markdown。"""
        src = ("[9月30日：生物及更多内容！[预览版/测试版]]"
               "(https://www.minecraft.net/zh-hans/article/drop-4-2026-testing#Mobs)")
        result = self.r.render([self._block("p", src, "")])
        assert "](https://www.minecraft.net" not in result
        assert "[url=https://www.minecraft.net/zh-hans/article/drop-4-2026-testing#Mobs]" in result
        assert "9月30日：生物及更多内容！[预览版/测试版][/url]" in result

    def test_heading_h1(self):
        result = self.r.render([self._block("h1", "Title", "标题")])
        assert "[hr]" in result
        assert "[size=6]" in result
        assert "[b]" in result

    def test_heading_h3(self):
        result = self.r.render([self._block("h3", "Sub", "子标题")])
        assert "[size=5]" in result

    def test_code_block(self):
        result = self.r.render([self._block("pre", "code here")])
        assert "[code]code here[/code]" in result

    def test_img_with_src(self):
        result = self.r.render([self._block("img", "", "", meta={"src": "http://img.com/a.png", "alt": "图片"})])
        assert "[img]http://img.com/a.png[/img]" in result
        assert "[align=center]" in result

    def test_img_no_src(self):
        result = self.r.render([self._block("img", "", "", meta={"src": "", "alt": "描述"})])
        assert "[i]描述[/i]" in result

    def test_quote(self):
        result = self.r.render([self._block("blockquote", "原文", "译文")])
        assert "[quote]" in result
        assert "译文" in result

    def test_li_basic(self):
        blocks = [
            {"type": "li", "source_text": "Item 1", "translated_text": "项目1", "meta": {"indent_level": 0}},
            {"type": "li", "source_text": "Item 2", "translated_text": "项目2", "meta": {"indent_level": 0}},
        ]
        result = self.r.render(blocks)
        assert "[list]" in result
        assert "[*]项目1" in result
        assert "[*]项目2" in result
        assert "[/list]" in result

    def test_li_nested(self):
        blocks = [
            {"type": "li", "source_text": "Parent", "translated_text": "父", "meta": {"indent_level": 0}},
            {"type": "li", "source_text": "Child", "translated_text": "子", "meta": {"indent_level": 1}},
        ]
        result = self.r.render(blocks)
        assert result.count("[list]") == 2

    def test_empty_blocks(self):
        assert self.r.render([]) == ""


# ── MarkdownRenderer ─────────────────────────────────

class TestMarkdownRenderer:
    def setup_method(self):
        self.r = MarkdownRenderer()

    def _block(self, btype, src, tr="", meta=None):
        return {"type": btype, "source_text": src, "translated_text": tr, "meta": meta or {}}

    def test_para_bilingual(self):
        result = self.r.render([self._block("p", "Hello", "你好")])
        assert "你好" in result
        assert "Hello" in result

    def test_para_same(self):
        result = self.r.render([self._block("p", "Hello", "Hello")])
        assert result.count("Hello") == 1

    def test_heading_h1(self):
        result = self.r.render([self._block("h1", "Title", "标题")])
        assert "# 标题" in result
        assert "---" in result

    def test_heading_h2(self):
        result = self.r.render([self._block("h2", "Sub", "子标题")])
        assert "## 子标题" in result

    def test_heading_h3(self):
        result = self.r.render([self._block("h3", "Sub", "子标题")])
        assert "### 子标题" in result
        assert "---" not in result

    def test_code_block(self):
        result = self.r.render([self._block("pre", "code here")])
        assert "```" in result
        assert "code here" in result

    def test_img(self):
        result = self.r.render([self._block("img", "", "", meta={"src": "http://img.com/a.png", "alt": "图片"})])
        assert "![图片](http://img.com/a.png)" in result

    def test_img_no_src(self):
        result = self.r.render([self._block("img", "", "", meta={"src": "", "alt": "描述"})])
        assert "*描述*" in result

    def test_quote(self):
        result = self.r.render([self._block("blockquote", "原文", "译文")])
        assert "> 译文" in result

    def test_li(self):
        blocks = [
            {"type": "li", "source_text": "Item", "translated_text": "项目", "meta": {"indent_level": 0}},
        ]
        result = self.r.render(blocks)
        assert "- 项目" in result

    def test_li_indent(self):
        blocks = [
            {"type": "li", "source_text": "Child", "translated_text": "子", "meta": {"indent_level": 2}},
        ]
        result = self.r.render(blocks)
        assert "        - 子" in result  # 2 * 4 spaces


# ── J2MMConverter ────────────────────────────────────

class TestJ2MMConverter:
    def setup_method(self):
        self.conv = J2MMConverter()

    def _make_data(self, title="Test Title", translated_title="测试标题", blocks=None):
        return {
            "title": title,
            "translated_title": translated_title,
            "release_date": "2024-03-15T10:00:00Z",
            "author": "jiubook",
            "url": "https://github.com/jiubook/",
            "description": "测试描述",
            "blocks": blocks or [],
        }

    def test_bbcode_has_title(self):
        result = self.conv.convert_to_bbcode(self._make_data())
        assert "测试标题" in result
        assert "Test Title" in result

    def test_bbcode_has_meta(self):
        result = self.conv.convert_to_bbcode(self._make_data())
        assert "jiubook" in result
        assert "github.com/jiubook" in result

    def test_bbcode_has_hr(self):
        result = self.conv.convert_to_bbcode(self._make_data())
        assert "[hr]" in result

    def test_markdown_has_title(self):
        result = self.conv.convert_to_markdown(self._make_data())
        assert "# 测试标题" in result

    def test_markdown_has_separator(self):
        result = self.conv.convert_to_markdown(self._make_data())
        assert "---" in result

    def test_bbcode_with_blocks(self):
        blocks = [{"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}}]
        result = self.conv.convert_to_bbcode(self._make_data(blocks=blocks))
        assert "你好" in result

    def test_markdown_with_blocks(self):
        blocks = [{"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}}]
        result = self.conv.convert_to_markdown(self._make_data(blocks=blocks))
        assert "你好" in result

    def test_same_title_no_duplicate(self):
        data = self._make_data(title="同一标题", translated_title="同一标题")
        result = self.conv.convert_to_bbcode(data)
        assert result.count("同一标题") == 1

    def test_no_translated_title(self):
        data = self._make_data(translated_title="")
        result = self.conv.convert_to_bbcode(data)
        assert "Test Title" in result

    def test_snapshot_type_detection(self):
        data = self._make_data(title="Minecraft Java Edition Snapshot 24w10a")
        result = self.conv.convert_to_bbcode(data)
        assert result  # 不崩溃即可

    def test_get_modules_empty_config(self):
        conv = J2MMConverter(modules_config=None)
        assert conv._get_modules("start") == []
        assert conv._get_modules("end") == []
        assert conv._get_modules("custom") == []
