"""test_version_manifest.py — Piston 版本清单发现与类型过滤测试

用 Mojang 版本清单补齐 Java 版本日志（不发起真实网络请求）。
URL 规则经实测确认：
  快照   -> minecraft-{ver}-snapshot-{n}
  预发布 -> minecraft-{ver}-pre-release-{n}
  RC     -> minecraft-{ver}-release-candidate-{n}
  正式版 -> minecraft-java-edition-{ver}
候选文章页会先做 HEAD 探测：只有 404/410 才跳过（清单常早于官网文章页更新），
网络错误或其它状态码按「存在」放行，避免探测本身吞掉候选。`max_versions` 是
探测次数上限（不是产出条数）：窗口内未上线的候选下一轮会重新探测。
"""
import copy

import requests

import scraper
from scraper import (
    DEFAULT_CONFIG,
    _classify_version_type,
    _version_id_to_url,
    get_java_news_from_manifest,
)

PREFIX = "https://www.minecraft.net/zh-hans/article/"


def _cfg(**manifest_overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["version_manifest"].update(manifest_overrides)
    return cfg


class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ── _version_id_to_url ───────────────────────────────

class TestVersionIdToUrl:
    def test_snapshot_slug(self):
        url = _version_id_to_url("26.4-snapshot-1", "java_snapshot", _cfg())
        assert url == PREFIX + "minecraft-26-4-snapshot-1"

    def test_release_uses_java_edition_slug(self):
        url = _version_id_to_url("26.3", "java_release", _cfg())
        assert url == PREFIX + "minecraft-java-edition-26-3"

    def test_dots_become_hyphens(self):
        url = _version_id_to_url("1.21.5", "java_release", _cfg())
        assert url.endswith("/minecraft-java-edition-1-21-5")

    def test_snapshot_dots_become_hyphens(self):
        url = _version_id_to_url("26.3-snapshot-7", "java_snapshot", _cfg())
        assert url.endswith("/minecraft-26-3-snapshot-7")

    def test_prerelease_expands_pre(self):
        url = _version_id_to_url("26.4-pre-2", "java_prerelease", _cfg())
        assert url == PREFIX + "minecraft-26-4-pre-release-2"

    def test_prerelease_without_separator(self):
        # 老版本号是 1.21.5-pre1，没有第二个连字符
        url = _version_id_to_url("1.21.5-pre1", "java_prerelease", _cfg())
        assert url == PREFIX + "minecraft-1-21-5-pre-release-1"

    def test_rc_expands_release_candidate(self):
        url = _version_id_to_url("26.3-rc-3", "java_rc", _cfg())
        assert url == PREFIX + "minecraft-26-3-release-candidate-3"

    def test_rc_without_separator(self):
        url = _version_id_to_url("1.21.5-rc1", "java_rc", _cfg())
        assert url == PREFIX + "minecraft-1-21-5-release-candidate-1"

    def test_unmapped_type_returns_none(self):
        assert _version_id_to_url("26.3-rc-3", "java_other", _cfg()) is None
        cfg = _cfg(url_templates={"java_release": "https://example.com/{version_id}"})
        assert _version_id_to_url("26.4-pre-2", "java_prerelease", cfg) is None

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
        assert _classify_version_type("26.4-pre-2", "snapshot") == "java_prerelease"

    def test_release_candidate(self):
        assert _classify_version_type("1.21.5-rc1", "snapshot") == "java_rc"
        # 官网真实版本号形如 26.3-rc-3
        assert _classify_version_type("26.3-rc-3", "snapshot") == "java_rc"


# ── get_java_news_from_manifest ──────────────────────

class TestGetJavaNewsFromManifest:
    _PAYLOAD = {
        "versions": [
            {"id": "26.4-pre-2", "type": "snapshot"},
            {"id": "26.4-pre-1", "type": "snapshot"},
            {"id": "26.4-snapshot-10", "type": "snapshot"},
            {"id": "26.4-snapshot-9", "type": "snapshot"},
            {"id": "26.3-rc-3", "type": "snapshot"},
            {"id": "26.3", "type": "release"},
            {"id": "26.2-pre1", "type": "snapshot"},
            {"id": "26.2-rc1", "type": "snapshot"},
            {"id": "b1.7.3", "type": "old_beta"},
            {"id": "a1.2.6", "type": "old_alpha"},
        ]
    }

    # 26.4 Snapshot 10 的官网 slug 其实不存在（文章挂在 snapshot-9 下），
    # 用来覆盖「盲拼 URL 得到 404」的场景
    MISSING_URL = PREFIX + "minecraft-26-4-snapshot-10"

    def _patch(self, monkeypatch, *, payload=None, get_error=None,
               head_status=200, head_error=None, calls=None):
        def fake_get(url, **kwargs):
            if get_error:
                raise get_error
            return _FakeResponse(payload if payload is not None else self._PAYLOAD)

        def fake_head(url, **kwargs):
            if calls is not None:
                calls.append(url)
            if head_error:
                raise head_error
            if callable(head_status):
                status = head_status(url)
            elif isinstance(head_status, dict):
                status = head_status.get(url, 200)
            else:
                status = head_status
            return _FakeResponse(status_code=status)

        monkeypatch.setattr(scraper.requests, "get", fake_get)
        monkeypatch.setattr(scraper.requests, "head", fake_head)

    def test_builds_candidates(self, monkeypatch):
        self._patch(monkeypatch)
        news = get_java_news_from_manifest(config=_cfg(max_versions=5))
        assert [n["_version_type"] for n in news] == [
            "java_prerelease",
            "java_prerelease",
            "java_snapshot",
            "java_snapshot",
            "java_rc",
        ]
        assert news[0]["title"] == "Minecraft 26.4-pre-2"
        assert "_source" not in news[0], "_source 由 main.py 负责标注"

    def test_urls_use_correct_slug_per_type(self, monkeypatch):
        self._patch(monkeypatch)
        urls = [n["url"] for n in get_java_news_from_manifest(config=_cfg(max_versions=5))]
        assert urls[0] == PREFIX + "minecraft-26-4-pre-release-2"
        assert urls[3] == PREFIX + "minecraft-26-4-snapshot-9"
        assert urls[4] == PREFIX + "minecraft-26-3-release-candidate-3"

    def test_drops_candidate_when_article_page_is_404(self, monkeypatch):
        self._patch(monkeypatch, head_status={self.MISSING_URL: 404})
        news = get_java_news_from_manifest(config=_cfg(max_versions=5))
        urls = [n["url"] for n in news]
        assert self.MISSING_URL not in urls
        # 被跳过的那次探测也占名额（最多 HEAD 5 次），所以只得到 4 条
        assert len(urls) == 4
        assert PREFIX + "minecraft-java-edition-26-3" not in urls

    def test_probe_count_is_capped_by_max_versions(self, monkeypatch):
        calls = []
        self._patch(monkeypatch, head_status=404, calls=calls)
        assert get_java_news_from_manifest(config=_cfg(max_versions=3)) == []
        assert len(calls) == 3, "max_versions 是探测次数上限，不能扫到清单尾部"

    def test_unavailable_candidate_is_reprobed_on_next_scan(self, monkeypatch):
        statuses = {self.MISSING_URL: 404}
        self._patch(monkeypatch, head_status=statuses)
        first = [n["url"] for n in get_java_news_from_manifest(config=_cfg(max_versions=5))]
        assert self.MISSING_URL not in first

        # 下一轮文章页上线后，同一个候选仍在探测窗口内，应当被重新发现
        statuses[self.MISSING_URL] = 200
        second = [n["url"] for n in get_java_news_from_manifest(config=_cfg(max_versions=5))]
        assert self.MISSING_URL in second

    def test_unmapped_types_do_not_consume_probe_budget(self, monkeypatch):
        self._patch(monkeypatch)
        cfg = _cfg(max_versions=1,
                   url_templates={"java_release": PREFIX + "minecraft-java-edition-{version_id}"})
        news = get_java_news_from_manifest(config=cfg)
        assert [n["_version_type"] for n in news] == ["java_release"]

    def test_probe_network_error_keeps_candidate(self, monkeypatch):
        # 探测失败（代理/TLS/超时）不能把候选吞掉，交给后续解析再判断
        self._patch(monkeypatch, head_error=requests.exceptions.ConnectionError("boom"))
        urls = [n["url"] for n in get_java_news_from_manifest(config=_cfg(max_versions=5))]
        assert self.MISSING_URL in urls

    def test_probe_other_status_keeps_candidate(self, monkeypatch):
        # 403 反爬之类不能当成「文章不存在」
        self._patch(monkeypatch, head_status={self.MISSING_URL: 403})
        urls = [n["url"] for n in get_java_news_from_manifest(config=_cfg(max_versions=5))]
        assert self.MISSING_URL in urls

    def test_skips_types_without_url_template(self, monkeypatch):
        self._patch(monkeypatch)
        cfg = _cfg(max_versions=10,
                   url_templates={"java_release": PREFIX + "minecraft-java-edition-{version_id}"})
        news = get_java_news_from_manifest(config=cfg)
        assert [n["_version_type"] for n in news] == ["java_release"]

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
        self._patch(monkeypatch, get_error=requests.exceptions.ConnectionError("boom"))
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
