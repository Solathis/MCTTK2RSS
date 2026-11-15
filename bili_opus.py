#!/usr/bin/env python3
"""bili_opus.py — B站专栏 eva3 段落渲染器（新版编辑器 draft/add 的正文模型）

背景：专栏有两套正文模型，互斥。
  - 老接口 `x/article/creative/draft/addupdate` 收 HTML 字符串（本仓库早先用它，见
    `bili_html.py`），但不接受 AI 创作声明等新字段；
  - 新版编辑器（内部代号 eva3）用 `POST /x/dynamic/feed/article/draft/add`，正文是
    结构化段落数组 `opus.content.paragraphs`，声明、卡片、挂载文件都在这个模型里。

段落结构取自实测：把一篇 HTML 正文的草稿在编辑器里保存后读回，132 个段落与 HTML 标签数
一一对应（54 段 / 58 引用 / 8 标题 / 7 图 / 5 列表），字段含义与取值即下面常量：
  para_type   1 普通段 / 2 图片段 / 4 引用段 / 6 列表项 / 9 标题
  node_type   1 文字（word）/ 4 链接（link）
  format      indent（缩进）、heading_type（1=一级标题）、list_format{level,order,theme}、
              combine_hash（编辑器把相邻同值段落并成一个引用框 / 一个列表）
所以这里只做「blocks / Markdown → 段落数组」的纯转换，不碰网络与凭据。

本模块只产出正文；标题、分区、标签、AI 声明等字段由发帖阶段拼进请求体。
"""

import re

from utils import rewrite_md_links

# ── 段落类型 ─────────────────────────────────────────

PARA_TEXT = 1
PARA_IMAGE = 2
PARA_QUOTE = 4
PARA_LIST = 6
PARA_HEADING = 9

NODE_TEXT = 1
NODE_LINK = 4

# ── 编辑器取值（实测，2026-11）───────────────────────

BODY_FONT_SIZE = 17
BODY_FONT_LEVEL = "regular"
HEADING_FONT_SIZE = 24
HEADING_FONT_LEVEL = "xxLarge"
HEADING_TYPE = 1
LIST_THEME = "dot"
LINK_TYPE = 16

_IMAGE_MD = re.compile(r'!\[[^\]]*\]\([^)]*\)')
_BOLD_RUN = re.compile(r'(\*\*.+?\*\*)', re.DOTALL)
_LINK_SLOT = re.compile('\x00L(\\d+)\x00')
_LIST_ITEM = re.compile(r'^(?P<indent>[ \t]*)[-*+]\s+(?P<text>.*)$')
_MD_HEADING = re.compile(r'^#{1,6}\s+(.*)$')
_LIST_INDENT_CHARS = 4


# ── 行内节点 ─────────────────────────────────────────

def _text_node(words: str, *, bold: bool, font_size: int, font_level: str) -> dict:
    word = {"words": words, "font_size": font_size}
    if bold:
        word["style"] = {"bold": True}
    word["font_level"] = font_level
    return {"node_type": NODE_TEXT, "word": word}


def _link_node(show_text: str, url: str, *, bold: bool, font_level: str) -> dict:
    link = {"show_text": show_text, "link": url, "link_type": LINK_TYPE}
    if bold:
        link["style"] = {"bold": True}
    link["font_level"] = font_level
    return {"node_type": NODE_LINK, "link": link}


def _stash_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """把 Markdown 链接换成占位符，返回（占位文本, [(文字, URL)]）。

    只认 http(s)：模块模板里存在 ``[标题](地址)`` 这种占位写法，段落模型里塞非 URL
    可能被接口挡掉，退回纯文本与编辑器剥链接后的效果一致。
    """
    links: list[tuple[str, str]] = []

    def render_link(label: str, url: str) -> str:
        if not url.lower().startswith(("http://", "https://")):
            return label
        links.append((label, url))
        return f"\x00L{len(links) - 1}\x00"

    return rewrite_md_links(text, render_link), links


