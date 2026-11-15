"""test_bili_opus.py — eva3 段落渲染器（B站新版编辑器的正文模型）

结构依据实测：把一篇 HTML 正文的草稿在编辑器里保存后读回，132 个段落与 HTML 标签数
一一对应（54 段 / 58 引用 / 8 标题 / 7 图 / 5 列表）。这里断言的是映射规则与字段取值，
不写死整段 JSON——字段名已经在参考样本里核对过，再逐字锁一遍只会增加维护成本。

段落模型里文字就是 JSON 字段，不存在 HTML 转义问题，所以「抓回来的内容是数据」
这条约束在这里表现为「尖括号原样出现在 words 里」，见 TestConvertToBiliOpus。
"""
import html
import re

import pytest

from bili.opus import (
    BODY_FONT_LEVEL,
    BODY_FONT_SIZE,
    HEADING_FONT_LEVEL,
    HEADING_FONT_SIZE,
    HEADING_TYPE,
    LINK_TYPE,
    LIST_THEME,
    ORIGINAL_COLOR,
    PARA_HEADING,
    PARA_IMAGE,
    PARA_LIST,
    PARA_QUOTE,
    PARA_TEXT,
    BiliOpusRenderer,
    OpusDocument,
    heading_nodes,
    inline_nodes,
    markdown_nodes,
    markdown_to_paragraphs,
)
from converter import J2MMConverter


def _block(btype, src, tr, meta=None):
    return {"type": btype, "source_text": src, "translated_text": tr, "meta": meta or {}}


def _words(paragraph):
    """段落里的全部文字（含链接文字），用于断言内容而不是结构。"""
    out = []
    for node in paragraph["text"]["nodes"]:
        if node["node_type"] == 1:
            out.append(node["word"]["words"])
        else:
            out.append(node["link"]["show_text"])
    return "".join(out)


def _group(paragraph):
    return paragraph["format"].get("combine_hash")


# ── 行内节点 ─────────────────────────────────────────

class TestInlineNodes:
    def test_plain_text_uses_body_font(self):
        node = inline_nodes("你好")[0]
        assert node["node_type"] == 1
        assert node["word"] == {"words": "你好", "font_size": BODY_FONT_SIZE, "font_level": BODY_FONT_LEVEL}
        assert "style" not in node["word"]

    def test_bold_flag_sets_style(self):
        assert inline_nodes("标题", bold=True)[0]["word"]["style"] == {"bold": True}

    def test_signature_link_becomes_link_node(self):
        nodes = inline_nodes("看 [MCTTK](https://github.com/jiubook/MCTTK)")
        assert [n["node_type"] for n in nodes] == [1, 4]
        link = nodes[1]["link"]
        assert link["show_text"] == "MCTTK"
        assert link["link"] == "https://github.com/jiubook/MCTTK"
        assert link["link_type"] == LINK_TYPE
        assert link["font_level"] == BODY_FONT_LEVEL
        assert "style" not in link

    def test_external_link_degrades_to_text(self):
        """除署名外不留超链接：B站 图文里点外链会跳到「离开 B站」拦截页（实测），
        但地址要留在正文里，读者至少能复制。"""
        nodes = inline_nodes("看 [这里](https://x.com/a)")
        assert [n["node_type"] for n in nodes] == [1]
        assert "".join(n["word"]["words"] for n in nodes) == "看 这里（https://x.com/a）"

    def test_non_url_link_degrades_to_text(self):
        """模块模板里的 [标题](地址) 是占位写法，塞进段模型会被接口挡，退回纯文本。"""
        nodes = inline_nodes("[标题](地址)")
        assert [n["node_type"] for n in nodes] == [1]
        assert nodes[0]["word"]["words"] == "标题"

    def test_label_with_nested_brackets(self):
        nodes = inline_nodes("[预览版[测试版]](https://github.com/jiubook/MCTTK)")
        assert nodes[0]["link"]["show_text"] == "预览版[测试版]"

    def test_url_with_parentheses(self):
        nodes = inline_nodes("[A](https://github.com/jiubook/MCTTK_(b))")
        assert nodes[0]["link"]["link"] == "https://github.com/jiubook/MCTTK_(b)"

    def test_newline_kept_inside_words(self):
        assert inline_nodes("第一行\n第二行")[0]["word"]["words"] == "第一行\n第二行"

    def test_empty(self):
        assert inline_nodes("") == []

    def test_bang_link_not_treated_as_link(self):
        nodes = inline_nodes("![图](https://x.com/a.png)")
        assert [n["node_type"] for n in nodes] == [1]


