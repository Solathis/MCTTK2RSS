"""test_scraper.py — scraper.py 纯逻辑单元测试

不测试网络请求、翻译 API、文件 I/O。
只测试可独立运行的纯函数。
"""
import pytest

from scraper import (
    _chunk_items_for_translation,
    _classify_version_type,
    _deep_merge,
    _is_recent_article,
    _normalize_whitespace,
    _parse_pattern,
    _version_id_to_url,
    blocks_to_plaintext,
    build_glossary_prompt,
    classify_news_type,
    find_relevant_terms,
    get_java_news_from_manifest,
    load_config,
    load_glossary,
    reindex_blocks,
)

# ── _deep_merge ──────────────────────────────────────

class TestDeepMerge:
    def test_basic(self):
        a = {"x": 1, "y": 2}
        b = {"y": 3, "z": 4}
        result = _deep_merge(a, b)
        assert result == {"x": 1, "y": 3, "z": 4}

    def test_nested(self):
        a = {"http": {"timeout": 30, "verify": True}}
        b = {"http": {"timeout": 60}}
        result = _deep_merge(a, b)
        assert result["http"]["timeout"] == 60
        assert result["http"]["verify"] is True

    def test_empty_b(self):
        a = {"x": 1}
        assert _deep_merge(a, {}) == {"x": 1}

    def test_none_b(self):
        a = {"x": 1}
        assert _deep_merge(a, None) == {"x": 1}

    def test_does_not_mutate_a(self):
        a = {"x": 1}
        b = {"x": 2}
        _deep_merge(a, b)
        assert a["x"] == 1


# ── _normalize_whitespace ────────────────────────────

class TestNormalizeWhitespace:
    def test_multiple_spaces(self):
        assert _normalize_whitespace("a  b   c") == "a b c"

    def test_tabs_newlines(self):
        assert _normalize_whitespace("a\t\nb") == "a b"

    def test_nbsp_preserved(self):
        # NBSP 被还原为普通空格（函数通过占位符机制处理后合并）
        result = _normalize_whitespace("a\u00A0b")
        assert result == "a b"

    def test_empty(self):
        assert _normalize_whitespace("") == ""

    def test_none(self):
        assert _normalize_whitespace(None) == ""

    def test_strip(self):
        assert _normalize_whitespace("  hello  ") == "hello"


# ── classify_news_type ───────────────────────────────

class TestClassifyNewsType:
    @pytest.mark.parametrize("title,expected", [
        ("Minecraft Java Edition Snapshot 24w10a", "java_snapshot"),
        ("Minecraft Java Edition 1.21 Pre-Release 1", "java_prerelease"),
        ("Minecraft Java Edition 1.21 Prerelease 1", "java_prerelease"),
        ("Minecraft Java Edition 1.21 Release Candidate 1", "java_rc"),
        ("Minecraft Beta & Preview 1.21.0.20", "bedrock_beta"),
        ("Minecraft Preview 1.21.0.20", "bedrock_beta"),
        ("Minecraft Bedrock Edition 1.21", "bedrock_release"),
        ("Minecraft Java Edition 1.21", "java_release"),
        ("Minecraft 1.21 Java Edition", "java_release"),
        ("普通新闻", "other"),
        ("", "other"),
    ])
    def test_classify(self, title, expected):
        assert classify_news_type(title) == expected

    def test_case_insensitive(self):
        assert classify_news_type("SNAPSHOT 24w10a") == "java_snapshot"
        assert classify_news_type("BEDROCK EDITION 1.21") == "bedrock_release"


# ── _parse_pattern ───────────────────────────────────

class TestParsePattern:
    def test_exact_match(self):
        base, pattern, has_optional = _parse_pattern("Snapshot")
        assert base == "Snapshot"
        assert has_optional is False

    def test_wildcard(self):
        base, pattern, has_optional = _parse_pattern("baby *")
        assert "baby" in base
        assert has_optional is False

    def test_optional_suffix(self):
        base, pattern, has_optional = _parse_pattern("undead * (mobs)")
        assert has_optional is True
        assert "undead" in base

    def test_no_wildcard_no_optional(self):
        base, pattern, has_optional = _parse_pattern("Java Edition")
        assert base == "Java Edition"
        assert has_optional is False


# ── find_relevant_terms ──────────────────────────────

