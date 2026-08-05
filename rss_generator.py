#!/usr/bin/env python3
"""rss_generator.py — 从 output 目录的 JSON 文章生成 RSS 2.0 Feed

用法：
  python rss_generator.py                    # 扫描 output/ 生成 feed.xml
  python rss_generator.py --dir output --out feed.xml

也可作为模块导入：
  from rss_generator import generate_rss
"""
import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import formatdate
from xml.sax.saxutils import escape

# 东八区时区
_TZ_CN = timezone(timedelta(hours=8))


def _parse_date_to_dt(date_str: str) -> datetime | None:
    """将多种日期格式解析为 datetime，失败返回 None"""
    if not date_str:
        return None
    s = date_str.strip()
    # ISO 8601 (含时区)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_TZ_CN)
    except ValueError:
        pass
    # "24 July 2025" / "July 24, 2025"
    for fmt in ("%d %B %Y", "%B %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_TZ_CN)
        except ValueError:
            continue
    return None


def _dt_to_rfc822(dt: datetime) -> str:
    """datetime 转 RFC 822 格式字符串"""
    return formatdate(float(dt.replace(tzinfo=UTC).timestamp()), usegmt=True)


def _blocks_to_html(blocks: list[dict]) -> str:
    """将文章 blocks 渲染为简单 HTML 用于 RSS content:encoded"""
    if not blocks or not isinstance(blocks, list):
        return ""
    html_parts = []
    for block in blocks:
        btype = block.get("type", "p").lower()
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
            meta = block.get("meta", {})
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


def _load_articles_from_dir(save_dir: str, max_items: int = 50) -> list[dict]:
    """
    从目录加载所有 news_*.json 文章，按发布日期降序排列。

    Returns:
        排序后的文章数据字典列表
    """
    import glob

    json_files = glob.glob(os.path.join(save_dir, "news_*.json"))
    articles = []
    for jf in json_files:
        # 跳过隐藏状态文件
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

    # 按日期降序排列（无日期的放最后）
    def sort_key(a):
        dt = _parse_date_to_dt(a.get("release_date", ""))
        return (dt is not None, dt or datetime.min.replace(tzinfo=_TZ_CN))
    articles.sort(key=sort_key, reverse=True)

    return articles[:max_items]


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
    articles = _load_articles_from_dir(save_dir, max_items)
    now_dt = datetime.now(_TZ_CN)
    last_build = _dt_to_rfc822(now_dt)
    base_link = feed_link or site_base

    items_xml = []
    for article in articles:
        title_cn = (article.get("translated_title") or "").strip()
        title_en = (article.get("title") or "").strip()
        title = title_cn or title_en
        link = escape(article.get("url", ""))
        release_date = article.get("release_date", "")
        dt = _parse_date_to_dt(release_date)
        pub_date = _dt_to_rfc822(dt) if dt else last_build

        # 摘要：使用 description 或翻译内容前 200 字
        description = (article.get("description") or "").strip()
        if not description:
            translated = (article.get("translated_content") or "").strip()
            description = translated[:200] + "…" if len(translated) > 200 else translated
        description = escape(description)

        # 完整内容 HTML
        content_html = _blocks_to_html(article.get("blocks", []))
        # 添加元数据
        author = escape(article.get("author", "")) if article.get("author") else ""
        meta_parts = []
        if title_en and title_en != title_cn:
            meta_parts.append(f"<p><strong>原标题：</strong>{escape(title_en)}</p>")
        if author:
            meta_parts.append(f"<p><strong>作者：</strong>{author}</p>")
        if link:
            meta_parts.append(f'<p><strong>原文：</strong><a href="{link}">{link}</a></p>')
        full_html = "\n".join(meta_parts) + content_html if content_html else "\n".join(meta_parts)

        guid = f"mcttk-{re.sub(r'[^a-zA-Z0-9]', '', title_en)[:50]}-{re.sub(r'[^0-9]', '', release_date)[:20]}"
        if not guid:
            guid = f"mcttk-{link}"

        item = f"""    <item>
      <title>{escape(title)}</title>
      <link>{link}</link>
      <description>{description}</description>
      <content:encoded><![CDATA[{full_html}]]></content:encoded>
      <pubDate>{pub_date}</pubDate>
      <guid isPermaLink="false">{escape(guid)}</guid>
    </item>"""
        items_xml.append(item)

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(feed_title)}</title>
    <link>{escape(base_link)}</link>
    <description>{escape(feed_description)}</description>
    <language>zh-CN</language>
    <lastBuildDate>{last_build}</lastBuildDate>
    <generator>MCTTK2RSS</generator>
    <atom:link href="{escape(base_link.rstrip('/') + '/feed.xml')}" rel="self" type="application/rss+xml" />
{chr(10).join(items_xml)}
  </channel>
</rss>
"""

    # 确保输出目录存在
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print(f"[RSS] 生成 {output_path}（{len(articles)} 篇文章）")
    return rss_xml


def main():
    parser = argparse.ArgumentParser(description="从 output 目录的 JSON 生成 RSS Feed")
    parser.add_argument("--dir", default="output", help="文章 JSON 目录（默认: output）")
    parser.add_argument("--out", default="feed.xml", help="输出 RSS XML 路径（默认: feed.xml）")
    parser.add_argument("--title", default="Minecraft News (中文翻译)", help="Feed 标题")
    parser.add_argument("--link", default="", help="Feed 链接（如 GitHub Pages URL）")
    parser.add_argument("--description", default="Minecraft 官方新闻与更新日志的中文翻译 RSS", help="Feed 描述")
    parser.add_argument("--max-items", type=int, default=50, help="最大条目数")
    args = parser.parse_args()

    # 加载 config.json 获取站点基础 URL
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
        feed_title=args.title or rss_config.get("feed_title", args.title),
        feed_link=args.link or rss_config.get("feed_link", ""),
        feed_description=args.description or rss_config.get("feed_description", args.description),
        max_items=args.max_items or rss_config.get("max_items", 50),
        site_base=site_base,
    )


if __name__ == "__main__":
    main()
