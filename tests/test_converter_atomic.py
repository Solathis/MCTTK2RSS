"""test_converter_atomic.py — 转换产物原子落盘（避免半截 .txt 被补发）"""
import pytest

import converter


class TestWriteAtomic:
    def test_overwrites_content(self, tmp_path):
        path = tmp_path / "news_x.txt"
        path.write_text("旧内容", encoding="utf-8")

        converter._write_atomic(str(path), "新内容")

        assert path.read_text(encoding="utf-8") == "新内容"
        assert not list(tmp_path.glob(".*.tmp"))

    def test_failed_replace_keeps_old_content(self, tmp_path, monkeypatch):
        path = tmp_path / "news_x.txt"
        path.write_text("旧内容", encoding="utf-8")

        def boom(src, dst):
            raise OSError("replace failed")

        monkeypatch.setattr(converter.os, "replace", boom)

        with pytest.raises(OSError):
            converter._write_atomic(str(path), "新内容")

        assert path.read_text(encoding="utf-8") == "旧内容", "写入失败不能留下半截正文"
        assert not list(tmp_path.glob(".*.tmp")), "失败后不能留下临时文件"
