"""test_main_state.py — `.state.json` 必须是原子写

背景：`save_state()` 原来是 `open(path, "w")` + `json.dump()`，写之前先把文件截断。
容器重启 / CI 超时 / 断电刚好落在这一瞬间，磁盘上就只剩半截 JSON；下一次 `load_state()`
解析失败会回落成「首次运行」，把当前抓到的新闻全部标成已处理 —— 日志是绿的，一篇都没发。
"""

import json

import pytest

import main


def _state_file(tmp_path):
    return tmp_path / ".state.json"


class TestSaveStateIsAtomic:
    def test_failed_write_keeps_previous_state(self, tmp_path):
        """写新状态失败时，旧文件必须原样留着，不能变成半截 JSON。"""
        state_file = _state_file(tmp_path)
        main.save_state(str(state_file), {"posted_urls": ["https://example.com/a"], "last_run": None})
        before = state_file.read_text(encoding="utf-8")

        with pytest.raises(TypeError):
            # 集合没法序列化成 JSON：写入会在 dump 阶段失败
            main.save_state(str(state_file), {"posted_urls": {"不是", "能序列化的值"}})

        assert state_file.read_text(encoding="utf-8") == before
        assert json.loads(state_file.read_text(encoding="utf-8"))["posted_urls"] == [
            "https://example.com/a"
        ]

    def test_no_temp_file_left_behind(self, tmp_path):
        main.save_state(str(_state_file(tmp_path)), {"posted_urls": []})

        leftovers = [path.name for path in tmp_path.iterdir() if path.suffix == ".tmp"]
        assert leftovers == [], "原子写的临时文件必须清干净"

    def test_write_creates_missing_directory(self, tmp_path):
        state_file = tmp_path / "nested" / ".state.json"

        main.save_state(str(state_file), {"posted_urls": []})

        assert json.loads(state_file.read_text(encoding="utf-8"))["posted_urls"] == []
