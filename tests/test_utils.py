"""test_utils.py — utils.py 单元测试"""
import json
import os

import pytest

import utils
from utils import (
    find_image,
    load_dotenv,
    load_posted,
    load_thread_records,
    save_posted,
    save_thread_records,
    thread_records_path,
)


class TestLoadDotenv:
    def test_loads_variables(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR_MCTTK=hello\n", encoding="utf-8")
        os.environ.pop("TEST_VAR_MCTTK", None)
        load_dotenv(str(tmp_path))
        assert os.environ.get("TEST_VAR_MCTTK") == "hello"
        del os.environ["TEST_VAR_MCTTK"]

    def test_does_not_overwrite_existing(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR_MCTTK2=from_file\n", encoding="utf-8")
        os.environ["TEST_VAR_MCTTK2"] = "original"
        load_dotenv(str(tmp_path))
        assert os.environ["TEST_VAR_MCTTK2"] == "original"
        del os.environ["TEST_VAR_MCTTK2"]

    def test_skips_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# this is a comment\nTEST_VAR_MCTTK3=value\n", encoding="utf-8")
        os.environ.pop("TEST_VAR_MCTTK3", None)
        load_dotenv(str(tmp_path))
        assert os.environ.get("TEST_VAR_MCTTK3") == "value"
        del os.environ["TEST_VAR_MCTTK3"]

    def test_no_env_file(self, tmp_path):
        # 不存在 .env 文件时不报错
        load_dotenv(str(tmp_path))

    def test_value_with_equals(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR_MCTTK4=jiubook=玖书=1501743\n", encoding="utf-8")
        os.environ.pop("TEST_VAR_MCTTK4", None)
        load_dotenv(str(tmp_path))
        assert os.environ.get("TEST_VAR_MCTTK4") == "jiubook=玖书=1501743"
        del os.environ["TEST_VAR_MCTTK4"]

    def test_empty_lines_skipped(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("\n\nTEST_VAR_MCTTK5=ok\n\n", encoding="utf-8")
        os.environ.pop("TEST_VAR_MCTTK5", None)
        load_dotenv(str(tmp_path))
        assert os.environ.get("TEST_VAR_MCTTK5") == "ok"
        del os.environ["TEST_VAR_MCTTK5"]


class TestPostedState:
    """两个平台各自一个状态文件，读写逻辑共用。"""

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_posted(str(tmp_path / "nope.json")) == set()

    def test_round_trip_sorted(self, tmp_path):
        path = tmp_path / ".posted.json"
        save_posted(str(path), {"news_b", "news_a"})
        assert load_posted(str(path)) == {"news_a", "news_b"}
        assert json.loads(path.read_text(encoding="utf-8")) == ["news_a", "news_b"]

    def test_overwrite_existing(self, tmp_path):
        path = str(tmp_path / ".posted.json")
        save_posted(path, {"old"})
        save_posted(path, {"new"})
        assert load_posted(path) == {"new"}

    def test_write_leaves_no_temp_file(self, tmp_path):
        path = str(tmp_path / ".posted.json")
        save_posted(path, {"news_a"})
        assert [item.name for item in tmp_path.iterdir()] == [".posted.json"]

    def test_interrupted_write_keeps_old_state(self, tmp_path, monkeypatch):
        """写到一半失败（断电 / 磁盘满 / 被 kill）时旧文件必须还完整可读。"""
        path = str(tmp_path / ".posted.json")
        save_posted(path, {"old"})

        def boom(obj, fp, **kwargs):
            fp.write('["half')
            raise OSError("磁盘满了")

        monkeypatch.setattr(utils.json, "dump", boom)
        with pytest.raises(OSError):
            save_posted(path, {"new"})

        assert load_posted(path) == {"old"}
        assert [item.name for item in tmp_path.iterdir()] == [".posted.json"]


class TestThreadRecords:
    """发帖之后的记录：读不到不能影响发布，写坏了不能留下半截文件。"""

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_thread_records(str(tmp_path / "nope.json")) == {}

    def test_round_trip_sorted(self, tmp_path):
        path = tmp_path / ".mcbbs_threads.json"
        records = {"news_b": {"tid": 2}, "news_a": {"tid": 1}}
        save_thread_records(str(path), records)
        assert load_thread_records(str(path)) == records
        assert list(json.loads(path.read_text(encoding="utf-8"))) == ["news_a", "news_b"]

    def test_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / ".mcbbs_threads.json"
        path.write_text('{"news_a": ', encoding="utf-8")
        assert load_thread_records(str(path)) == {}

    def test_non_dict_payload_returns_empty(self, tmp_path):
        path = tmp_path / ".mcbbs_threads.json"
        path.write_text('["news_a"]', encoding="utf-8")
        assert load_thread_records(str(path)) == {}

    def test_write_leaves_no_temp_file(self, tmp_path):
        path = str(tmp_path / ".mcbbs_threads.json")
        save_thread_records(path, {"news_a": {}})
        assert [item.name for item in tmp_path.iterdir()] == [".mcbbs_threads.json"]

    def test_path_named_after_records(self, tmp_path):
        assert os.path.basename(thread_records_path(str(tmp_path))) == ".mcbbs_threads.json"


class TestFindImage:
    def test_finds_first_supported_extension(self, tmp_path):
        (tmp_path / "news_x.webp").write_bytes(b"x")
        (tmp_path / "news_x.jpg").write_bytes(b"x")
        assert find_image(str(tmp_path), "news_x").endswith(".jpg")

    def test_missing_image(self, tmp_path):
        assert find_image(str(tmp_path), "news_x") is None

    def test_same_stem_other_format_ignored(self, tmp_path):
        (tmp_path / "news_x.txt").write_text("x", encoding="utf-8")
        assert find_image(str(tmp_path), "news_x") is None
