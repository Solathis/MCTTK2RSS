import json

from rss_generator import generate_rss


def test_generate_rss_renders_html_images_highlights_and_unicode(tmp_path):
    article = {
        "title": "Original 😀",
        "translated_title": "测试 😀",
        "release_date": "2026-01-01",
        "url": "https://example.com/article",
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
    )
    assert xml.startswith("<?xml")
    assert xml.rstrip().endswith("</rss>")

    assert "<![CDATA[" in xml
    assert "&lt;p&gt;" not in xml
    assert "<strong>高亮</strong>" in xml
    assert '<a href="https://example.com">链接</a>' in xml
    assert "<ul>" in xml and "<li>列表项</li>" in xml
    assert 'src="https://example.com/image.png"' in xml
    assert "😀" in xml
    assert "https://solathis.github.io/MCTTK2RSS/logo.png" in xml
