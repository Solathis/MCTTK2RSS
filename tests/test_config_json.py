"""test_config_json.py — config.json 结构与内容验证"""
import json
from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"
SAMPLE_PATH = CONFIG_DIR / "config.sample.json"


def _active_config_path() -> Path:
    """本地开发校验 config.json；CI / 新克隆没有本地配置时校验模板。

    config.json 已改为不入库（.gitignore 忽略），仓库只保留 config.sample.json，
    因此这里必须能退回到模板，否则 CI 会因为缺少文件整片失败。
    """
    return CONFIG_PATH if CONFIG_PATH.exists() else SAMPLE_PATH


@pytest.fixture(scope="module")
def config():
    with open(_active_config_path(), encoding="utf-8") as f:
        return json.load(f)


class TestConfigStructure:
    def test_config_file_available(self):
        assert _active_config_path().exists(), "既没有 config.json 也没有 config.sample.json"

    def test_valid_json(self):
        with open(_active_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_required_top_level_keys(self, config):
        required = ["openai_compat", "prompts", "minecraft_api", "http", "output", "retry", "concurrency"]
        for key in required:
            assert key in config, f"缺少顶级键: {key}"

    def test_openai_compat_keys(self, config):
        oc = config["openai_compat"]
        for key in ["host", "endpoint", "model", "max_tokens", "timeout"]:
            assert key in oc, f"openai_compat 缺少键: {key}"

    def test_openai_compat_types(self, config):
        oc = config["openai_compat"]
        assert isinstance(oc["host"], str)
        assert isinstance(oc["endpoint"], str)
        assert isinstance(oc["model"], str)
        assert isinstance(oc["max_tokens"], int)
        assert isinstance(oc["timeout"], int)
        assert oc["max_tokens"] > 0
        assert oc["timeout"] > 0

    def test_prompts_keys(self, config):
        prompts = config["prompts"]
        for key in ["translate_text_default", "translate_blocks_system", "translate_title_system",
                    "summarize_system"]:
            assert key in prompts, f"prompts 缺少键: {key}"
            assert isinstance(prompts[key], str) and len(prompts[key]) > 0

    def test_minecraft_api_keys(self, config):
        api = config["minecraft_api"]
        for key in ["search_url", "pageSize", "sortType", "category", "site_base"]:
            assert key in api, f"minecraft_api 缺少键: {key}"

    def test_minecraft_api_url_format(self, config):
        api = config["minecraft_api"]
        assert api["search_url"].startswith("https://")
        assert api["site_base"].startswith("https://")
        assert isinstance(api["pageSize"], int) and api["pageSize"] > 0

    def test_version_manifest_url_templates(self, config):
        """四类 slug 缺一类，对应类型就会被版本清单静默跳过（预发布/RC 踩过）。"""
        templates = config.get("version_manifest", {}).get("url_templates", {})
        for news_type in ("java_snapshot", "java_prerelease", "java_rc", "java_release"):
            assert news_type in templates, f"version_manifest.url_templates 缺少: {news_type}"
            assert "{version_id}" in templates[news_type], f"{news_type} 模板缺少 {{version_id}}"

    def test_http_keys(self, config):
        http = config["http"]
        for key in ["verify_ssl", "user_agent", "accept", "proxies", "timeout"]:
            assert key in http, f"http 缺少键: {key}"

    def test_http_types(self, config):
        http = config["http"]
        assert isinstance(http["verify_ssl"], bool)
        assert isinstance(http["user_agent"], str) and len(http["user_agent"]) > 0
        assert isinstance(http["timeout"], int) and http["timeout"] > 0
        assert isinstance(http["proxies"], dict)

    def test_output_save_dir(self, config):
        assert "save_dir" in config["output"]
        assert isinstance(config["output"]["save_dir"], str)

    def test_retry_structure(self, config):
        retry = config["retry"]
        for key in ["translation", "download"]:
            assert key in retry
            assert "max_retries" in retry[key]
            assert isinstance(retry[key]["max_retries"], int)
            assert retry[key]["max_retries"] >= 0
        # 这两个键是模板的一部分，删掉就该报错，不能靠 if 跳过检查
        assert "login" in retry, "模板必须保留 retry.login（MCBBS 登录重试）"
        assert isinstance(retry["login"]["max_attempts"], int)
        assert retry["login"]["max_attempts"] >= 1
        assert isinstance(retry["login"]["wait_seconds"], int)
        assert retry["login"]["wait_seconds"] >= 0
        assert "backfill_unposted" in retry, "模板必须保留 retry.backfill_unposted（未发布文章补发）"
        assert isinstance(retry["backfill_unposted"], bool)

    def test_concurrency_structure(self, config):
        c = config["concurrency"]
        for key in ["translation_workers", "batch_max_chars", "batch_max_items"]:
            assert key in c, f"concurrency 缺少键: {key}"
            assert isinstance(c[key], int) and c[key] > 0

    def test_glossary_structure(self, config):
        if "glossary" not in config:
            return
        g = config["glossary"]
        assert isinstance(g.get("verify", True), bool)
        assert isinstance(g.get("verify_max_rounds", 1), int) and g["verify_max_rounds"] >= 0
        assert isinstance(g.get("verify_max_items", 20), int) and g["verify_max_items"] > 0

    def test_summary_structure(self, config):
        if "summary" not in config:
            return
        s = config["summary"]
        assert isinstance(s.get("enabled", True), bool)
        for key in ("max_chars", "input_max_chars"):
            assert isinstance(s[key], int) and s[key] > 0, f"summary.{key} 必须是正整数"
        assert isinstance(s.get("min_chars", 0), int) and s["min_chars"] >= 0
        assert s.get("min_chars", 0) < s["max_chars"], "summary 的下限必须小于上限"

    def test_news_types_structure(self, config):
        if "news_types" not in config:
            return
        nt = config["news_types"]
        expected_types = ["java_release", "java_snapshot", "java_prerelease", "java_rc",
                          "bedrock_release", "bedrock_beta"]
        for t in expected_types:
            assert t in nt, f"news_types 缺少: {t}"
            assert isinstance(nt[t], bool)

    def test_mcbbs_structure(self, config):
        if "mcbbs" not in config:
            return
        mcbbs = config["mcbbs"]
        for key in ["enabled", "base_url", "forum_fid"]:
            assert key in mcbbs, f"mcbbs 缺少键: {key}"
        assert isinstance(mcbbs["enabled"], bool)
        assert isinstance(mcbbs["base_url"], str)
        assert mcbbs["base_url"].startswith("https://")

    def test_mcbbs_highlight_structure(self, config):
        """highlight 是带默认值的嵌套段，本地 config.json 允许只写想改的键。"""
        if "mcbbs" not in config:
            return
        highlight = config["mcbbs"].get("highlight")
        if highlight is None:
            return
        assert isinstance(highlight, dict)
        if "enabled" in highlight:
            assert isinstance(highlight["enabled"], bool)
        if "colors" in highlight:
            assert isinstance(highlight["colors"], dict)
            for news_type, color in highlight["colors"].items():
                assert isinstance(news_type, str) and news_type
                assert isinstance(color, str) and color.startswith("#"), f"色值必须是 #RRGGBB: {color}"
        if "bold_types" in highlight:
            assert isinstance(highlight["bold_types"], list)
            for news_type in highlight["bold_types"]:
                assert isinstance(news_type, str) and news_type
        for key in ("max_attempts", "max_age_hours", "max_age_days"):
            if key in highlight:
                assert isinstance(highlight[key], int) and highlight[key] > 0, f"highlight.{key} 必须是正整数"

    def test_mcbbs_highlight_defaults_match_poster_module(self):
        """模板里的 highlight 键必须与 poster.DEFAULT_HIGHLIGHT 对齐。

        这一段的默认值写在 poster 里（main 侧不读 mcbbs 段），模板少一个键，
        新用户就会静默用上模块默认值，看不出差异。
        """
        from poster import DEFAULT_HIGHLIGHT

        sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        highlight = sample["mcbbs"]["highlight"]
        assert set(highlight) == set(DEFAULT_HIGHLIGHT)
        assert set(highlight["colors"]) == set(DEFAULT_HIGHLIGHT["colors"])

    def test_bilibili_structure(self, config):
        """本地 config.json 允许只写想改的键（其余由 DEFAULT_CONFIG 补齐），

        所以这里只校验「出现了的键」类型是否对；键的完整性由
        test_bilibili_defaults_match_poster_module 对模板做全量校验。
        """
        if "bilibili" not in config:
            return
        bili = config["bilibili"]
        if "enabled" in bili:
            assert isinstance(bili["enabled"], bool)
        if "cookie_file" in bili:
            assert isinstance(bili["cookie_file"], str)
        if "category_id" in bili:
            assert isinstance(bili["category_id"], int) and bili["category_id"] > 0
        if "submit" in bili:
            assert bili["submit"] in ("draft", "publish"), f"submit 只能是 draft/publish: {bili['submit']}"
        if "max_title_chars" in bili:
            assert isinstance(bili["max_title_chars"], int) and bili["max_title_chars"] > 0

    def test_bilibili_defaults_match_poster_module(self):
        """配置模板与 DEFAULT_CONFIG 里的 bilibili 键必须与 bili.poster 默认值一致。

        三处各写一遍不可避免（模块间不能反向 import），所以用测试钉住；
        否则新增一个配置项只改一处，另一个入口就会静默用错默认值。
        """
        from bili.poster import DEFAULT_BILI_CONFIG
        from scraper import DEFAULT_CONFIG

        expected = set(DEFAULT_BILI_CONFIG)
        assert set(DEFAULT_CONFIG["bilibili"]) == expected
        sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        assert set(sample["bilibili"]) == expected

    def test_feedback_site_structure(self, config):
        if "feedback_site" not in config:
            return
        fb = config["feedback_site"]
        assert "enabled" in fb
        assert isinstance(fb["enabled"], bool)
        if fb.get("enabled"):
            assert "base_url" in fb
            assert fb["base_url"].startswith("https://")


class TestSampleConfig:
    """config.sample.json 是唯一入库的配置，不能带任何真实凭据。"""

    def test_sample_exists(self):
        assert SAMPLE_PATH.exists(), "仓库必须保留 config.sample.json 模板"

    def test_sample_has_no_secrets(self):
        data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        assert data["openai_compat"]["api_key"] == "", "模板里不能出现真实 API Key"
        assert data["mcbbs"]["password"] == "", "模板里不能出现真实密码"
        assert data["mcbbs"]["username"] == "", "模板里不能出现真实用户名"

    def test_sample_has_new_sections(self):
        """新增配置段必须进模板，不能只写进 DEFAULT_CONFIG。

        只靠默认值兜底的话，拉新版本的部署永远在用默认值，模板与代码悄悄分叉。
        """
        data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        assert "summary" in data, "config.sample.json 必须包含 summary 段"
        assert "summarize_system" in data["prompts"], "config.sample.json 必须包含摘要提示词"

    def test_sample_ships_with_tls_verification_on(self):
        """模板默认开着校验：关掉它等于把带 API key 的请求暴露给中间人。"""
        data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        assert data["http"]["verify_ssl"] is True

    def test_config_with_bom_still_loads(self, tmp_path):
        """Windows 编辑器另存的 UTF-8 会带 BOM，不能让整份配置回落默认值。"""
        import scraper

        path = tmp_path / "config.json"
        path.write_text('{"openai_compat": {"host": "api.mojang.com", "api_key": "k"}}',
                        encoding="utf-8-sig")

        config = scraper.load_config(str(path))

        assert config["openai_compat"]["host"] == "api.mojang.com"
        assert config["openai_compat"]["api_key"] == "k"