def _nodes_from(stashed: str, links: list[tuple[str, str]], *, bold: bool,
                font_size: int, font_level: str) -> list[dict]:
    nodes: list[dict] = []
    for index, piece in enumerate(_LINK_SLOT.split(stashed)):
        if index % 2:  # 占位符 → 链接节点
            show_text, url = links[int(piece)]
            nodes.append(_link_node(show_text, url, bold=bold, font_level=font_level))
        elif piece:
            nodes.append(_text_node(piece, bold=bold, font_size=font_size, font_level=font_level))
    return nodes


def inline_nodes(text: str, *, bold: bool = False) -> list[dict]:
    """正文 / 元信息文字 → 节点：只认 Markdown 链接，不解析粗体与图片。

    与 HTML 渲染器的行内规则保持一致（``**`` 在正文里就是字面量），这样两套产物
    在同一篇文章上只有「段落 vs 标签」的差别。
    """
    if not text:
        return []
    stashed, links = _stash_links(text)
    return _nodes_from(
        stashed, links, bold=bold, font_size=BODY_FONT_SIZE,
        font_level=BODY_FONT_LEVEL,
    )


def heading_nodes(text: str) -> list[dict]:
    """标题 → 节点：字号 / 字重按实测的标题规格填。"""
    if not text:
        return []
    stashed, links = _stash_links(text)
    return _nodes_from(
        stashed, links, bold=False, font_size=HEADING_FONT_SIZE,
        font_level=HEADING_FONT_LEVEL,
    )


def markdown_nodes(text: str, *, bold: bool = False) -> list[dict]:
    """模块文字 → 节点：额外解析 ``**粗体**``，并丢掉版面小图标。

    模块里的图片都是 MCBBS 版面的站内图标（webp），搬到专栏既没意义还得多传几张图，
    HTML 渲染器同样直接删掉，这里保持一致。
    """
    if not text:
        return []
    stashed, links = _stash_links(_IMAGE_MD.sub('', text))
    nodes: list[dict] = []
    for run in _BOLD_RUN.split(stashed):
        if not run:
            continue
        run_bold = run.startswith('**') and run.endswith('**') and len(run) > 4
        nodes.extend(_nodes_from(
            run[2:-2] if run_bold else run, links, bold=bold or run_bold,
            font_size=BODY_FONT_SIZE, font_level=BODY_FONT_LEVEL,
        ))
    return nodes


# ── 段落 ─────────────────────────────────────────────

def text_paragraph(nodes: list[dict]) -> dict:
    return {"para_type": PARA_TEXT, "format": {"indent": {}}, "text": {"nodes": nodes}}


def heading_paragraph(nodes: list[dict]) -> dict:
    return {
        "para_type": PARA_HEADING,
        "format": {"indent": {}, "heading_type": HEADING_TYPE},
        "text": {"nodes": nodes},
    }


def quote_paragraph(nodes: list[dict], group: str) -> dict:
    """引用段。同 group 的相邻引用段会被编辑器并成一个引用框。"""
    return {
        "para_type": PARA_QUOTE,
        "format": {"indent": {}, "combine_hash": group},
        "text": {"nodes": nodes},
    }


def list_paragraph(nodes: list[dict], group: str, *, level: int, order: int) -> dict:
    """列表项。level 从 1 开始表示层级，order 是同层内的序号，group 相同的相邻项成组。"""
    return {
        "para_type": PARA_LIST,
        "format": {
            "list_format": {"level": level, "order": order, "theme": LIST_THEME},
            "combine_hash": group,
        },
        "text": {"nodes": nodes},
    }


def image_paragraph(url: str, alt: str = "") -> dict:
    """图片段。

    实测编辑器会写全 url / width / height / size / comment，这里只有 URL 与说明文字：
    宽高与体积要下载图片才知道，而转换器不碰网络；缺的字段由发帖阶段上传后补。
    """
    return {"para_type": PARA_IMAGE, "pic": {"pics": [{"url": url, "comment": alt}]}}


# ── 文档 ─────────────────────────────────────────────

