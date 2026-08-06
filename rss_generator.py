#!/usr/bin/env python3
"""rss_generator.py — 从 output 目录的 JSON 文章生成 RSS 2.0 Feed

使用 feedgen 库生成标准 RSS 2.0 XML（含 content:encoded 全文）。

用法：
  python rss_generator.py                    # 扫描 output/ 生成 feed.xml
  python rss_generator.py --dir output --out feed.xml

也可作为模块导入：
  from rss_generator import generate_rss
"""
import argparse
import glob
import json
import os
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import bleach
from feedgen.feed import FeedGenerator
from markdown_it import MarkdownIt

# 东八区时区
_TZ_CN = timedelta(hours=8)
_MARKDOWN = MarkdownIt("commonmark", {"html": True, "breaks": True})
_ALLOWED_TAGS = [
    "a", "blockquote", "br", "code", "del", "em", "figcaption", "figure", "h1", "h2", "h3", "h4",
    "img", "li", "mark", "ol", "p", "pre", "strong", "u", "ul",
]
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height", "loading"],
}
_ALLOWED_PROTOCOLS = ["http", "https"]
_MARKDOWN_LINK = re.compile(r"\\?\[([^\]]+)\\?\]\\?\((https?://[^)\s]+)\\?\)")


def _image_key(url: str) -> str:
    """按 URL 主体去重图片，忽略缓存查询参数。"""
    parsed = urlsplit((url or "").strip())
    return parsed._replace(query="", fragment="").geturl().rstrip("/").lower()


def _normalize_markdown_links(text: str) -> str:
    """兼容 AI 添加反斜杠后的 Markdown 链接写法。"""
    return _MARKDOWN_LINK.sub(r"[\1](\2)", text or "")


def _clean_model_text(text: str) -> str:
    """清理 AI 偶发输出的代码围栏、说明前缀和 JSON 外壳。"""
    value = (text or "").strip()
    value = re.sub(r"^```(?:json|markdown|text|html)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value).strip()
    value = re.sub(r"^(?:以下是翻译后的内容|翻译后的内容|译文)\s*[:：]\s*", "", value).strip()

    candidates = [value]
    for marker in ("[{", "{\"translations\"", "{\"translated_text\""):
        index = value.find(marker)
        if index >= 0:
            candidates.append(value[index:])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            if parsed.get("translated_text"):
                return str(parsed["translated_text"]).strip()
            parsed = parsed.get("translations")
        if isinstance(parsed, list):
            values = []
            for item in parsed:
                if isinstance(item, dict):
                    item_text = item.get("translated_text") or item.get("text")
                    if item_text:
                        values.append(str(item_text).strip())
            if values:
                return "\n".join(values)
    return value


def _markdown_to_html(text: str) -> str:
    """将文章文本转为安全 HTML，保留链接、代码、强调和高亮。"""
    html = _MARKDOWN.renderInline(_normalize_markdown_links(_clean_model_text(text)))
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


def _distinct_translation(source: str, translated: str) -> str:
    """相同译文不重复展示，避免原文和译文占用两份版面。"""
    source_text = _clean_model_text(source).strip()
    translated_text = _clean_model_text(translated).strip()
    if not translated_text or translated_text == source_text:
        return ""
    return translated_text


def _is_short_translation(text: str) -> bool:
    """少于10个词的译文与原文同行，避免标题和短标签各占一行。"""
    html = _markdown_to_html(text)
    plain_text = unescape(bleach.clean(html, tags=[], strip=True)).strip()
    words = re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]", plain_text)
    return bool(words) and len(words) < 10


def _render_text_block(block: dict, tag: str = "p") -> str:
    """长文本分行，短译文跟在原文后面。"""
    source = (block.get("source_text") or "").strip()
    translated = _distinct_translation(source, block.get("translated_text", ""))
    if not source and not translated:
        return ""

    if source and translated and _is_short_translation(translated):
        return (
            f'<{tag} style="display:block;color:#333;margin:0 0 0.8em">'
            f'{_markdown_to_html(source)} '
            f'<span style="color:#999">（{_markdown_to_html(translated)}）</span></{tag}>'
        )

    rendered = []
    if source:
        rendered.append(
            f'<{tag} style="display:block;color:#333;margin:0 0 0.35em">'
            f"{_markdown_to_html(source)}</{tag}>"
        )
    if translated:
        rendered.append(
            f'<{tag} style="display:block;color:#999;margin:0 0 0.8em">'
            f"{_markdown_to_html(translated)}</{tag}>"
        )
    return "".join(rendered)


def _render_code_block(block: dict) -> str:
    """代码块也按原文、译文分两行，避免代码内容被 Markdown 再解析。"""
    source = (block.get("source_text") or "").strip()
    translated = _distinct_translation(source, block.get("translated_text", ""))
    rendered = []
    if source:
        rendered.append(f'<pre style="color:#333;margin:0 0 0.35em"><code>{escape(source)}</code></pre>')
    if translated:
        rendered.append(f'<pre style="color:#999;margin:0 0 0.8em"><code>{escape(translated)}</code></pre>')
    return "".join(rendered)


