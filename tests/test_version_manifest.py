"""test_version_manifest.py — Piston 版本清单发现与类型过滤测试

用 Mojang 版本清单补齐 Java 版本日志（不发起真实网络请求）。
URL 规则经实测确认：
  快照   -> minecraft-{ver}-snapshot-{n}
  正式版 -> minecraft-java-edition-{ver}
  RC/预发布 -> 官网无对应文章页，必须跳过，否则会产生 404 候选
"""
import copy

import scraper
from scraper import (
    DEFAULT_CONFIG,
    _classify_version_type,
    _version_id_to_url,
    get_java_news_from_manifest,
)


def _cfg(**manifest_overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["version_manifest"].update(manifest_overrides)
    return cfg


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ── _version_id_to_url ───────────────────────────────

class TestVersionIdToUrl:
    def test_snapshot_slug(self):
        url = _version_id_to_url("26.4-snapshot-1", "java_snapshot", _cfg())
        assert url == "https://www.minecraft.net/zh-hans/article/minecraft-26-4-snapshot-1"

    def test_release_uses_java_edition_slug(self):
        url = _version_id_to_url("26.3", "java_release", _cfg())
        assert url == "https://www.minecraft.net/zh-hans/article/minecraft-java-edition-26-3"

    def test_dots_become_hyphens(self):
        url = _version_id_to_url("1.21.5", "java_release", _cfg())
        assert url.endswith("/minecraft-java-edition-1-21-5")

    def test_snapshot_dots_become_hyphens(self):
        url = _version_id_to_url("26.3-snapshot-7", "java_snapshot", _cfg())
        assert url.endswith("/minecraft-26-3-snapshot-7")

    def test_unmapped_type_returns_none(self):
        assert _version_id_to_url("26.3-rc-3", "java_rc", _cfg()) is None
        assert _version_id_to_url("26.3-pre-1", "java_prerelease", _cfg()) is None

    def test_custom_template_map(self):
        cfg = _cfg(url_templates={"java_release": "https://example.com/{version_id}"})
        assert _version_id_to_url("1.2", "java_release", cfg) == "https://example.com/1-2"


# ── _classify_version_type ───────────────────────────

class TestClassifyVersionType:
    def test_release(self):
        assert _classify_version_type("26.2", "release") == "java_release"

    def test_snapshot(self):
        assert _classify_version_type("26.3-snapshot-7", "snapshot") == "java_snapshot"

    def test_prerelease(self):
        assert _classify_version_type("1.21.5-pre1", "snapshot") == "java_prerelease"

    def test_release_candidate(self):
        assert _classify_version_type("1.21.5-rc1", "snapshot") == "java_rc"

    def test_real_world_release_candidate(self):
        # 官网真实版本号形如 26.3-rc-3
        assert _classify_version_type("26.3-rc-3", "snapshot") == "java_rc"


# ── get_java_news_from_manifest ──────────────────────

class TestGetJavaNewsFromManifest:
    _PAYLOAD = {
        "versions": [
            {"id": "26.3-snapshot-7", "type": "snapshot"},
            {"id": "26.2", "type": "release"},
            {"id": "26.2-pre1", "type": "snapshot"},
            {"id": "26.2-rc1", "type": "snapshot"},
            {"id": "26.1", "type": "release"},
            {"id": "b1.7.3", "type": "old_beta"},
            {"id": "a1.2.6", "type": "old_alpha"},
        ]
    }

    def _patch(self, monkeypatch, payload=None, error=None):
        def fake_get(url, **kwargs):
            if error:
                raise error
            return _FakeResponse(payload if payload is not None else self._PAYLOAD)

        monkeypatch.setattr(scraper.requests, "get", fake_get)

    def test_builds_candidates(self, monkeypatch):
        self._patch(monkeypatch)
        news = get_java_news_from_manifest(config=_cfg(max_versions=5))
        assert [n["_version_type"] for n in news] == [
            "java_snapshot",
            "java_release",
            "java_release",
        ]
        assert news[0]["title"] == "Minecraft 26.3-snapshot-7"
        assert "_source" not in news[0], "_source 由 main.py 负责标注"

    def test_urls_use_correct_slug_per_type(self, monkeypatch):
        self._patch(monkeypatch)
        news = get_java_news_from_manifest(config=_cfg(max_versions=5))
        assert news[0]["url"].endswith("/minecraft-26-3-snapshot-7")
        assert news[1]["url"].endswith("/minecraft-java-edition-26-2")

    def test_skips_types_without_url_template(self, monkeypatch):
        self._patch(monkeypatch)
        news = get_java_news_from_manifest(config=_cfg(max_versions=10))
        types = {n["_version_type"] for n in news}
        assert "java_rc" not in types, "RC 没有文章页，不应生成 404 候选"
        assert "java_prerelease" not in types, "预发布没有文章页，不应生成 404 候选"

    def test_skips_old_beta_and_alpha(self, monkeypatch):
        self._patch(monkeypatch)
        news = get_java_news_from_manifest(config=_cfg(max_versions=10))
        urls = " ".join(n["url"] for n in news)
        assert "b1-7-3" not in urls
        assert "a1-2-6" not in urls

    def test_respects_max_versions(self, monkeypatch):
        self._patch(monkeypatch)
        assert len(get_java_news_from_manifest(config=_cfg(max_versions=2))) == 2

    def test_disabled_returns_empty(self, monkeypatch):
        self._patch(monkeypatch)
        assert get_java_news_from_manifest(config=_cfg(enabled=False)) == []

    def test_network_error_returns_empty(self, monkeypatch):
        import requests

        self._patch(monkeypatch, error=requests.exceptions.ConnectionError("boom"))
        assert get_java_news_from_manifest(config=_cfg()) == []


# ── main.filter_news_by_types 使用 _version_type ──────

class TestFilterUsesVersionType:
    def test_manifest_item_filtered_by_version_type(self):
        from main import filter_news_by_types

        config = {"news_types": {"java_snapshot": True, "other": False}}
        news = [{"title": "Minecraft 26.2", "_version_type": "java_snapshot"}]
        assert filter_news_by_types(news, config) == news

    def test_manifest_item_excluded_when_type_disabled(self):
        from main import filter_news_by_types

        config = {"news_types": {"java_snapshot": False, "other": False}}
        news = [{"title": "Minecraft 26.2", "_version_type": "java_snapshot"}]
        assert filter_news_by_types(news, config) == []

    def test_without_version_type_title_classification_still_used(self):
        from main import filter_news_by_types

        config = {"news_types": {"java_snapshot": True, "other": False}}
        news = [{"title": "Minecraft 1.21.5 Snapshot"}]
        assert filter_news_by_types(news, config) == news