class OpusDocument:
    """一篇专栏的段落集合：唯一的追加入口，也是分组编号的唯一来源。

    combine_hash 是编辑器合并相邻段落用的键（实测形如 blockquote_<毫秒戳>_<随机数>）。
    这里改用按出现顺序自增的确定性编号：产物可复现，重跑同一篇不会因为随机数产生
    无意义的 diff，测试也能直接断言。
    """

    def __init__(self) -> None:
        self.paragraphs: list[dict] = []
        self._group_seq = 0
        self._list_orders: dict[tuple[str, int], int] = {}

    def new_group(self, kind: str) -> str:
        self._group_seq += 1
        return f"{kind}_{self._group_seq}"

    def add(self, paragraph: dict) -> None:
        self.paragraphs.append(paragraph)

    def text(self, text: str, *, bold: bool = False, markdown: bool = False) -> None:
        nodes = markdown_nodes(text, bold=bold) if markdown else inline_nodes(text, bold=bold)
        if nodes:
            self.add(text_paragraph(nodes))

    def heading(self, text: str) -> None:
        nodes = heading_nodes(text)
        if nodes:
            self.add(heading_paragraph(nodes))

    def quote(self, text: str, *, group: str | None = None, markdown: bool = False) -> None:
        nodes = markdown_nodes(text) if markdown else inline_nodes(text)
        if nodes:
            self.add(quote_paragraph(nodes, group or self.new_group("blockquote")))

    def quote_block(self, lines: list[str], *, markdown: bool = False) -> None:
        """一组引用（元信息那种多行一个框）：共用一个 group。"""
        lines = [line for line in lines if line]
        if not lines:
            return
        group = self.new_group("blockquote")
        for line in lines:
            self.quote(line, group=group, markdown=markdown)

    def list_item(self, text: str, group: str, *, level: int = 1, markdown: bool = False) -> None:
        nodes = markdown_nodes(text) if markdown else inline_nodes(text)
        if not nodes:
            return
        key = (group, level)
        order = self._list_orders.get(key, 0) + 1
        self._list_orders[key] = order
        self.add(list_paragraph(nodes, group, level=level, order=order))

    def code(self, text: str) -> None:
        """代码块：原样发一段纯文本。

        不解析链接与粗体——代码里的 ``[x](y)`` 是字面量；新编辑器也没观察到独立的
        代码段类型，先按普通段落发，后面实测到再加。
        """
        if not text:
            return
        self.add(text_paragraph([_text_node(
            text, bold=False, font_size=BODY_FONT_SIZE, font_level=BODY_FONT_LEVEL,
        )]))

    def image(self, url: str, alt: str = "") -> None:
        if url:
            self.add(image_paragraph(url, alt))


def _bilingual_line(translated: str, source: str) -> str:
    """列表项的双语：译文 + 原文放在同一段内换行。

    列表项在段模型里只能是一段文字，往中间插引用段会把一个列表劈成两截；而 BBCode 版
    的写法本来就是「[*]译文\\n原文」同一项内两行，读感一致。
    """
    if translated and source and translated != source:
        return f"{translated}\n{source}"
    return translated or source


# ── 正文渲染器 ───────────────────────────────────────

