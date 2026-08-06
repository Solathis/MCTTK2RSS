import json

from main import _filter_and_check_state, _filter_feedback_by_sections, filter_news_by_types


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


def test_skip_first_run_protection_consumes_first_run_marker(tmp_path):
    state_file = tmp_path / ".state.json"
    state_file.write_text(json.dumps({"_first_run": True, "posted_urls": []}), encoding="utf-8")
    config = {
        "first_run_protection": False,
        "output": {"save_dir": str(tmp_path)},
        "news_types": {"java_release": True, "other": False},
        "feedback_site": {"sections": []},
    }
    news = [{
        "_source": "minecraft_api",
        "title": "Minecraft Java Edition 1.21.8",
        "url": "https://www.minecraft.net/article/1.21.8",
    }]

    result = _filter_and_check_state(news, config, str(state_file))

    assert result[0] == news
    assert "_first_run" not in json.loads(state_file.read_text(encoding="utf-8"))

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