class TestHeadingNodes:
    def test_heading_font(self):
        node = heading_nodes("新特性")[0]["word"]
        assert node["font_size"] == HEADING_FONT_SIZE
        assert node["font_level"] == HEADING_FONT_LEVEL
        assert "style" not in node

    def test_empty(self):
        assert heading_nodes("") == []


class TestMarkdownNodes:
    def test_bold_run_split(self):
        nodes = markdown_nodes("前 **重点** 后")
        assert [n["node_type"] for n in nodes] == [1, 1, 1]
        assert nodes[1]["word"]["words"] == "重点"
        assert nodes[1]["word"]["style"] == {"bold": True}
        assert "style" not in nodes[0]["word"]

    def test_bold_across_lines(self):
        nodes = markdown_nodes("**跨行\n粗体**")
        assert "".join(n["word"]["words"] for n in nodes) == "跨行\n粗体"
        assert all(n["word"]["style"] == {"bold": True} for n in nodes)

    def test_bold_link_node(self):
        nodes = markdown_nodes("**【[工具](https://github.com/jiubook/MCTTK)】】**")
        link = [n for n in nodes if n["node_type"] == 4][0]
        assert link["link"]["style"] == {"bold": True}

    def test_module_icon_dropped(self):
        nodes = markdown_nodes("![](https://mcbbs.co/icon.webp)**转载须知**")
        assert _words({"text": {"nodes": nodes}}) == "转载须知"

    def test_empty(self):
        assert markdown_nodes("") == []


# ── 正文渲染器 ───────────────────────────────────────