class TestFindRelevantTerms:
    def setup_method(self):
        self.glossary = {
            "terms": {
                "Snapshot": "快照",
                "Java Edition": "Java版",
                "Bedrock Edition": "基岩版",
                "baby *": "幼年",
                "undead * (mobs)": "亡灵",
            },
            "placeholders": {"(*)": "[英文原词]"}
        }

    def test_exact_match(self):
        result = find_relevant_terms("This is a Snapshot release", self.glossary)
        assert "Snapshot" in result

    def test_no_match(self):
        result = find_relevant_terms("普通文字", self.glossary)
        assert result == {}

    def test_multiple_matches(self):
        result = find_relevant_terms("Java Edition Snapshot", self.glossary)
        assert len(result) >= 2

    def test_empty_text(self):
        assert find_relevant_terms("", self.glossary) == {}

    def test_empty_glossary(self):
        assert find_relevant_terms("Snapshot", {}) == {}

    def test_wildcard_match(self):
        result = find_relevant_terms("baby zombie", self.glossary)
        assert "baby" in result

    def test_no_overlap(self):
        # Java Edition 和 Bedrock Edition 不应重叠
        result = find_relevant_terms("Java Edition and Bedrock Edition", self.glossary)
        assert "Java Edition" in result
        assert "Bedrock Edition" in result


# ── build_glossary_prompt ────────────────────────────

class TestBuildGlossaryPrompt:
    def test_basic(self):
        terms = {"Snapshot": "快照", "Java Edition": "Java版"}
        result = build_glossary_prompt(terms)
        assert "Snapshot" in result
        assert "快照" in result
        assert "Java Edition" in result

    def test_empty_terms(self):
        assert build_glossary_prompt({}) == ""

    def test_placeholder_star(self):
        terms = {"Minecraft World": "MC乐园(*)"}
        placeholders = {"(*)": "玖布克"}
        result = build_glossary_prompt(terms, placeholders)
        assert "Minecraft World" in result
        assert "(Minecraft World)" in result

    def test_format(self):
        terms = {"Snapshot": "快照"}
        result = build_glossary_prompt(terms)
        assert "→" in result
        assert "专业术语对照" in result


# ── _chunk_items_for_translation ─────────────────────

class TestChunkItems:
    def test_basic_split(self):
        items = [{"id": f"t{i:04d}", "text": "x" * 100} for i in range(20)]
        batches = _chunk_items_for_translation(items, max_chars=500, max_items=5)
        assert len(batches) > 1
        for batch in batches:
            assert len(batch) <= 5

    def test_single_batch(self):
        items = [{"id": "t0000", "text": "short"}]
        batches = _chunk_items_for_translation(items, max_chars=1000, max_items=10)
        assert len(batches) == 1
        assert batches[0] == items

    def test_empty(self):
        assert _chunk_items_for_translation([], max_chars=1000, max_items=10) == []

    def test_max_items_respected(self):
        items = [{"id": f"t{i:04d}", "text": "a"} for i in range(25)]
        batches = _chunk_items_for_translation(items, max_chars=100000, max_items=10)
        for batch in batches:
            assert len(batch) <= 10

    def test_all_items_preserved(self):
        items = [{"id": f"t{i:04d}", "text": f"text{i}"} for i in range(15)]
        batches = _chunk_items_for_translation(items, max_chars=200, max_items=5)
        all_items = [item for batch in batches for item in batch]
        assert len(all_items) == 15


# ── blocks_to_plaintext ──────────────────────────────

