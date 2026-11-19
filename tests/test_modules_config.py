"""test_modules_config.py — 模块配置（config/modules_config.json）结构校验

模块是入库的配置文件，写错 platform 不会报错、只会让某个平台悄悄少一段头尾
（无效值按 mcbbs 处理），所以这里把结构约束钉住。
"""
import json
from pathlib import Path

import pytest

MODULES_PATH = Path(__file__).parent.parent / "config" / "modules_config.json"
VALID_PLATFORMS = {"all", "mcbbs", "bilibili"}


@pytest.fixture(scope="module")
def config():
    with open(MODULES_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def modules(config):
    return config["default_modules"] + config.get("custom_modules", [])


class TestModulesConfig:
    def test_file_exists(self):
        assert MODULES_PATH.is_file(), "config/modules_config.json 必须入库"

    def test_has_default_modules(self, config):
        assert config.get("default_modules"), "至少要有头部/尾部模板"

    def test_ids_unique(self, modules):
        ids = [m.get("id") for m in modules]
        assert len(ids) == len(set(ids)), f"模块 id 重复: {ids}"

    def test_position_values(self, modules):
        for module in modules:
            assert module.get("position") in ("start", "end"), f"{module.get('id')} 的 position 非法"

    def test_platform_values(self, modules):
        for module in modules:
            if "platform" not in module:
                continue  # 缺省按 mcbbs 处理
            assert module["platform"] in VALID_PLATFORMS, \
                f"{module.get('id')} 的 platform 非法: {module['platform']}"

    def test_every_module_has_content(self, modules):
        for module in modules:
            has_content = module.get("content") or module.get("bbcode_content") or module.get("markdown_content")
            assert has_content, f"{module.get('id')} 没有任何内容字段"

    def test_both_platforms_covered(self, modules):
        """专栏与论坛都必须至少有一个模块，否则头尾会整段消失。"""
        platforms = {m.get("platform", "mcbbs") for m in modules}
        assert "all" in platforms or {"mcbbs", "bilibili"} <= platforms


class TestLoadModulesConfig:
    def test_bom_does_not_break_loading(self, tmp_path, monkeypatch):
        """Windows 编辑器另存的 UTF-8 带 BOM，不能让读取直接抛错、整篇转换中断。"""
        import main

        path = tmp_path / "modules_config.json"
        path.write_text(json.dumps({"default_modules": []}), encoding="utf-8-sig")
        monkeypatch.setattr(main, "CONFIG_DIR", str(tmp_path))

        assert main._load_modules_config() == {"default_modules": []}
