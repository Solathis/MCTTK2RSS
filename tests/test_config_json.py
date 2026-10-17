"""test_config_json.py — config.json 结构与内容验证"""
import json
from pathlib import Path

import pytest

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
SAMPLE_PATH = Path(__file__).parent.parent / "config.sample.json"


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
        for key in ["translate_text_default", "translate_blocks_system", "translate_title_system"]:
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

    def test_concurrency_structure(self, config):
        c = config["concurrency"]
        for key in ["translation_workers", "batch_max_chars", "batch_max_items"]:
            assert key in c, f"concurrency 缺少键: {key}"
            assert isinstance(c[key], int) and c[key] > 0

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