class TestBlocksToPlaintext:
    def test_basic(self):
        blocks = [
            {"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}},
            {"type": "p", "source_text": "World", "translated_text": "世界", "meta": {}},
        ]
        result = blocks_to_plaintext(blocks, field="source_text")
        assert "Hello" in result
        assert "World" in result

    def test_translated(self):
        blocks = [{"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}}]
        result = blocks_to_plaintext(blocks, field="translated_text")
        assert "你好" in result
        assert "Hello" not in result

    def test_img_block(self):
        blocks = [{"type": "img", "source_text": "", "translated_text": "",
                   "meta": {"src": "http://img.com/a.png", "alt": "图片"}}]
        result = blocks_to_plaintext(blocks, field="source_text")
        assert "http://img.com/a.png" in result
        assert "图片" in result

    def test_img_no_src(self):
        blocks = [{"type": "img", "source_text": "", "translated_text": "", "meta": {"src": "", "alt": "描述"}}]
        result = blocks_to_plaintext(blocks, field="source_text")
        assert result == ""

    def test_empty_blocks(self):
        assert blocks_to_plaintext([], field="source_text") == ""

    def test_none_blocks(self):
        assert blocks_to_plaintext(None, field="source_text") == ""

    def test_skip_empty_text(self):
        blocks = [
            {"type": "p", "source_text": "", "translated_text": "", "meta": {}},
            {"type": "p", "source_text": "Hello", "translated_text": "你好", "meta": {}},
        ]
        result = blocks_to_plaintext(blocks, field="source_text")
        assert result == "Hello"


# ── reindex_blocks ───────────────────────────────────

class TestReindexBlocks:
    def test_basic(self):
        blocks = [
            {"id": "b9999", "type": "p", "source_text": "a"},
            {"id": "b0001", "type": "p", "source_text": "b"},
        ]
        result = reindex_blocks(blocks)
        assert result[0]["id"] == "b0001"
        assert result[1]["id"] == "b0002"

    def test_empty(self):
        assert reindex_blocks([]) == []

    def test_single(self):
        blocks = [{"id": "old", "type": "p"}]
        result = reindex_blocks(blocks)
        assert result[0]["id"] == "b0001"

    def test_mutates_in_place(self):
        blocks = [{"id": "old", "type": "p"}]
        result = reindex_blocks(blocks)
        assert result is blocks  # 原地修改


# ── load_config ────────���─────────────────────────────

class TestLoadConfig:
    def test_returns_dict(self):
        config = load_config()
        assert isinstance(config, dict)

    def test_has_required_keys(self):
        config = load_config()
        assert "openai_compat" in config
        assert "http" in config
        assert "output" in config
        assert "retry" in config
        assert "concurrency" in config

    def test_nonexistent_path_uses_defaults(self, tmp_path):
        config = load_config(str(tmp_path / "nonexistent.json"))
        assert "openai_compat" in config

    def test_custom_config_merged(self, tmp_path):
        import json
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"http": {"timeout": 999}}), encoding="utf-8")
        config = load_config(str(cfg_file))
        assert config["http"]["timeout"] == 999
        # 默认值仍然存在
        assert "verify_ssl" in config["http"]

    def test_invalid_json_uses_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not valid json", encoding="utf-8")
        config = load_config(str(cfg_file))
        assert "openai_compat" in config


# ── load_glossary ────────────────────────────────────

class TestLoadGlossary:
    def test_returns_dict(self):
        glossary = load_glossary()
        assert isinstance(glossary, dict)

    def test_has_terms(self):
        glossary = load_glossary()
        assert "terms" in glossary
        assert len(glossary["terms"]) > 0

    def test_has_placeholders(self):
        glossary = load_glossary()
        assert "placeholders" in glossary

    def test_nonexistent_returns_empty(self, tmp_path):
        result = load_glossary(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_invalid_json_returns_empty(self, tmp_path):
        f = tmp_path / "glossary.json"
        f.write_text("invalid", encoding="utf-8")
        result = load_glossary(str(f))
        assert result == {}

    def test_custom_glossary(self, tmp_path):
        import json
        data = {"terms": {"玖书": "jiubook", "玖布克": "1501743"}, "placeholders": {}}
        f = tmp_path / "glossary.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        result = load_glossary(str(f))
        assert result["terms"]["玖书"] == "jiubook"
        assert result["terms"]["玖布克"] == "1501743"


def test_latest_api_reads_only_the_first_page(monkeypatch):
    import scraper as scraper_module

    calls = []

    class FakeResponse:
        def __init__(self, page):
            self.page = page

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "result": {
                    "numFound": 97,
                    "page": self.page,
                    "results": [{"title": f"article-{self.page}", "url": f"/article-{self.page}"}],
                }
            }

    def fake_get(_url, **kwargs):
        calls.append(kwargs["params"]["page"])
        return FakeResponse(kwargs["params"]["page"])

    monkeypatch.setattr(scraper_module.requests, "get", fake_get)
    config = {
        "minecraft_api": {
            "search_url": "https://example.test/search",
            "pageSize": 10,
            "sortType": "recent",
            "category": "News",
            "site_base": "https://www.minecraft.net",
        },
        "http": {
            "timeout": 10,
            "verify_ssl": True,
            "proxies": {},
            "user_agent": "test",
            "accept": "application/json",
        },
    }

    result = scraper_module.get_latest_news_list(config=config)

    assert calls == [1]
    assert [item["title"] for item in result] == ["article-1"]

    config = {"rss": {"max_age_days": 365}}

    assert not _is_recent_article("2021-01-14T00:00:00Z", config)
    assert _is_recent_article("", config)


