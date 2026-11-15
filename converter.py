#!/usr/bin/env python3
"""converter.py — JSON 到 BBCode/Markdown 转换器（原 J2MM）

用法:
  python converter.py <input.json> [选项]
  python converter.py --batch <目录> [选项]

也可作为模块导入:
  from converter import J2MMConverter
"""
import argparse
import html
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from log_setup import log_debug, log_error, log_info, log_warning, setup_logging
from utils import CONFIG_DIR, MODULE_TYPE_MAP

# ── 工具函数 ─────────────────────────────────────────

def _find_closing_bracket(text: str, open_pos: int, opener: str, closer: str) -> int:
    """返回与 text[open_pos] 处 opener 匹配的 closer 下标；找不到返回 -1。"""
    depth = 0
    for i in range(open_pos, len(text)):
        ch = text[i]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _md_images_to_bbcode(text: str) -> str:
    """把内嵌在正文里的 Markdown 图片 ![alt](url) 转成 BBCode [img]url[/img]。

    必须先于链接转换执行：否则 ![alt](url) 会被当成 [alt](url) 链接处理，
    结果变成 ![url=url]alt[/url] 这种坏标签。
    图片按块级展示，前后补换行并清掉残留空格，避免和正文粘在同一行。
    """
    if not text or '![' not in text:
        return text
    text = re.sub(
        r'!\[[^\]]*\]\((https?://[^)\s]+)\)',
        lambda m: f'\n[img]{m.group(1)}[/img]\n',
        text,
    )
    text = re.sub(r'[ \t]*\n\[img\]', '\n[img]', text)
    text = re.sub(r'\[/img\]\n[ \t]*', '[/img]\n', text)
    return text.strip()


def _rewrite_md_links(text: str, render_link: Callable[[str, str], str]) -> str:
    """按配对扫描 Markdown 链接 ``[文字](URL)``，把每个链接交给 render_link 格式化。

    BBCode 与 HTML 两种产物只有最终格式不同，扫描逻辑必须共用一份：
    链接文字允许含嵌套方括号（如 ``[9月30日：生物及更多内容！[预览版/测试版]](url)``），
    URL 允许含括号（如 ``https://en.wikipedia.org/wiki/A_(b)``）；
    因此不能用简单的正则匹配，需要按括号配对扫描。
    """
    if not text:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != '[':
            out.append(text[i])
            i += 1
            continue
        # 图片语法已在上一步转成相应标记，这里跳过 ![ 避免被当成链接
        if i > 0 and text[i - 1] == '!':
            out.append(text[i])
            i += 1
            continue
        # 链接文字：方括号需配对，例如 [a[b]c]
        text_end = _find_closing_bracket(text, i, '[', ']')
        if text_end < 0:
            out.append(text[i:])
            break
        if text_end + 1 >= n or text[text_end + 1] != '(':
            # 不是 Markdown 链接，原样保留
            out.append(text[i:text_end + 1])
            i = text_end + 1
            continue
        # URL：圆括号需配对（标题行等可含换行，各方言里压成空格）
        url_end = _find_closing_bracket(text, text_end + 1, '(', ')')
        if url_end < 0:
            out.append(text[i:text_end + 1])
            i = text_end + 1
            continue
        label = text[i + 1:text_end].replace('\n', ' ')
        url = text[text_end + 2:url_end].replace('\n', ' ')
        if label and url:
            out.append(render_link(label, url))
        else:
            out.append(text[i:url_end + 1])
        i = url_end + 1
    return ''.join(out)


def _md_links_to_bbcode(text: str) -> str:
    """把 Markdown 链接 [文字](URL) 转成 BBCode [url=URL]文字[/url]。

    必须先于链接转换执行图片转换：否则 ![alt](url) 会被当成 [alt](url) 链接处理，
    结果变成 ![url=url]alt[/url] 这种坏标签。
    """
    if not text:
        return text
    return _rewrite_md_links(
        _md_images_to_bbcode(text),
        lambda label, url: f'[url={url}]{label}[/url]',
    )


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
    """把 Markdown 链接 [文字](URL) 转成 <a href>（专栏只认 HTML，没有 BBCode）。"""
    if not text:
        return text
    return _rewrite_md_links(
        _md_images_to_html(text),
        lambda label, url: f'<a href="{url}">{label}</a>',
    )


