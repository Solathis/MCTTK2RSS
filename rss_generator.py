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
from xml.sax.saxutils import escape

from feedgen.feed import FeedGenerator

# 东八区时区
_TZ_CN = timedelta(hours=8)


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
    """将文章 blocks 渲染为 HTML 用于 RSS content:encoded"""
    if not blocks or not isinstance(blocks, list):
        return ""
    html_parts = []
    for block in blocks:
        btype = (block.get("type") or "p").lower()
        tr = (block.get("translated_text") or "").strip()
        src = (block.get("source_text") or "").strip()
        text = tr or src
        if not text:
            continue
        if btype in ("h1", "h2", "h3", "h4"):
            level = int(btype[-1])
            html_parts.append(f"<h{level}>{escape(text)}</h{level}>")
        elif btype in ("pre", "code"):
            html_parts.append(f"<pre><code>{escape(text)}</code></pre>")
        elif btype == "img":
            meta = block.get("meta") or {}
            src_url = meta.get("src", "")
            alt = meta.get("alt", "")
            if src_url:
                html_parts.append(f'<img src="{escape(src_url)}" alt="{escape(alt)}" />')
        elif btype == "li":
            html_parts.append(f"<li>{escape(text)}</li>")
        elif btype in ("blockquote", "quote"):
            html_parts.append(f"<blockquote>{escape(text)}</blockquote>")
        else:
            html_parts.append(f"<p>{escape(text)}</p>")
    return "\n".join(html_parts)


def _load_articles(save_dir: str, max_items: int = 50) -> list[dict]:
    """从目录加载 news_*.json，按发布日期降序排列"""
    json_files = glob.glob(os.path.join(save_dir, "news_*.json"))
    articles = []
    for jf in json_files:
        if os.path.basename(jf).startswith("."):
            continue
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("title"):
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
    """构建单篇文章的 content:encoded HTML"""
    title_en = (article.get("title") or "").strip()
    title_cn = (article.get("translated_title") or "").strip()
    author = (article.get("author") or "").strip()

    meta_parts = []
    if title_en and title_en != title_cn:
        meta_parts.append(f"<p><strong>原标题：</strong>{escape(title_en)}</p>")
    if author:
        meta_parts.append(f"<p><strong>作者：</strong>{escape(author)}</p>")
    if link:
        meta_parts.append(f'<p><strong>原文：</strong><a href="{link}">{link}</a></p>')

    content_html = _blocks_to_html(article.get("blocks", []))
    return "\n".join(meta_parts) + content_html if content_html else "\n".join(meta_parts)


def generate_rss(
    save_dir: str = "output",
    output_path: str = "feed.xml",
    feed_title: str = "Minecraft News (中文翻译)",
    feed_link: str = "",
    feed_description: str = "Minecraft 官方新闻与更新日志的中文翻译 RSS",
    max_items: int = 50,
    site_base: str = "https://www.minecraft.net",
) -> str:
    """
    从 output 目录中的 JSON 文章生成 RSS 2.0 XML。

    Returns:
        生成的 XML 字符串
    """
    articles = _load_articles(save_dir, max_items)
    base_link = feed_link or site_base

    fg = FeedGenerator()
    fg.id(base_link)
    fg.title(feed_title)
    fg.link(href=base_link, rel="alternate")
    fg.link(href=base_link.rstrip("/") + "/feed.xml", rel="self")
    fg.description(feed_description)
    fg.language("zh-CN")

    # RSS channel logo（feed.xml 中的 <image> 元素）
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if os.path.exists(logo_path):
        fg.image(url=base_link.rstrip("/") + "/logo.png", title=feed_title, link=base_link)

    for article in articles:
        title_cn = (article.get("translated_title") or "").strip()
        title_en = (article.get("title") or "").strip()
        title = title_cn or title_en
        link = article.get("url", "")

        fe = fg.add_entry()
        fe.id(link or f"mcttk-{re.sub(r'[^a-zA-Z0-9]', '', title_en)[:50]}")
        fe.title(title)
        fe.link(href=link or base_link)

        # 摘要
        description = (article.get("description") or "").strip()
        if not description:
            translated = (article.get("translated_content") or "").strip()
            description = translated[:200] + "…" if len(translated) > 200 else translated
        if description:
            fe.description(description)

        # 发布日期
        dt = _parse_date(article.get("release_date", ""))
        if dt:
            fe.published(dt)
            fe.updated(dt)

        # content:encoded 全文
        content_html = _build_content_html(article, link)
        if content_html:
            fe.content(content_html, type="html")

        # 作者
        author = (article.get("author") or "").strip()
        if author:
            fe.author({"name": author})

    # 确保输出目录存在
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    rss_xml = fg.rss_str(pretty=True)
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
        site_base=site_base,
    )


if __name__ == "__main__":
    main()