def test_feedback_challenge_stops_retries_and_enters_cooldown(monkeypatch):
    import scraper as scraper_module

    if not scraper_module.CURL_CFFI_AVAILABLE:
        pytest.skip("curl_cffi unavailable")

    class ChallengeResponse:
        status_code = 403
        headers = {"cf-mitigated": "challenge"}
        text = "Just a moment..."

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return ChallengeResponse()

    session = FakeSession()
    monkeypatch.setattr(scraper_module.cffi_requests, "Session", lambda: session)
    config = {
        "feedback_site": {
            "base_url": "https://feedback.minecraft.net",
            "request_interval_seconds": 0,
            "challenge_cooldown_seconds": 900,
            "max_retries": 3,
        }
    }
    feedback_scraper = scraper_module.FeedbackScraper(config)

    assert feedback_scraper.fetch_page("/first") is None
    assert feedback_scraper.fetch_page("/second") is None
    assert session.calls == 1


# ── _version_id_to_url ─────────────────────────────

class TestVersionIdToUrl:
    def test_has_dash_already(self):
        cfg = {"version_manifest": {}}
        url = _version_id_to_url("26.3-snapshot-7", cfg)
        assert url == "https://www.minecraft.net/zh-hans/article/minecraft-26-3-snapshot-7"

    def test_dot_converted_to_dash(self):
        cfg = {"version_manifest": {}}
        url = _version_id_to_url("26.2", cfg)
        assert url == "https://www.minecraft.net/zh-hans/article/minecraft-26-2"

    def test_custom_template(self):
        cfg = {"version_manifest": {
            "article_url_template": "https://example.test/en-us/article/mc-{version_id}"
        }}
        url = _version_id_to_url("1.21", cfg)
        assert url == "https://example.test/en-us/article/mc-1-21"

    def test_empty_version_id(self):
        cfg = {"version_manifest": {}}
        url = _version_id_to_url("", cfg)
        assert url == "https://www.minecraft.net/zh-hans/article/minecraft-"


# ── _classify_version_type ─────────────────────────

class TestClassifyVersionType:
    @pytest.mark.parametrize("version_id,version_type,expected", [
        ("26.3-snapshot-7", "snapshot", "java_snapshot"),
        ("26.2", "release", "java_release"),
        ("26.2-rc-1", "snapshot", "java_rc"),
        ("26.2-rc-2", "snapshot", "java_rc"),
        ("26.2-pre-5", "snapshot", "java_prerelease"),
        ("1.21-pre-1", "snapshot", "java_prerelease"),
    ])
    def test_classify(self, version_id, version_type, expected):
        assert _classify_version_type(version_id, version_type) == expected


# ── get_java_news_from_manifest ─────────────────────

def test_get_java_news_from_manifest(monkeypatch):
    import scraper as scraper_module

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "versions": [
                    {"id": "26.3-snapshot-7", "type": "snapshot"},
                    {"id": "26.2", "type": "release"},
                    {"id": "26.2-rc-1", "type": "snapshot"},
                    {"id": "26.2-pre-3", "type": "snapshot"},
                    {"id": "26.2-pre-2", "type": "snapshot"},
                    {"id": "26.2-pre-1", "type": "snapshot"},
                ]
            }

    monkeypatch.setattr(scraper_module.requests, "get", lambda *a, **kw: FakeResponse())

    config = {
        "version_manifest": {
            "enabled": True,
            "manifest_url": "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
            "max_versions": 3,
            "article_url_template": "https://www.minecraft.net/zh-hans/article/minecraft-{version_id}",
        },
        "http": {
            "timeout": 10, "verify_ssl": True, "proxies": {},
            "user_agent": "test", "accept": "application/json",
        },
    }

    result = get_java_news_from_manifest(config=config)

    assert len(result) == 3
    assert result[0]["url"] == "https://www.minecraft.net/zh-hans/article/minecraft-26-3-snapshot-7"
    assert result[0]["_source"] == "version_manifest"
    assert result[0]["_version_type"] == "java_snapshot"
    assert result[1]["_version_type"] == "java_release"
    assert result[1]["url"] == "https://www.minecraft.net/zh-hans/article/minecraft-26-2"
    assert result[2]["_version_type"] == "java_rc"


def test_get_java_news_from_manifest_empty(monkeypatch):
    import scraper as scraper_module

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"versions": []}

    monkeypatch.setattr(scraper_module.requests, "get", lambda *a, **kw: FakeResponse())

    config = {
        "version_manifest": {"max_versions": 5},
        "http": {"timeout": 10, "verify_ssl": True, "proxies": {},
                 "user_agent": "test", "accept": "application/json"},
    }

    result = get_java_news_from_manifest(config=config)
    assert result == []