def _escape_html(text: str) -> str:
    """转义成 HTML 文本节点/属性值。

    抓回来的正文是数据不是标记：不转义的话正文里一个 `<` 就能把整篇结构写坏。
    """
    return html.escape(text, quote=True)


def _bili_inline(text: str) -> str:
    """把一段译文/原文转成专栏 HTML 的行内内容。

    顺序不能反：先转义再插标签，否则我们插入的 <a>/<img> 会被一起转义成字面量。
    """
    if not text:
        return ""
    return _md_links_to_html(_escape_html(text))


def _parse_date(date_str: str) -> str:
    if not date_str:
        return date_str
    from datetime import timedelta, timezone
    TZ_CN = timezone(timedelta(hours=8))
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        dt = dt.astimezone(TZ_CN)
        return f"{dt.year}/{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    except Exception:  # noqa: BLE001
        log_debug("_parse_date: fromisoformat 失败，尝试下一格式", exc_info=True)
    try:
        dt = datetime.strptime(date_str.strip(), "%d %B %Y")
        return f"{dt.year}/{dt.month}/{dt.day} 00:00:00"
    except Exception:  # noqa: BLE001
        log_debug("_parse_date: strptime %%d %%B %%Y 失败，尝试下一格式", exc_info=True)
    try:
        dt = datetime.strptime(date_str.strip(), "%B %d, %Y")
        return f"{dt.year}/{dt.month}/{dt.day} 00:00:00"
    except Exception:  # noqa: BLE001
        log_debug("_parse_date: 所有格式均失败，原样返回 %r", date_str, exc_info=True)
    return date_str


def _bbcode_list_to_markdown(content: str, depth: int = 0) -> str:
    """把 [list] 内部内容转成 Markdown 列表，支持嵌套列表。

    - 仅在 [list] 块内才把 [*] 当项目符号，避免正文里作为字面量出现的 [*] 被误改；
    - [list]...[/list] 一定开启新的嵌套层级（不依赖 [*] 与 [list] 之间有无换行）。
    """
    lines = []
    item_text = []       # 累积当前 [*] 项的文字
    nested_seen = False  # 当前项是否已经跟了嵌套列表

    def flush_item():
        text = ''.join(item_text).strip()
        if text and not nested_seen:
            lines.append('    ' * depth + '- ' + text)

    i = 0
    n = len(content)
    while i < n:
        if content.startswith('[*]', i):
            flush_item()
            item_text = []
            nested_seen = False
            i += 3
            continue
        if content.startswith('[list', i):
            head = content.find(']', i)
            end = content.find('[/list]', i)
            if head < 0 or end < 0 or head > end:
                item_text.append(content[i:])
                break
            flush_item()
            item_text = []
            nested_seen = True
            lines.append(_bbcode_list_to_markdown(content[head + 1:end], depth + 1))
            i = end + len('[/list]')
            continue
        item_text.append(content[i])
        i += 1
    flush_item()
    return '\n'.join(line for line in lines if line.strip())


def _find_tag_block(text: str, open_pos: int, open_tag: str, close_tag: str) -> int:
    """返回 open_pos 处 open_tag 对应的 close_tag 起始下标；找不到返回 -1（按嵌套配对）。"""
    depth = 0
    pos = open_pos
    n = len(text)
    while pos < n:
        if text.startswith(open_tag, pos):
            depth += 1
            pos += len(open_tag)
            continue
        if text.startswith(close_tag, pos):
            depth -= 1
            if depth == 0:
                return pos
            pos += len(close_tag)
            continue
        pos += 1
    return -1


