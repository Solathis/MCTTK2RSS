#!/usr/bin/env python3
"""bili/html.py — B站专栏 HTML 渲染器（老版 form 接口的 content 字段）

老接口 `x/article/creative/draft/addupdate` 收的是整篇 HTML 字符串，编辑器存盘时再把
它一一转成自己的段落模型（实测 132 个段落与 HTML 标签数完全对应）。这条链路 2026-11
真机投过稿、可用，因此原样保留：段模型（`bili/opus.py`）那套出问题时，把发帖用的渲染器
切回 HTML 即可，不必重写。

专栏编辑器只认白名单标签（p/h1/ul/figure/a/strong/em）：既没有 [size]/[color]/[align]
的等价物，内联 style 也会被剥离，所以这里只产出结构标签，排版交给专栏自己。
双语沿用「译文正文 + 原文引用块」而不是灰色小字：专栏没有对应样式，引用块是唯一
稳定可用的视觉降级。
"""

import html
import re

from bili import degrade_link, is_signature_link
from utils import rewrite_md_links


def escape_html(text: str) -> str:
    """转义成 HTML 文本节点/属性值。

    抓回来的正文是数据不是标记：不转义的话正文里一个 `<` 就能把整篇结构写坏。
    """
    return html.escape(text, quote=True)


def _md_images_to_html(text: str) -> str:
    """把内嵌的 Markdown 图片 ![alt](URL) 转成行内 <img>。

    正文图片在 blocks 里是独立图片块（渲染成 <figure>），这里只兜住行内出现的
    图片语法。调用方必须已经做过 HTML 转义，否则 alt/URL 里的引号会截断属性。
    """
    if not text or '![' not in text:
        return text
    return re.sub(
        r'!\[([^\]]*)\]\((https?://[^)\s]+)\)',
        lambda m: f'<img src="{m.group(2)}" alt="{m.group(1)}">',
        text,
    )


def _md_links_to_html(text: str) -> str:
    """把 Markdown 链接 [文字](URL) 转成 <a href>；非署名链接只留「文字（URL）」。

    专栏只认 HTML、没有 BBCode，但外链点开是 B站 的「离开 B站」提示页（实测），
    所以除署名外一律降级成纯文本，地址仍留在正文里供读者复制。
    """
    if not text:
        return text
    return rewrite_md_links(
        _md_images_to_html(text),
        lambda label, url: f'<a href="{url}">{label}</a>' if is_signature_link(url)
        else degrade_link(label, url),
    )


def inline_html(text: str) -> str:
    """把一段译文/原文转成专栏 HTML 的行内内容。

    顺序不能反：先转义再插标签，否则我们插入的 <a>/<img> 会被一起转义成字面量。
    """
    if not text:
        return ""
    return _md_links_to_html(escape_html(text))


def _markdown_inline_to_html(text: str) -> str:
    """把一段 Markdown 行内元素转成 HTML：链接、粗体、斜体；图片丢弃。

    文内换行最终转成 <br>：模块里常见「[b]多行文字[/b]」，压成一行会丢掉断行。
    粗体匹配允许多行（DOTALL），否则跨行的 **...** 会以字面量漏到页面上。
    模块里的图片都是 MCBBS 版面的小图标（站内图床 webp），搬到专栏既没意义
    还要多上传几张图，所以直接删掉。
    """
    out = _md_links_to_html(escape_html(text))
    out = re.sub(r'<img [^>]*>', '', out)
    out = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', out, flags=re.DOTALL)
    out = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', out)
    return out.replace('\n', '<br>')


_MD_LIST_ITEM = re.compile(r'^\s*[-*+]\s+(.*)$')


def markdown_to_html(md: str) -> str:
    """极简 Markdown → HTML，只服务模块内容（段落 / 列表 / 引用 / 标题 / 行内）。

    不追求通用：模块内容由 `converter._bbcode_to_markdown` 产出，只会出现这几种形态，
    为此引入 Markdown 依赖或写完整解析器都不划算。
    """
    if not md:
        return ''
    parts: list[str] = []
    items: list[str] = []
    paragraph: list[str] = []

    def flush_items():
        if items:
            body = ''.join(f'<li>{_markdown_inline_to_html(item)}</li>' for item in items)
            parts.append(f'<ul>{body}</ul>')
            items.clear()

    def flush_paragraph():
        if not paragraph:
            return
        heading = re.match(r'^#{1,6}\s+(.*)$', paragraph[0])
        if heading and len(paragraph) == 1:
            parts.append(f'<p><strong>{_markdown_inline_to_html(heading.group(1))}</strong></p>')
        elif all(line.lstrip().startswith('>') for line in paragraph):
            body = ''.join(
                f'<p>{_markdown_inline_to_html(line.lstrip().removeprefix(">").strip())}</p>'
                for line in paragraph
            )
            parts.append(f'<blockquote>{body}</blockquote>')
        else:
            # 交给行内转换统一处理换行（跨行粗体也要合并），不要逐行拼 <br>
            paragraph_text = "\n".join(paragraph)
            parts.append(f'<p>{_markdown_inline_to_html(paragraph_text)}</p>')
        paragraph.clear()

    for raw_line in md.splitlines():
        line = raw_line.strip()
        if not line:
            flush_items()
            flush_paragraph()
            continue
        item = _MD_LIST_ITEM.match(line)
        if item:
            flush_paragraph()
            items.append(item.group(1))
            continue
        flush_items()
        paragraph.append(line)
    flush_items()
    flush_paragraph()
    return '\n'.join(parts)