class BiliOpusRenderer:
    """已翻译 blocks → eva3 段落数组。

    映射规则（与 `bili_html.BiliHTMLRenderer` 一一对应，差别只在承载格式）：
      正文段 → 普通段 + 原文引用段
      h1/h2  → 一级标题段 + 原文引用段（专栏只有一级标题）
      h3/h4  → 加粗段 + 原文引用段（避免整篇都是大标题）
      引用块 → 译文、原文各一个引用框
      连续 li → 列表段，meta.indent_level 决定层级
      图片   → 图片段（外链 URL，重托管由发帖阶段负责）
      代码块 → 原样纯文本段
    """

    def render(self, blocks: list[dict], doc: OpusDocument | None = None) -> list[dict]:
        doc = doc or OpusDocument()
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = (block.get('type') or 'p').lower()
            if btype == 'li':
                chunk, i = self._collect_li_chunk(blocks, i)
                self._render_list(chunk, doc)
            elif btype in ('pre', 'code'):
                raw = block.get('source_text', '') or block.get('translated_text', '')
                doc.code(raw.strip())
                i += 1
            elif btype == 'img':
                self._render_img(block, doc)
                i += 1
            elif btype in ('h1', 'h2', 'h3', 'h4'):
                self._render_heading(block, doc)
                i += 1
            elif btype in ('blockquote', 'quote'):
                self._render_quote(block, doc)
                i += 1
            else:
                self._render_para(block, doc)
                i += 1
        return doc.paragraphs

    @staticmethod
    def _collect_li_chunk(blocks, start):
        chunk = []
        i = start
        while i < len(blocks) and blocks[i].get('type', '').lower() == 'li':
            chunk.append(blocks[i])
            i += 1
        return chunk, i

    @staticmethod
    def _texts(block) -> tuple[str, str]:
        return (
            (block.get('translated_text') or '').strip(),
            (block.get('source_text') or '').strip(),
        )

    @staticmethod
    def _split_dual(block) -> tuple[str, str]:
        """返回（主体文字, 需要另起引用框的原文）；没有译文时不重复原文。"""
        translated, source = BiliOpusRenderer._texts(block)
        main = translated or source
        return main, (source if translated and source and source != translated else '')

    def _render_para(self, block, doc: OpusDocument) -> None:
        main, source = self._split_dual(block)
        doc.text(main)
        if source:
            doc.quote(source)

    def _render_heading(self, block, doc: OpusDocument) -> None:
        main, source = self._split_dual(block)
        if not main:
            return
        if (block.get('type') or '').lower() in ('h1', 'h2'):
            doc.heading(main)
        else:
            doc.text(main, bold=True)
        if source:
            doc.quote(source)

    def _render_quote(self, block, doc: OpusDocument) -> None:
        main, source = self._split_dual(block)
        if not main:
            return
        doc.quote(main)
        if source:
            doc.quote(source)

    def _render_img(self, block, doc: OpusDocument) -> None:
        meta = block.get('meta', {})
        src = (meta.get('src') or '').strip()
        alt = (meta.get('alt') or '').strip()
        if not src:
            doc.text(alt)
            return
        # 这里保留原始外链：重托管到 B站 CDN 由发帖阶段负责（转换器不碰网络）
        doc.image(src, alt)

    def _render_list(self, items: list[dict], doc: OpusDocument) -> None:
        if not items:
            return
        group = doc.new_group("list")
        for item in items:
            level = int(item.get('meta', {}).get('indent_level', 0) or 0) + 1
            translated, source = self._texts(item)
            doc.list_item(_bilingual_line(translated, source), group, level=level)


# ── 模块内容（Markdown）→ 段落 ───────────────────────

def markdown_to_paragraphs(md: str, doc: OpusDocument | None = None) -> list[dict]:
    """极简 Markdown → 段落数组，只服务头尾模块（段落 / 列表 / 引用 / 标题 / 行内）。

    分块规则与 `bili_html.markdown_to_html` 保持一致：模块内容由
    `converter._bbcode_to_markdown` 产出，只会出现这几种形态；两套渲染器看到同样的
    段落边界，才不会出现「HTML 版把两行并成一段、段模型版拆成两段」的差异。
    """
    doc = doc or OpusDocument()
    if not md:
        return doc.paragraphs

    items: list[str] = []
    item_levels: list[int] = []
    paragraph: list[str] = []

    def flush_items():
        if not items:
            return
        group = doc.new_group("list")
        for text, level in zip(items, item_levels, strict=True):
            doc.list_item(text, group, level=level, markdown=True)
        items.clear()
        item_levels.clear()

    def flush_paragraph():
        if not paragraph:
            return
        heading = _MD_HEADING.match(paragraph[0])
        if heading and len(paragraph) == 1:
            doc.text(heading.group(1), bold=True, markdown=True)
        elif all(line.lstrip().startswith('>') for line in paragraph):
            doc.quote_block(
                [line.lstrip().removeprefix('>').strip() for line in paragraph], markdown=True,
            )
        else:
            # 段落内的换行原样留在文字里（编辑器把 <br> 存成 words 里的 \n）
            doc.text("\n".join(paragraph), markdown=True)
        paragraph.clear()

    for raw_line in md.splitlines():
        line = raw_line.strip()
        if not line:
            flush_items()
            flush_paragraph()
            continue
        item = _LIST_ITEM.match(raw_line)
        if item:
            flush_paragraph()
            indent = item.group('indent').expandtabs(_LIST_INDENT_CHARS)
            item_levels.append(len(indent) // _LIST_INDENT_CHARS + 1)
            items.append(item.group('text'))
            continue
        flush_items()
        paragraph.append(line)
    flush_items()
    flush_paragraph()
    return doc.paragraphs