def _convert_lists(text: str) -> str:
    """把 [list]...[/list] 转成 Markdown 列表，按嵌套配对取最外层列表块。

    非贪婪正则会匹配到内层 [list] 的 [/list]，导致嵌套列表结构错乱，
    因此这里同样按标签配对扫描。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        start = text.find('[list', i)
        if start < 0:
            out.append(text[i:])
            break
        head = text.find(']', start)
        if head < 0:
            out.append(text[i:])
            break
        close = _find_tag_block(text, start, '[list', '[/list]')
        if close < 0:
            # 缺少闭合标签，原样保留
            out.append(text[i:])
            break
        out.append(text[i:start])
        out.append(_bbcode_list_to_markdown(text[head + 1:close]))
        i = close + len('[/list]')
    return ''.join(out)


def _convert_quotes(text: str) -> str:
    """把 [quote] 转成 Markdown 引用块，支持嵌套引用。

    按 [quote]/[/quote] 配对扫描（而非非贪婪正则），先取出最外层引用的完整内容，
    递归转换其中的内层引用，再整体加 '>' 前缀。
    """
    out = []
    i = 0
    n = len(text)

    def emit_block(chunk):
        """引用属于块级元素，与前后文字之间要断开成独立行。"""
        if not chunk:
            return
        if out and not ''.join(out).endswith('\n'):
            out.append('\n')
        out.append(chunk)
        out.append('\n')

    while i < n:
        start = text.find('[quote]', i)
        if start < 0:
            out.append(text[i:])
            break
        end = _find_tag_block(text, start, '[quote]', '[/quote]')
        if end < 0:
            # 缺少闭合标签，原样保留
            out.append(text[i:])
            break
        out.append(text[i:start])
        inner = _convert_quotes(text[start + len('[quote]'):end])
        emit_block(_quote_to_markdown(inner))
        i = end + len('[/quote]')
    return ''.join(out).strip('\n')


def _quote_to_markdown(content: str) -> str:
    """给引用内容的每一行加 '>' 前缀；内层引用已由递归处理成 '> ...'。"""
    return '\n'.join('> ' + line for line in content.splitlines())


def _bbcode_to_markdown(bbcode: str) -> str:
    if not bbcode:
        return bbcode
    text = bbcode

    # [code] 内容必须原样保留，否则里面的 [b] 之类会被当成样式标签破坏
    code_blocks = []

    def _stash_code(m):
        code_blocks.append(m.group(2))
        return f'\x00CODE{len(code_blocks) - 1}\x00'

    text = re.sub(r'(\[code(?:=[^\]]*)?\])(.*?)(\[/code\])',
                  lambda m: _stash_code(m), text, flags=re.DOTALL)

    # 最多处理 5 层嵌套 BBCode；超过 5 层的嵌套会保留原始标签
    for _ in range(5):
        text = re.sub(r'\[b\](.*?)\[/b\]', r'**\1**', text, flags=re.DOTALL)
        text = re.sub(r'\[i\](.*?)\[/i\]', r'*\1*', text, flags=re.DOTALL)
        text = re.sub(r'\[url=([^\]]+)\](.*?)\[/url\]', r'[\2](\1)', text, flags=re.DOTALL)
        text = re.sub(r'\[size=[^\]]+\](.*?)\[/size\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[color=[^\]]+\](.*?)\[/color\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[align=[^\]]+\](.*?)\[/align\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[font=[^\]]+\](.*?)\[/font\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[table=[^\]]+\](.*?)\[/table\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[tr=[^\]]+\](.*?)\[/tr\]', r'\1\n', text, flags=re.DOTALL)
        text = re.sub(r'\[td\](.*?)\[/td\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[float=[^\]]+\](.*?)\[/float\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\[img=[^\]]+\](.*?)\[/img\]', r'![](\1)', text, flags=re.DOTALL)
        text = re.sub(r'\[img\](.*?)\[/img\]', r'![](\1)', text, flags=re.DOTALL)
        # 先取最外层列表块整体转换（内部嵌套由 _bbcode_list_to_markdown 递归处理）
        text = _convert_lists(text)
        # 嵌套引用：先取最外层引用的完整内容（含内层），内层递归转换后整体加引用前缀
        text = _convert_quotes(text)

    for idx, code in enumerate(code_blocks):
        text = text.replace(f'\x00CODE{idx}\x00', code)

    # 引用块紧邻列表时补一个空行，否则列表会被 Markdown 当成引用内容
    text = re.sub(r'((?:^[ \t]*>.*(?:\n|$))+)(?=[ \t]*(?:[-*+] |\d+\. ))', r'\1\n', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _bbcode_to_plain_html(bbcode: str) -> str:
    """把模块内容（署名 / 协议等）降级成纯文本段落。

    模块是表格 + 彩色 + 对齐拼出来的版面，专栏没有等价物，硬转会原样吐出
    ``[table=85%]`` 之类的脏标签。这里只保留可读文字，链接降级成「文字（地址）」
    以便保留署名与协议溯源。
    """
    if not bbcode:
        return ''
    text = re.sub(r'\[url=([^\]]+)\](.*?)\[/url\]', r'\2（\1）', bbcode, flags=re.DOTALL)
    text = re.sub(r'\[img(?:=[^\]]*)?\].*?\[/img\]', '', text, flags=re.DOTALL)
    text = re.sub(r'\[/?[a-zA-Z*][^\]]*\]', '', text)
    lines = (line.strip() for line in text.splitlines())
    return '\n'.join(f'<p>{_escape_html(line)}</p>' for line in lines if line)


# ── BBCode 渲染器 ───────────────────────────────────

class BBCodeRenderer:
    def render(self, blocks: list[dict]) -> str:
        out = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get('type', 'p').lower()
            if btype == 'li':
                chunk, i = self._collect_li_chunk(blocks, i)
                out.append(self._render_li_chunk(chunk))
            elif btype in ('pre', 'code'):
                src = block.get('source_text', '') or block.get('translated_text', '')
                out.append(f'[code]{src}[/code]')
                i += 1
            elif btype == 'img':
                out.append(self._render_img_bbcode(block))
                i += 1
            elif btype in ('h1', 'h2', 'h3', 'h4'):
                out.append(self._render_heading_bbcode(block))
                i += 1
            elif btype in ('blockquote', 'quote'):
                out.append(self._render_quote_bbcode(block))
                i += 1
            else:
                rendered = self._render_para_bbcode(block)
                if rendered:
                    out.append(rendered)
                i += 1
        return '\n\n'.join(out)

    def _collect_li_chunk(self, blocks, start):
        chunk = []
        i = start
        while i < len(blocks) and blocks[i].get('type', '').lower() == 'li':
            chunk.append(blocks[i])
            i += 1
        return chunk, i

    def _render_li_chunk(self, items):
        if not items:
            return ''
        lines = []
        indent_stack = []
        for item in items:
            level = item.get('meta', {}).get('indent_level', 0)
            src = _md_links_to_bbcode(item.get('source_text', '').strip())
            tr = _md_links_to_bbcode(item.get('translated_text', '').strip())
            closing_tags = ''
            while indent_stack and indent_stack[-1] > level:
                closing_tags += '[/list]'
                indent_stack.pop()
            if closing_tags:
                lines.append(closing_tags)
            if not indent_stack or indent_stack[-1] < level:
                lines.append('[list]')
                indent_stack.append(level)
            if tr and tr == src:
                lines.append(f'[*]{tr}')
            elif tr and src:
                lines.append(f'[*]{tr}\n[color=#bcbcbc]{src}[/color]')
            else:
                lines.append(f'[*]{tr or src}')
        closing = '[/list]' * len(indent_stack)
        indent_stack.clear()
        if closing:
            lines.append(closing)
        return '\n'.join(lines)

    def _render_heading_bbcode(self, block):
        btype = block.get('type', '').lower()
        src = _md_links_to_bbcode(block.get('source_text', '').strip())
        tr = _md_links_to_bbcode(block.get('translated_text', '').strip())
        def duo(main, sub):
            if main and sub and main == sub:
                return main
            if main and sub:
                return f'{main}\n[color=#bcbcbc]{sub}[/color]'
            return main or sub or ''
        content = duo(tr, src)
        if btype in ('h1', 'h2'):
            return f'[hr]\n[size=6][b]{content}[/b][/size]'
        if btype == 'h3':
            return f'[size=5][b]{content}[/b][/size]'
        if btype == 'h4':
            return f'[size=4][b]{content}[/b][/size]'
        return content

    def _render_quote_bbcode(self, block):
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        if tr and tr == src:
            return f'[quote]{tr}[/quote]'
        if tr and src:
            return f'[quote]{tr}\n[color=#bcbcbc]{src}[/color][/quote]'
        return f'[quote]{tr or src}[/quote]'

    def _render_img_bbcode(self, block):
        meta = block.get('meta', {})
        src = meta.get('src', '').strip()
        alt = meta.get('alt', '').strip()
        if not src:
            return f'[i]{alt}[/i]' if alt else ''
        return f'[align=center][img]{src}[/img][/align]'

    def _render_para_bbcode(self, block):
        src = _md_links_to_bbcode(block.get('source_text', '').strip())
        tr = _md_links_to_bbcode(block.get('translated_text', '').strip())
        if tr and tr == src:
            return tr
        if tr and src:
            return f'{tr}\n[color=#bcbcbc]{src}[/color]'
        return tr or src or ''


# ── Markdown 渲染器 ─────────────────────────────────

class MarkdownRenderer:
    def render(self, blocks: list[dict]) -> str:
        out = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get('type', 'p').lower()
            if btype == 'li':
                chunk, i = self._collect_li_chunk(blocks, i)
                out.append(self._render_li_chunk(chunk))
            elif btype in ('pre', 'code'):
                src = block.get('source_text', '') or block.get('translated_text', '')
                out.append(f'```\n{src}\n```')
                i += 1
            elif btype == 'img':
                out.append(self._render_img_md(block))
                i += 1
            elif btype in ('h1', 'h2', 'h3', 'h4'):
                out.append(self._render_heading_md(block))
                i += 1
            elif btype in ('blockquote', 'quote'):
                out.append(self._render_quote_md(block))
                i += 1
            else:
                rendered = self._render_para_md(block)
                if rendered:
                    out.append(rendered)
                i += 1
        return '\n\n'.join(out)

    def _collect_li_chunk(self, blocks, start):
        chunk = []
        i = start
        while i < len(blocks) and blocks[i].get('type', '').lower() == 'li':
            chunk.append(blocks[i])
            i += 1
        return chunk, i

    def _render_li_chunk(self, items):
        if not items:
            return ''
        lines = []
        for item in items:
            level = item.get('meta', {}).get('indent_level', 0)
            src = item.get('source_text', '').strip()
            tr = item.get('translated_text', '').strip()
            indent = '    ' * level
            prefix = f'{indent}- '
            if tr and tr == src:
                lines.append(f'{prefix}{tr}')
            elif tr and src:
                lines.append(f'{prefix}{tr}')
                lines.append(f'{prefix}{src}')
            else:
                lines.append(f'{prefix}{tr or src}')
        return '\n'.join(lines)

    def _render_heading_md(self, block):
        btype = block.get('type', '').lower()
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        level_map = {'h1': '#', 'h2': '##', 'h3': '###', 'h4': '####'}
        prefix = level_map.get(btype, '#')
        hr = '---\n' if btype in ('h1', 'h2') else ''
        text = tr or src
        suffix = f'\n> {src}' if src and tr and src != tr else ''
        return f'{hr}{prefix} {text}{suffix}'

    def _render_quote_md(self, block):
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        a = tr.replace('\n', '\n> ') if tr else ''
        b = src.replace('\n', '\n> ') if src else ''
        if a and b and a == b:
            return f'> {a}'
        if a and b:
            return f'> {a}\n>\n> {b}'
        return f'> {a or b}' if (a or b) else ''

    def _render_img_md(self, block):
        meta = block.get('meta', {})
        src = meta.get('src', '').strip()
        alt = meta.get('alt', '').strip()
        if not src:
            return f'*{alt}*' if alt else ''
        return f'![{alt}]({src})'

    def _render_para_md(self, block):
        src = block.get('source_text', '').strip()
        tr = block.get('translated_text', '').strip()
        if tr and tr == src:
            return tr
        if tr and src:
            return f'{tr}\n\n> {src}'
        return tr or (f'> {src}' if src else '') or ''


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
                    out.append(f'<pre><code>{_escape_html(src)}</code></pre>')
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
        content = _bili_inline(text)
        if tr and src and tr != src:
            content += f'<blockquote><p>{_bili_inline(src)}</p></blockquote>'
        return content

    def _render_para(self, block):
        src = (block.get('source_text') or '').strip()
        tr = (block.get('translated_text') or '').strip()
        text = tr or src
        if not text:
            return ''
        parts = [f'<p>{_bili_inline(text)}</p>']
        if tr and src and tr != src:
            parts.append(f'<blockquote><p>{_bili_inline(src)}</p></blockquote>')
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
            rendered = f'<h1>{_bili_inline(text)}</h1>'
        else:
            # h3/h4 降级成加粗段落，避免整篇都是大标题
            rendered = f'<p><strong>{_bili_inline(text)}</strong></p>'
        if tr and src and tr != src:
            rendered += f'\n<blockquote><p>{_bili_inline(src)}</p></blockquote>'
        return rendered

    def _render_quote(self, block):
        src = (block.get('source_text') or '').strip()
        tr = (block.get('translated_text') or '').strip()
        text = tr or src
        if not text:
            return ''
        parts = [f'<blockquote><p>{_bili_inline(text)}</p></blockquote>']
        if tr and src and tr != src:
            parts.append(f'<blockquote><p>{_bili_inline(src)}</p></blockquote>')
        return '\n'.join(parts)

    def _render_img(self, block):
        meta = block.get('meta', {})
        src = (meta.get('src') or '').strip()
        alt = (meta.get('alt') or '').strip()
        if not src:
            return f'<p><em>{_escape_html(alt)}</em></p>' if alt else ''
        # 这里保留原始外链：重托管到 B站 CDN 由发帖阶段负责（转换器不碰网络）
        return f'<figure><img src="{_escape_html(src)}" alt="{_escape_html(alt)}"></figure>'

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


# ── 文章类型检测 ─────────────────────────────────────

def _detect_article_type(title: str) -> str:
    from utils import classify_article_type
    return classify_article_type(title, commentary=True, fallback='normal')


# ── 主转换器 ─────────────────────────────────────────

class J2MMConverter:
    def __init__(self, modules_config: dict | None = None):
        self.modules_config = modules_config or {'default_modules': [], 'custom_modules': []}
        self._bb = BBCodeRenderer()
        self._md = MarkdownRenderer()
        self._bili = BiliHTMLRenderer()

    def convert_to_bbcode(self, json_data: dict) -> str:
        blocks = json_data.get('blocks', [])
        parts = []
        article_type = _detect_article_type(json_data.get('title', ''))

        for m in self._get_modules('start', article_type):
            parts.append(m.get('bbcode_content') or m['content'])

        parts.append('[hr]')
        parts.append('[align=center][size=5][b]NEWS[/b][/size][/align]')

        title_cn = json_data.get('translated_title', '')
        title_en = json_data.get('title', '')
        if title_cn:
            parts.append(f'[align=center][size=6][b]{title_cn}[/b][/size][/align]')
        if title_en and title_en != title_cn:
            parts.append(f'[align=center][size=4]{title_en}[/size][/align]')

        meta_lines = []
        if json_data.get('release_date'):
            meta_lines.append(f"[b]时间：[/b] {_parse_date(json_data['release_date'])}")
        if json_data.get('author'):
            meta_lines.append(f"[b]作者：[/b] {json_data['author']}")
        if json_data.get('url'):
            u = json_data['url']
            meta_lines.append(f'[b]原文：[/b] [url={u}]{u}[/url]')
        if json_data.get('description'):
            meta_lines.append(f"[b]简介：[/b][i]{json_data['description']}[/i]")
        if meta_lines:
            parts.append('[quote]' + '\n'.join(meta_lines) + '[/quote]')

        if blocks:
            parts.append(self._bb.render(blocks))

        parts.append('[hr]')

        for m in self._get_modules('custom', article_type):
            parts.append(m.get('bbcode_content') or m['content'])
        for m in self._get_modules('end', article_type):
            parts.append(m.get('bbcode_content') or m['content'])

        return '\n\n'.join(p for p in parts if p)

    def convert_to_markdown(self, json_data: dict) -> str:
        blocks = json_data.get('blocks', [])
        parts = []
        article_type = _detect_article_type(json_data.get('title', ''))

        for m in self._get_modules('start', article_type):
            md_content = m.get('markdown_content')
            fallback = _bbcode_to_markdown(m.get('bbcode_content') or m['content'])
            parts.append(md_content if md_content is not None else fallback)

        parts.append('---')
        parts.append('**NEWS**')

        title_cn = json_data.get('translated_title', '')
        title_en = json_data.get('title', '')
        if title_cn:
            parts.append(f'# {title_cn}')
        if title_en and title_en != title_cn:
            parts.append(f'_{title_en}_')

        meta_lines = []
        if json_data.get('release_date'):
            meta_lines.append(f"- 时间：{_parse_date(json_data['release_date'])}")
        if json_data.get('author'):
            meta_lines.append(f"- 作者：{json_data['author']}")
        if json_data.get('url'):
            meta_lines.append(f"- 原文：{json_data['url']}")
        if json_data.get('description'):
            meta_lines.append(f"- 简介：{json_data['description']}")
        if meta_lines:
            parts.append('\n'.join(meta_lines))

        if blocks:
            parts.append(self._md.render(blocks))

        parts.append('---')

        for m in self._get_modules('custom', article_type):
            md_content = m.get('markdown_content')
            fallback = _bbcode_to_markdown(m.get('bbcode_content') or m['content'])
            parts.append(md_content if md_content is not None else fallback)
        for m in self._get_modules('end', article_type):
            md_content = m.get('markdown_content')
            fallback = _bbcode_to_markdown(m.get('bbcode_content') or m['content'])
            parts.append(md_content if md_content is not None else fallback)

        return '\n\n'.join(p for p in parts if p)

    def convert_to_bili_html(self, json_data: dict) -> str:
        """转为 B站专栏富文本 HTML。

        结构对齐 BBCode/Markdown：模块（模块版面专栏无等价物，降级为纯文本段落）
        → 标题 → 元信息引用块 → 正文 → 模块。
        """
        blocks = json_data.get('blocks', [])
        parts = []
        article_type = _detect_article_type(json_data.get('title', ''))

        for m in self._get_modules('start', article_type):
            parts.append(_bbcode_to_plain_html(m.get('bbcode_content') or m['content']))

        title_cn = json_data.get('translated_title', '')
        title_en = json_data.get('title', '')
        if title_cn or title_en:
            parts.append(f'<h1>{_escape_html(title_cn or title_en)}</h1>')
        if title_cn and title_en and title_en != title_cn:
            parts.append(f'<p><em>{_escape_html(title_en)}</em></p>')

        meta_lines = []
        if json_data.get('release_date'):
            meta_lines.append(f'时间：{_escape_html(_parse_date(json_data["release_date"]))}')
        if json_data.get('author'):
            meta_lines.append(f'作者：{_escape_html(json_data["author"])}')
        if json_data.get('url'):
            url = _escape_html(json_data['url'])
            meta_lines.append(f'原文：<a href="{url}">{url}</a>')
        if json_data.get('description'):
            meta_lines.append(f'简介：{_escape_html(json_data["description"])}')
        if meta_lines:
            parts.append('<blockquote>' + ''.join(f'<p>{line}</p>' for line in meta_lines) + '</blockquote>')

        if blocks:
            parts.append(self._bili.render(blocks))

        for position in ('custom', 'end'):
            for m in self._get_modules(position, article_type):
                parts.append(_bbcode_to_plain_html(m.get('bbcode_content') or m['content']))

        return '\n'.join(p for p in parts if p)

    def _get_modules(self, position: str, article_type: str = 'normal') -> list[dict]:
        cfg = self.modules_config
        if position == 'custom':
            return [m for m in cfg.get('custom_modules', []) if m.get('enabled')]
        modules = []
        for m in cfg.get('default_modules', []):
            if m.get('position') != position:
                continue
            if m.get('enabled') or MODULE_TYPE_MAP.get(m.get('id', '')) == article_type:
                modules.append(m)
        return sorted(modules, key=lambda m: m.get('order', 9999))


# ── 文件转换辅助 ─────────────────────────────────────

def convert_json_file(json_path: str, output_prefix: str = None, modules_config: dict = None) -> tuple:
    """
    将单个 JSON 文件转换为 BBCode (.txt) 和 Markdown (.md)

    Returns:
        (bbcode_path, markdown_path) 元组
    """
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    conv = J2MMConverter(modules_config)
    stem = output_prefix or Path(json_path).stem
    blocks = data.get("blocks") or []

    bbcode_content = conv.convert_to_bbcode(data)
    bbcode_path = f'{stem}.txt'
    with open(bbcode_path, 'w', encoding='utf-8') as f:
        f.write(bbcode_content)
    log_debug("[转换] BBCode: %s（%d 块 → %d 字符）", bbcode_path, len(blocks), len(bbcode_content))

    md_content = conv.convert_to_markdown(data)
    md_path = f'{stem}.md'
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_content)
    log_debug("[转换] Markdown: %s（%d 字符）", md_path, len(md_content))

    return bbcode_path, md_path


def convert_bili_html_file(json_path: str, output_prefix: str = None, modules_config: dict = None) -> str:
    """
    将单个 JSON 文件转换为 B站专栏 HTML (.html)

    与 convert_json_file 分开而不是合并成多返回值：后者的 (bbcode_path, md_path)
    会被 main 解包后继续往下传，改签名会波及调用方与测试，收益不足以抵消风险。

    Returns:
        html_path
    """
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)

    conv = J2MMConverter(modules_config)
    stem = output_prefix or Path(json_path).stem
    content = conv.convert_to_bili_html(data)
    html_path = f'{stem}.html'
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(content)
    log_debug("[转换] 专栏 HTML: %s（%d 块 → %d 字符）", html_path, len(data.get("blocks") or []), len(content))

    return html_path


# ── CLI 入口 ─────────────────────────────────────────

def _load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _load_modules(path):
    if path and Path(path).exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return None


def _save(content, path):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    log_info(f'[OK] {path}')


def main():
    parser = argparse.ArgumentParser(description='converter.py — JSON 到 BBCode/Markdown 转换器')
    parser.add_argument('input', nargs='?', help='输入 JSON 文件路径')
    parser.add_argument('--batch', metavar='DIR', help='批量转换目录')
    parser.add_argument('-o', '--output', help='输出路径')
    parser.add_argument('-m', '--modules', help='模块配置文件路径')
    parser.add_argument('--bbcode-only', action='store_true', help='仅输出 BBCode')
    parser.add_argument('--markdown-only', action='store_true', help='仅输出 Markdown')
    args = parser.parse_args()

    if not args.modules:
        default_cfg = Path(CONFIG_DIR) / 'modules_config.json'
        if default_cfg.exists():
            args.modules = str(default_cfg)

    if args.batch:
        in_dir = Path(args.batch)
        out_dir = Path(args.output) if args.output else in_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        modules = _load_modules(args.modules)
        conv = J2MMConverter(modules)
        json_files = list(in_dir.glob('*.json'))
        if not json_files:
            log_warning(f'未找到 JSON 文件: {in_dir}')
            return
        ok = 0
        for jf in json_files:
            try:
                data = _load_json(str(jf))
                stem = jf.stem
                if not args.markdown_only:
                    _save(conv.convert_to_bbcode(data), str(out_dir / f'{stem}.txt'))
                if not args.bbcode_only:
                    _save(conv.convert_to_markdown(data), str(out_dir / f'{stem}.md'))
                ok += 1
            except Exception as e:
                log_error(f'[错误] {jf.name}: {e}')
        log_info(f'\n完成：{ok}/{len(json_files)}')
    elif args.input:
        modules = _load_modules(args.modules)
        data = _load_json(args.input)
        conv = J2MMConverter(modules)
        stem = args.output or Path(args.input).stem
        if not args.markdown_only:
            _save(conv.convert_to_bbcode(data), f'{stem}.txt')
        if not args.bbcode_only:
            _save(conv.convert_to_markdown(data), f'{stem}.md')
    else:
        parser.print_help()


if __name__ == '__main__':
    setup_logging()
    main()
