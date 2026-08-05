from main import _filter_feedback_by_sections, filter_news_by_types


def test_filter_news_by_types_skips_unknown_articles_in_java_only_mode():
    config = {
        "news_types": {
            "java_release": True,
            "java_snapshot": True,
            "java_prerelease": True,
            "java_rc": True,
            "bedrock_release": False,
            "bedrock_beta": False,
            "other": False,
        }
    }
    news = [
        {"title": "Minecraft Java Edition 1.21.8"},
        {"title": "Minecraft Bedrock Edition 1.21.80"},
        {"title": "Minecraft Dungeons Update"},
    ]

    result = filter_news_by_types(news, config)

    assert [item["title"] for item in result] == ["Minecraft Java Edition 1.21.8"]


def test_filter_feedback_by_sections_uses_enabled_section_allowlist():
    config = {
        "feedback_site": {
            "sections": [
                {"name": "Snapshot Information and Changelogs", "enabled": True},
                {"name": "Release Changelogs", "enabled": False},
            ]
        }
    }
    news = [
        {"section": "Snapshot Information and Changelogs"},
        {"section": "Release Changelogs"},
        {"section": "Minecraft Education Changelogs"},
    ]

    assert _filter_feedback_by_sections(news, config) == [news[0]]
