import json

from rss_generator import _blocks_to_html, _distinct_translation, _is_short_translation, generate_rss


def test_generate_rss_renders_html_images_highlights_and_unicode(tmp_path):
    article = {
        "title": "Original 😀",
        "translated_title": "测试 😀",
        "release_date": "2026-01-01",
        "url": "https://example.com/article",
        "header_image_url": "https://example.com/image.png",
        "blocks": [
            {
                "type": "p",
                "source_text": "**Original** [link](https://example.com) 😀",
                "translated_text": "**高亮** [链接](https://example.com) 😀",
            },
            {
                "type": "li",
                "source_text": "Original item",
                "translated_text": "列表项",
            },
            {
                "type": "img",
                "source_text": "",
                "translated_text": "",
                "meta": {"src": "https://example.com/image.png", "alt": "示例图片"},
            },
        ],
    }
    article_path = tmp_path / "news_test.json"
    article_path.write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
    feed_path = tmp_path / "feed.xml"

    xml = generate_rss(
        save_dir=str(tmp_path),
        output_path=str(feed_path),
        feed_link="https://solathis.github.io/MCTTK2RSS",
        max_age_days=365,
    )
    assert xml.startswith("<?xml")
    assert xml.rstrip().endswith("</rss>")

    assert "<![CDATA[" in xml
    assert "&lt;p&gt;" not in xml
    assert "](" not in xml
    assert "<strong>高亮</strong>" in xml
    assert '<a href="https://example.com">链接</a>' in xml
    assert "<ul>" in xml and "<li>" in xml
    assert "color:#333" in xml and "color:#999" in xml
    assert 'src="https://example.com/image.png"' in xml
    assert xml.count('src="https://example.com/image.png"') == 1
    assert "<atom:logo>https://solathis.github.io/MCTTK2RSS/logo.png</atom:logo>" in xml
    assert "😀" in xml
    assert "https://solathis.github.io/MCTTK2RSS/logo.png" in xml


def test_expired_articles_are_excluded(tmp_path):
    article = {
        "title": "Old article",
        "translated_title": "旧文章",
        "release_date": "2020-01-01T00:00:00Z",
        "url": "https://example.com/old",
        "blocks": [],
    }
    (tmp_path / "news_old.json").write_text(json.dumps(article), encoding="utf-8")

    xml = generate_rss(
        save_dir=str(tmp_path),
        output_path=str(tmp_path / "feed.xml"),
        max_age_days=365,
    )

    assert "Old article" not in xml
    assert "旧文章" not in xml



    html = _blocks_to_html([
        {"type": "h2", "source_text": "Snapshot", "translated_text": "快照"},
    ])

    assert "Snapshot <span" in html
    assert "（快照）" in html
    assert html.count("<h2") == 1


def test_translation_word_limit_uses_words_not_characters():
    assert _is_short_translation("one two three")
    assert _is_short_translation("一二三四五六七八九")
    assert not _is_short_translation("one two three four five six seven eight nine ten")


def test_identical_translation_is_not_rendered():
    html = _blocks_to_html([
        {"type": "p", "source_text": "same text", "translated_text": "same text"},
        {"type": "pre", "source_text": "code", "translated_text": "code"},
    ])

    assert _distinct_translation("same text", "same text") == ""
    assert "（same text）" not in html
    assert html.count("color:#999") == 0
    assert html.count("<pre") == 1


def test_articles_ordered_newest_first(tmp_path):
    """RSS 中最新文章应排在最上方。"""
    articles = [
        {"title": "Old", "translated_title": "旧", "release_date": "2026-06-01T00:00:00Z",
         "url": "https://example.com/old", "blocks": []},
        {"title": "Newest", "translated_title": "最新", "release_date": "2026-08-06T12:00:00Z",
         "url": "https://example.com/new", "blocks": []},
        {"title": "Mid", "translated_title": "中间", "release_date": "2026-07-15T00:00:00Z",
         "url": "https://example.com/mid", "blocks": []},
    ]
    for i, a in enumerate(articles):
        (tmp_path / f"news_{i}.json").write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")

    xml = generate_rss(
        save_dir=str(tmp_path),
        output_path=str(tmp_path / "feed.xml"),
        max_age_days=365,
    )

    # XML 中第一个 <item> 应是最新的，最后一个是最旧的
    pos_new = xml.find("https://example.com/new")
    pos_mid = xml.find("https://example.com/mid")
    pos_old = xml.find("https://example.com/old")
    assert 0 < pos_new < pos_mid < pos_old, "文章应按发布日期降序排列（最新在上）"