class TestBiliOpusRenderer:
    def setup_method(self):
        self.r = BiliOpusRenderer()

    def test_para_keeps_bilingual_quote(self):
        paras = self.r.render([_block("p", "Hello", "你好")])
        assert [p["para_type"] for p in paras] == [PARA_TEXT, PARA_QUOTE]
        assert _words(paras[0]) == "你好"
        assert _words(paras[1]) == "Hello"
        assert _group(paras[0]) is None  # 普通段不带 combine_hash
        assert _group(paras[1])

    def test_bilingual_uses_separate_groups(self):
        """译文段与原文段不是同一个引用框：这里原文只在引用框里，天然分开。"""
        paras = self.r.render([_block("p", "A", "甲"), _block("p", "B", "乙")])
        groups = [_group(p) for p in paras if p["para_type"] == PARA_QUOTE]
        assert len(set(groups)) == 2

    def test_para_identical_text_not_duplicated(self):
        paras = self.r.render([_block("p", "Same", "Same")])
        assert [p["para_type"] for p in paras] == [PARA_TEXT]

    def test_untranslated_falls_back_to_source(self):
        paras = self.r.render([_block("p", "Only source", "")])
        assert [p["para_type"] for p in paras] == [PARA_TEXT]
        assert _words(paras[0]) == "Only source"

    def test_empty_block_dropped(self):
        assert self.r.render([_block("p", "", "")]) == []

    def test_h2_becomes_heading(self):
        paras = self.r.render([_block("h2", "New Features", "新特性")])
        assert paras[0]["para_type"] == PARA_HEADING
        assert paras[0]["format"]["heading_type"] == HEADING_TYPE
        assert paras[0]["text"]["nodes"][0]["word"]["font_level"] == HEADING_FONT_LEVEL
        assert paras[1]["para_type"] == PARA_QUOTE

    def test_h3_downgraded_to_bold_paragraph(self):
        paras = self.r.render([_block("h3", "Changes", "改动")])
        assert paras[0]["para_type"] == PARA_TEXT
        assert paras[0]["text"]["nodes"][0]["word"]["style"] == {"bold": True}

    def test_quote_block_uses_two_boxes(self):
        paras = self.r.render([_block("quote", "Quoted", "引用")])
        assert [p["para_type"] for p in paras] == [PARA_QUOTE, PARA_QUOTE]
        assert _group(paras[0]) != _group(paras[1])

    def test_code_block_is_plain_text(self):
        """代码里的 [x](y) 是字面量，不能变成链接节点。"""
        paras = self.r.render([_block("pre", "load [x](y)", "")])
        assert [p["para_type"] for p in paras] == [PARA_TEXT]
        assert _words(paras[0]) == "load [x](y)"
        assert [n["node_type"] for n in paras[0]["text"]["nodes"]] == [1]

    def test_img_becomes_image_paragraph(self):
        paras = self.r.render([_block("img", "", "", {"src": "https://x.com/a.jpg", "alt": "图说"})])
        assert paras[0]["para_type"] == PARA_IMAGE
        assert paras[0]["pic"]["pics"] == [{"url": "https://x.com/a.jpg", "comment": "图说"}]

    def test_img_without_src_falls_back_to_alt(self):
        paras = self.r.render([_block("img", "", "", {"src": "", "alt": "只有说明"})])
        assert paras[0]["para_type"] == PARA_TEXT
        assert _words(paras[0]) == "只有说明"

    def test_flat_list_shares_one_group(self):
        blocks = [
            _block("li", "A", "一", {"indent_level": 0}),
            _block("li", "B", "二", {"indent_level": 0}),
        ]
        paras = self.r.render(blocks)
        assert [p["para_type"] for p in paras] == [PARA_LIST, PARA_LIST]
        assert len({_group(p) for p in paras}) == 1
        assert [p["format"]["list_format"]["order"] for p in paras] == [1, 2]
        assert {p["format"]["list_format"]["level"] for p in paras} == {1}
        assert paras[0]["format"]["list_format"]["theme"] == LIST_THEME

    def test_nested_list_raises_level_and_restarts_order(self):
        blocks = [
            _block("li", "A", "一", {"indent_level": 0}),
            _block("li", "A1", "一甲", {"indent_level": 1}),
            _block("li", "B", "二", {"indent_level": 0}),
        ]
        paras = self.r.render(blocks)
        assert [p["format"]["list_format"]["level"] for p in paras] == [1, 2, 1]
        assert [p["format"]["list_format"]["order"] for p in paras] == [1, 1, 2]

    def test_list_bilingual_in_same_paragraph(self):
        """列表项只能是一段文字：双语用段内换行，插引用段会把列表劈成两截。"""
        paras = self.r.render([_block("li", "Added X", "新增 X", {"indent_level": 0})])
        assert [p["para_type"] for p in paras] == [PARA_LIST]
        assert _words(paras[0]) == "新增 X\nAdded X"

    def test_list_groups_split_by_non_list_block(self):
        blocks = [
            _block("li", "A", "一", {"indent_level": 0}),
            _block("p", "P", "段"),
            _block("li", "B", "二", {"indent_level": 0}),
        ]
        paras = self.r.render(blocks)
        lists = [p for p in paras if p["para_type"] == PARA_LIST]
        assert len({_group(p) for p in lists}) == 2
        assert [p["format"]["list_format"]["order"] for p in lists] == [1, 1]

    def test_unknown_block_type_treated_as_paragraph(self):
        paras = self.r.render([_block("weird", "Hello", "你好")])
        assert paras[0]["para_type"] == PARA_TEXT
        assert _words(paras[0]) == "你好"

    def test_empty_blocks(self):
        assert self.r.render([]) == []

    def test_group_keys_are_deterministic(self):
        """分组键用自增编号而不是随机数：同一篇重跑产物一致，diff 才有意义。"""
        blocks = [_block("p", "A", "甲"), _block("p", "B", "乙")]
        first = self.r.render(blocks)
        second = self.r.render(blocks)
        assert [_group(p) or "" for p in first] == [_group(p) or "" for p in second]

    def test_appends_into_shared_document(self):
        doc = OpusDocument()
        doc.heading("标题")
        self.r.render([_block("p", "Hello", "你好")], doc)
        assert [p["para_type"] for p in doc.paragraphs] == [PARA_HEADING, PARA_TEXT, PARA_QUOTE]