# ── 专栏 HTML 渲染器（B站）───────────────────────────

class BiliHTMLRenderer:
    """已翻译 blocks → B站专栏富文本 HTML。

    专栏编辑器只认白名单标签：既没有 [size]/[color]/[align] 的等价物，内联 style
    也会被剥离，所以这里只产出结构标签（p/h1/ul/figure），排版交给专栏自己。
    双语对照沿用「译文正文 + 原文引用块」而不是灰色小字：专栏没有对应样式，
    引用块是唯一稳定可用的视觉降级。
    """

    def render(self, blocks: list[dict]) -> str:
        out = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = (block.get('type') or 'p').lower()
            if btype == 'li':
                chunk, i = self._collect_li_chunk(blocks, i)
                out.append(self._render_li_chunk(chunk))
            elif btype in ('pre', 'code'):
                # 代码块不翻译，原样保留并转义，否则内容里的尖括号会破坏结构
                src = block.get('source_text', '') or block.get('translated_text', '')
                if src.strip():
                    out.append(f'<pre><code>{escape_html(src)}</code></pre>')
                i += 1
            elif btype == 'img':
                out.append(self._render_img(block))
                i += 1
            elif btype in ('h1', 'h2', 'h3', 'h4'):
                out.append(self._render_heading(block))
                i += 1
            elif btype in ('blockquote', 'quote'):
                out.append(self._render_quote(block))
                i += 1
            else:
                out.append(self._render_para(block))
                i += 1
        return '\n'.join(part for part in out if part)

    def _collect_li_chunk(self, blocks, start):
        chunk = []
        i = start
        while i < len(blocks) and blocks[i].get('type', '').lower() == 'li':
            chunk.append(blocks[i])
            i += 1
        return chunk, i

    @staticmethod
    def _dual(src: str, tr: str) -> str:
        """行内双语片段：译文主体 + 原文引用块（两者相同或缺原文时只留一份）。"""
        text = tr or src
        if not text:
            return ''
        content = inline_html(text)
        if tr and src and tr != src:
            content += f'<blockquote><p>{inline_html(src)}</p></blockquote>'
        return content

    def _render_para(self, block):
        src = (block.get('source_text') or '').strip()
        tr = (block.get('translated_text') or '').strip()
        text = tr or src
        if not text:
            return ''
        parts = [f'<p>{inline_html(text)}</p>']
        if tr and src and tr != src:
            parts.append(f'<blockquote><p>{inline_html(src)}</p></blockquote>')
        return '\n'.join(parts)

    def _render_heading(self, block):
        btype = (block.get('type') or '').lower()
        src = (block.get('source_text') or '').strip()
        tr = (block.get('translated_text') or '').strip()
        text = tr or src
        if not text:
            return ''
        if btype in ('h1', 'h2'):
            # 专栏只支持一级标题，h2 也只能落到 h1
            rendered = f'<h1>{inline_html(text)}</h1>'
        else:
            # h3/h4 降级成加粗段落，避免整篇都是大标题
            rendered = f'<p><strong>{inline_html(text)}</strong></p>'
        if tr and src and tr != src:
            rendered += f'\n<blockquote><p>{inline_html(src)}</p></blockquote>'
        return rendered

    def _render_quote(self, block):
        src = (block.get('source_text') or '').strip()
        tr = (block.get('translated_text') or '').strip()
        text = tr or src
        if not text:
            return ''
        parts = [f'<blockquote><p>{inline_html(text)}</p></blockquote>']
        if tr and src and tr != src:
            parts.append(f'<blockquote><p>{inline_html(src)}</p></blockquote>')
        return '\n'.join(parts)

    def _render_img(self, block):
        meta = block.get('meta', {})
        src = (meta.get('src') or '').strip()
        alt = (meta.get('alt') or '').strip()
        if not src:
            return f'<p><em>{escape_html(alt)}</em></p>' if alt else ''
        # 这里保留原始外链：重托管到 B站 CDN 由发帖阶段负责（转换器不碰网络）
        return f'<figure><img src="{escape_html(src)}" alt="{escape_html(alt)}"></figure>'

    def _render_li_chunk(self, items):
        """把连续的 li 块还原成嵌套 <ul>；子列表挂在父项 <li> 内部，保证结构合法。"""
        if not items:
            return ''
        parts: list[str] = []
        levels: list[int] = []
        for item in items:
            level = int(item.get('meta', {}).get('indent_level', 0) or 0)
            while levels and level < levels[-1]:
                parts.append('</li>')
                parts.append('</ul>')
                levels.pop()
            if not levels:
                parts.append('<ul>')
                levels.append(level)
            elif level > levels[-1]:
                # 上一项的 <li> 保持打开，嵌套列表直接放进去
                parts.append('<ul>')
                levels.append(level)
            else:
                parts.append('</li>')
            src = (item.get('source_text') or '').strip()
            tr = (item.get('translated_text') or '').strip()
            parts.append(f'<li>{self._dual(src, tr)}')
        while levels:
            parts.append('</li>')
            parts.append('</ul>')
            levels.pop()
        return '\n'.join(parts)