def _render_image(block: dict) -> str:
    """渲染图片 block，避免空 source_text 导致图片被跳过。"""
    meta = block.get("meta") or {}
    src_url = (meta.get("src") or "").strip()
    if not src_url:
        return ""
    alt = meta.get("alt") or ""
    return (
        f'<figure><img src="{escape(src_url)}" alt="{escape(alt)}" '
        f'loading="lazy" />'
        f'{f"<figcaption>{escape(alt)}</figcaption>" if alt else ""}</figure>'
    )


def _parse_date(date_str: str) -> datetime | None:
    """将多种日期格式解析为带时区的 datetime，失败返回 None"""
    if not date_str:
        return None
    s = date_str.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass
    for fmt in ("%d %B %Y", "%B %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _blocks_to_html(blocks: list[dict]) -> str:
    """将结构化 blocks 渲染为合法 HTML，用于 RSS content:encoded。"""
    if not isinstance(blocks, list):
        return ""

    html_parts = []
    list_items = []
    list_tag = "ul"
    seen_images = set()

    def flush_list():
        nonlocal list_tag
        if list_items:
            html_parts.append(f"<{list_tag}>" + "".join(list_items) + f"</{list_tag}>")
            list_items.clear()
            list_tag = "ul"

    for block in blocks:
        btype = (block.get("type") or "p").lower()
        if btype == "img":
            flush_list()
            image_url = (block.get("meta") or {}).get("src", "")
            image_key = _image_key(image_url)
            if image_key and image_key in seen_images:
                continue
            if image_key:
                seen_images.add(image_key)
            image_html = _render_image(block)
            if image_html:
                html_parts.append(image_html)
            continue
        if btype == "li":
            current_list_tag = "ol" if (block.get("meta") or {}).get("list_type") == "ol" else "ul"
            if list_items and current_list_tag != list_tag:
                flush_list()
            list_tag = current_list_tag
            list_items.append(f"<li>{_render_text_block(block, 'span')}</li>")
            continue

        flush_list()
        if btype in ("h1", "h2", "h3", "h4"):
            html_parts.append(_render_text_block(block, btype))
        elif btype in ("pre", "code"):
            html_parts.append(_render_code_block(block))
        elif btype in ("blockquote", "quote"):
            html_parts.append(f"<blockquote>{_render_text_block(block, 'div')}</blockquote>")
        else:
            html_parts.append(_render_text_block(block) or "")

    flush_list()
    return "\n".join(html_parts)


def _load_articles(save_dir: str, max_items: int = 50, max_age_days: int = 90) -> list[dict]:
    """从目录加载近期 news_*.json，按发布日期降序排列。"""
    json_files = glob.glob(os.path.join(save_dir, "news_*.json"))
    cutoff = datetime.now(UTC) - timedelta(days=max(max_age_days, 0))
    articles = []
    for jf in json_files:
        if os.path.basename(jf).startswith("."):
            continue
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("title"):
                continue
            release_date = _parse_date(data.get("release_date", ""))
            if max_age_days > 0 and release_date and release_date < cutoff:
                continue
            articles.append(data)
        except (json.JSONDecodeError, OSError):
            continue

    def sort_key(a):
        dt = _parse_date(a.get("release_date", ""))
        return (dt is not None, dt or datetime.min.replace(tzinfo=UTC))

    articles.sort(key=sort_key, reverse=True)
    return articles[:max_items]


def _build_content_html(article: dict, link: str) -> str:
    """构建单篇文章的 content:encoded HTML。"""
    title_en = (article.get("title") or "").strip()
    title_cn = (article.get("translated_title") or "").strip()
    author = (article.get("author") or "").strip()

    meta_parts = []
    if title_en and title_en != title_cn:
        if title_cn and _is_short_translation(title_cn):
            title_cn_html = _markdown_to_html(title_cn)
            meta_parts.append(
                f'<p><strong>原标题：</strong>{escape(title_en)} '
                f'<span style="color:#999">（{title_cn_html}）</span></p>'
            )
        else:
            meta_parts.append(f"<p><strong>原标题：</strong>{escape(title_en)}</p>")
    if author:
        meta_parts.append(f"<p><strong>作者：</strong>{escape(author)}</p>")
    if link:
        safe_link = escape(link, {"\"": "&quot;"})
        meta_parts.append(f'<p><strong>原文：</strong><a href="{safe_link}">{escape(link)}</a></p>')

    header_image = article.get("header_image_url") or ""
    block_image_keys = {
        _image_key((block.get("meta") or {}).get("src", ""))
        for block in article.get("blocks", [])
        if block.get("type") == "img"
    }
    if header_image and _image_key(header_image) not in block_image_keys:
        alt = article.get("imageAltText") or title_cn or title_en
        meta_parts.append(
            f'<figure><img src="{escape(header_image)}" alt="{escape(alt)}" loading="lazy" /></figure>'
        )

    content_html = _blocks_to_html(article.get("blocks", []))
    return "\n".join(meta_parts) + ("\n" + content_html if content_html else "")


def _translated_summary(article: dict) -> str:
    """从翻译后的 blocks 生成纯文本摘要，避免 description 回退到英文。"""
    translated_parts = []
    for block in article.get("blocks", []):
        if block.get("type") == "img":
            continue
        text = _distinct_translation(
            block.get("source_text", ""), block.get("translated_text", "")
        )
        if text:
            summary_html = _markdown_to_html(text)
            summary_text = bleach.clean(summary_html, tags=[], strip=True)
            translated_parts.append(unescape(summary_text))
    return re.sub(r"\s+", " ", " ".join(translated_parts)).strip()


def generate_rss(
    save_dir: str = "output",
    output_path: str = "feed.xml",
    feed_title: str = "Minecraft News (中文翻译)",
    feed_link: str = "",
    feed_description: str = "Minecraft 官方新闻与更新日志的中文翻译 RSS",
    max_items: int = 50,
    max_age_days: int = 90,
    site_base: str = "https://www.minecraft.net",
) -> str:
    """
    从 output 目录中的 JSON 文章生成 RSS 2.0 XML。

    Returns:
        生成的 XML 字符串
    """
    articles = _load_articles(save_dir, max_items, max_age_days)
    base_link = feed_link or site_base

    fg = FeedGenerator()
    fg.id(base_link)
    fg.title(feed_title)
    fg.link(href=base_link, rel="alternate")
    fg.link(href=base_link.rstrip("/") + "/feed.xml", rel="self")
    fg.description(feed_description)
    fg.language("zh-CN")

    logo_url = ""
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if os.path.exists(logo_path):
        logo_url = base_link.rstrip("/") + "/logo.png"
        fg.image(
            url=logo_url,
            title=feed_title,
            link=base_link,
            width="144",
            height="144",
        )

    for article in articles:
        title_cn = (article.get("translated_title") or "").strip()
        title_en = (article.get("title") or "").strip()
        title = title_cn or title_en
        link = article.get("url", "")

        fe = fg.add_entry(order="append")
        fe.id(link or f"mcttk2rss-{re.sub(r'[^a-zA-Z0-9]', '', title_en)[:50]}")
        fe.title(title)
        fe.link(href=link or base_link)

        # 摘要优先使用翻译后的 blocks，避免把英文 description 放入 RSS
        description = _translated_summary(article)
        if not description:
            description = _clean_model_text(article.get("translated_content", ""))
        if not description:
            description = (article.get("description") or "").strip()
        if description:
            description = description[:300] + "…" if len(description) > 300 else description
            fe.description(description)

        # 发布日期
        dt = _parse_date(article.get("release_date", ""))
        if dt:
            fe.published(dt)
            fe.updated(dt)

        # content:encoded 全文
        content_html = _build_content_html(article, link)
        if content_html:
            # feedgen 的 CDATA 模式保留 HTML 标签，避免输出 &lt;p&gt; 这类转义文本
            fe.content(content_html, type="CDATA")

        # 作者
        author = (article.get("author") or "").strip()
        if author:
            fe.author({"name": author})

    # 确保输出目录存在
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    rss_xml = fg.rss_str(pretty=True)
    if logo_url:
        atom_logo = f"<atom:logo>{escape(logo_url)}</atom:logo>".encode()
        rss_xml = rss_xml.replace(b"<channel>", atom_logo + b"<channel>", 1)
    with open(output_path, "wb") as f:
        f.write(rss_xml)

    print(f"[RSS] 生成 {output_path}（{len(articles)} 篇文章）")
    return rss_xml.decode("utf-8")


def main():
    parser = argparse.ArgumentParser(description="从 output 目录的 JSON 生成 RSS Feed")
    parser.add_argument("--dir", default="output", help="文章 JSON 目录（默认: output）")
    parser.add_argument("--out", default="feed.xml", help="输出 RSS XML 路径（默认: feed.xml）")
    parser.add_argument("--title", default="", help="Feed 标题")
    parser.add_argument("--link", default="", help="Feed 链接（如 GitHub Pages URL）")
    parser.add_argument("--description", default="", help="Feed 描述")
    parser.add_argument("--max-items", type=int, default=0, help="最大条目数")
    args = parser.parse_args()

    # 加载 config.json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    site_base = "https://www.minecraft.net"
    rss_config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            rss_config = cfg.get("rss", {})
            site_base = cfg.get("minecraft_api", {}).get("site_base", site_base)
        except (json.JSONDecodeError, OSError):
            pass

    generate_rss(
        save_dir=args.dir,
        output_path=args.out,
        feed_title=args.title or rss_config.get("feed_title", "Minecraft News (中文翻译)"),
        feed_link=args.link or rss_config.get("feed_link", ""),
        feed_description=args.description or rss_config.get("feed_description", ""),
        max_items=args.max_items or rss_config.get("max_items", 50),
        max_age_days=rss_config.get("max_age_days", 90),
        site_base=site_base,
    )


if __name__ == "__main__":
    main()