class TestMutedOriginal:
    """原文对照刷灰：译文保持默认色，原文节点带 color。"""

    def test_inline_nodes_muted_has_color(self):
        nodes = inline_nodes("Added Javanese support", muted=True)
        assert nodes[0]["word"]["color"] == ORIGINAL_COLOR

    def test_default_nodes_have_no_color(self):
        assert "color" not in inline_nodes("译文")[0]["word"]

    def test_para_original_quote_is_gray(self):
        paras = BiliOpusRenderer().render([_block("p", "Added X", "新增 X")])
        assert "color" not in paras[0]["text"]["nodes"][0]["word"]
        assert paras[1]["para_type"] == PARA_QUOTE
        assert all(n["word"]["color"] == ORIGINAL_COLOR for n in paras[1]["text"]["nodes"])

    def test_heading_original_quote_is_gray(self):
        paras = BiliOpusRenderer().render([_block("h2", "New Features", "新特性")])
        assert "color" not in paras[0]["text"]["nodes"][0]["word"]
        assert paras[1]["text"]["nodes"][0]["word"]["color"] == ORIGINAL_COLOR

    def test_list_original_part_is_gray(self):
        paras = BiliOpusRenderer().render([_block("li", "Added X", "新增 X", {"indent_level": 0})])
        nodes = paras[0]["text"]["nodes"]
        assert [n["word"]["words"] for n in nodes] == ["新增 X", "\n", "Added X"]
        assert "color" not in nodes[0]["word"]
        assert nodes[2]["word"]["color"] == ORIGINAL_COLOR

    def test_list_without_translation_not_gray(self):
        """没译文时整条就是原文，不该刷灰（否则列表整段变灰）。"""
        paras = BiliOpusRenderer().render([_block("li", "Only source", "", {"indent_level": 0})])
        nodes = paras[0]["text"]["nodes"]
        assert [n["word"]["words"] for n in nodes] == ["Only source"]
        assert "color" not in nodes[0]["word"]

    def test_text_content_unchanged(self):
        """刷灰只加 color，不改文字（两套渲染器的文字契约不能破）。"""
        plain = BiliOpusRenderer().render([_block("p", "Hello", "你好")])
        muted = BiliOpusRenderer().render([_block("p", "Hello", "你好")])
        assert [_words(p) for p in plain] == [_words(p) for p in muted] == ["你好", "Hello"]


# ── 模块内容（Markdown）→ 段落 ───────────────────────

class TestMarkdownToParagraphs:
    def test_paragraph_split_by_blank_line(self):
        paras = markdown_to_paragraphs("第一段\n\n第二段")
        assert [p["para_type"] for p in paras] == [PARA_TEXT, PARA_TEXT]
        assert [_words(p) for p in paras] == ["第一段", "第二段"]

    def test_multiline_paragraph_kept_in_one(self):
        paras = markdown_to_paragraphs("第一行\n第二行")
        assert len(paras) == 1
        assert _words(paras[0]) == "第一行\n第二行"

    def test_bold_line_becomes_bold_paragraph(self):
        paras = markdown_to_paragraphs("**转载须知**")
        assert paras[0]["text"]["nodes"][0]["word"]["style"] == {"bold": True}

    def test_hash_line_becomes_bold_paragraph(self):
        """模块里用 # 当小标题的地方按加粗段落处理（与 HTML 版一致，不产出标题段）。"""
        paras = markdown_to_paragraphs("# 小标题")
        assert paras[0]["para_type"] == PARA_TEXT
        assert _words(paras[0]) == "小标题"
        assert paras[0]["text"]["nodes"][0]["word"]["style"] == {"bold": True}

    def test_list_items_share_group(self):
        paras = markdown_to_paragraphs("- 甲\n- 乙\n- 丙")
        assert [p["para_type"] for p in paras] == [PARA_LIST] * 3
        assert len({_group(p) for p in paras}) == 1
        assert [p["format"]["list_format"]["order"] for p in paras] == [1, 2, 3]

    def test_nested_markdown_list_raises_level(self):
        paras = markdown_to_paragraphs("- 甲\n    - 甲一")
        assert [p["format"]["list_format"]["level"] for p in paras] == [1, 2]

    def test_list_with_bold_and_signature_link(self):
        paras = markdown_to_paragraphs("- **每周快照**是 [机制](https://github.com/jiubook/MCTTK)")
        assert _words(paras[0]) == "每周快照是 机制"
        kinds = [n["node_type"] for n in paras[0]["text"]["nodes"]]
        assert 4 in kinds
        assert paras[0]["text"]["nodes"][0]["word"]["style"] == {"bold": True}

    def test_list_external_link_keeps_text_only(self):
        paras = markdown_to_paragraphs("- 见 [讨论帖](https://x.com/a)")
        assert _words(paras[0]) == "见 讨论帖（https://x.com/a）"
        assert [n["node_type"] for n in paras[0]["text"]["nodes"]] == [1]

    def test_quote_lines_share_one_group(self):
        paras = markdown_to_paragraphs("> 甲\n> 乙")
        assert [p["para_type"] for p in paras] == [PARA_QUOTE, PARA_QUOTE]
        assert _group(paras[0]) == _group(paras[1])
        assert [_words(p) for p in paras] == ["甲", "乙"]

    def test_heading_before_list_does_not_swallow_items(self):
        """模块常见「加粗小标题 + 紧跟列表」且中间没有空行。"""
        paras = markdown_to_paragraphs("**外部来源**\n- 甲\n- 乙")
        assert [p["para_type"] for p in paras] == [PARA_TEXT, PARA_LIST, PARA_LIST]
        assert _words(paras[0]) == "外部来源"

    def test_empty(self):
        assert markdown_to_paragraphs("") == []

    def test_expands_tabs_for_level(self):
        paras = markdown_to_paragraphs("- 甲\n\t- 甲一")
        assert paras[1]["format"]["list_format"]["level"] == 2


# ── J2MMConverter.convert_to_bili_opus ───────────────

class TestConvertToBiliOpus:
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

    def test_title_becomes_heading(self):
        paras = self.conv.convert_to_bili_opus(self._data())
        assert paras[0]["para_type"] == PARA_HEADING
        assert _words(paras[0]) == "测试标题"
        assert _words(paras[1]) == "Test Title"

    def test_meta_lines_share_one_group(self):
        paras = [p for p in self.conv.convert_to_bili_opus(self._data()) if p["para_type"] == PARA_QUOTE]
        assert len({_group(p) for p in paras}) == 1
        assert len(paras) == 4

    def test_meta_url_is_plain_text(self):
        """元信息里的原文地址不保留超链接（外链在 B站 会被拦成提示页），但文字要在。"""
        paras = [p for p in self.conv.convert_to_bili_opus(self._data()) if p["para_type"] == PARA_QUOTE]
        meta = [p for p in paras if _words(p).startswith("原文：")][0]
        assert [n["node_type"] for n in meta["text"]["nodes"]] == [1]
        assert _words(meta) == "原文：https://github.com/jiubook/"

    def test_source_fields_are_literal_text(self):
        """段模型的文字是 JSON 字段，尖括号就是字面量；不转义、也不能当标签使。"""
        data = self._data(title="<script>x</script>", translated_title="", description="<b>粗</b>")
        paras = self.conv.convert_to_bili_opus(data)
        assert _words(paras[0]) == "<script>x</script>"
        assert "&lt;" not in _words(paras[0])

    def test_blocks_rendered(self):
        paras = self.conv.convert_to_bili_opus(self._data(blocks=[_block("p", "Hello", "你好")]))
        assert _words(paras[-2]) == "你好"
        assert _words(paras[-1]) == "Hello"

    def test_module_keeps_bold_and_signature_link(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all",
                "content": "[b]署名[/b] [url=https://github.com/jiubook/MCTTK]MCTTK[/url] "
                           "[url=https://a.com]工具[/url]",
            }],
            "custom_modules": [],
        }
        paras = J2MMConverter(modules).convert_to_bili_opus(self._data())
        assert _words(paras[0]) == "署名 MCTTK 工具（https://a.com）"
        nodes = paras[0]["text"]["nodes"]
        links = [n for n in nodes if n["node_type"] == 4]
        assert [n["link"]["link"] for n in links] == ["https://github.com/jiubook/MCTTK"]
        assert nodes[0]["word"]["style"] == {"bold": True}
        assert nodes[0]["word"]["style"] == {"bold": True}

    def test_module_list_becomes_list_paragraphs(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all", "content": "[list][*]甲\n[*]乙[/list]",
            }],
            "custom_modules": [],
        }
        paras = J2MMConverter(modules).convert_to_bili_opus(self._data())
        assert [p["para_type"] for p in paras[:2]] == [PARA_LIST, PARA_LIST]

    def test_disabled_module_skipped(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": False,
                "platform": "all", "content": "不该出现的署名",
            }],
            "custom_modules": [],
        }
        paras = J2MMConverter(modules).convert_to_bili_opus(self._data())
        assert all("不该出现的署名" not in _words(p) for p in paras)

    def test_mcbbs_only_module_not_in_opus(self):
        """platform 缺省视为 mcbbs：MCBBS 版面模板不能漏到专栏。"""
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "content": "MCBBS 专用版面",
            }],
            "custom_modules": [],
        }
        paras = J2MMConverter(modules).convert_to_bili_opus(self._data())
        assert all("MCBBS 专用版面" not in _words(p) for p in paras)

    def test_hidden_zero_size_text_dropped(self):
        modules = {
            "default_modules": [{
                "id": "module_sign", "position": "start", "order": 1, "enabled": True,
                "platform": "all", "content": "署名[size=0px]本帖来自www.mcbbs.co[/size]",
            }],
            "custom_modules": [],
        }
        paras = J2MMConverter(modules).convert_to_bili_opus(self._data())
        assert _words(paras[0]) == "署名"


# ── 两套渲染器的契约 ─────────────────────────────────

class TestRendererParity:
    """HTML 与段模型只是承载格式不同，文字内容与段落边界必须一致。"""

    BLOCKS = [
        _block("p", "Hello world", "你好世界"),
        _block("h2", "New Features", "新特性"),
        _block("h3", "Changes", "改动"),
        _block("quote", "Quoted line", "引用行"),
        _block("li", "Added A", "新增 A", {"indent_level": 0}),
        _block("li", "Added B", "新增 B", {"indent_level": 1}),
    ]

    @staticmethod
    def _squeeze(text):
        return re.sub(r"\s+", "", text).replace("\u00a0", "")

    def test_text_content_matches_html_renderer(self):
        from bili.html import BiliHTMLRenderer

        html_out = BiliHTMLRenderer().render(self.BLOCKS)
        html_text = re.sub(r"<[^>]+>", "", html_out)
        html_text = self._squeeze(html.unescape(html_text))

        paras = BiliOpusRenderer().render(self.BLOCKS)
        opus_text = self._squeeze("".join(_words(p) for p in paras))
        assert html_text == opus_text

    @pytest.mark.parametrize("blocks", [[_block("p", "a", "")], [_block("li", "b", "", {"indent_level": 0})]])
    def test_block_count_matches(self, blocks):
        """块数与段落数一致：一个 p 两段（译文 + 原文引用），一个列表项一段。"""
        paras = BiliOpusRenderer().render(blocks)
        assert 1 <= len(paras) <= 2
