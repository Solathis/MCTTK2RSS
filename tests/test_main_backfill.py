"""test_main_backfill.py — 登录/发帖失败后补发 output 里已生成但未发布的文章"""
import json
import os

import pytest

import main
import poster


class FakePoster:
    instances = []

    def __init__(self, config):
        self.config = config
        self.posted = []
        self.records = []
        FakePoster.instances.append(self)

    def login(self):
        return True

    def post_news_file(self, stem, txt_path, json_path, save_dir,
                       no_image=False, attach_json=True):
        self.posted.append(stem)
        self.records.append((stem, txt_path, json_path))
        return f"https://example.invalid/{stem}"


class FailingLoginPoster(FakePoster):
    def login(self):
        raise RuntimeError("无法提取验证码 hash")


def _seed(save_dir, stem):
    """写出 .txt + 同名 .json，返回 (txt, json) 路径。"""
    txt_path = save_dir / f"{stem}.txt"
    json_path = save_dir / f"{stem}.json"
    txt_path.write_text("正文", encoding="utf-8")
    json_path.write_text(json.dumps({"title": stem}), encoding="utf-8")
    return str(txt_path), str(json_path)


def _write_posted(save_dir, stems=()):
    (save_dir / ".posted.json").write_text(
        json.dumps(sorted(stems), ensure_ascii=False), encoding="utf-8")


def _assert_paths_match_stems(tmp_path, records):
    """补发队列里的路径必须是真实存在、且与 stem 同名的 .txt/.json。"""
    assert records
    for stem, txt_path, json_path in records:
        assert txt_path == str(tmp_path / f"{stem}.txt")
        assert json_path == str(tmp_path / f"{stem}.json")
        assert os.path.isfile(txt_path) and os.path.isfile(json_path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(poster, "MCBBSPoster", FakePoster)
    monkeypatch.setattr(poster, "load_poster_config", lambda *a, **k: {
        "base_url": "https://www.mcbbs.co",
        "forum_fid": 45,
    })
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    FakePoster.instances.clear()
    config = {
        "mcbbs": {"enabled": True},
        "retry": {"backfill_unposted": True},
        "output": {"save_dir": str(tmp_path)},
    }
    return tmp_path, config


class TestBackfillQueue:
    def test_posts_this_run_plus_unposted(self, env):
        tmp_path, config = env
        new_txt, new_json = _seed(tmp_path, "news_new")
        _seed(tmp_path, "news_old")
        _write_posted(tmp_path)

        result = main.run_post([("news_new", new_txt, new_json)], config)

        assert result["success"] == 2
        poster_inst = FakePoster.instances[-1]
        assert poster_inst.posted == ["news_new", "news_old"]
        _assert_paths_match_stems(tmp_path, poster_inst.records)
        assert json.loads((tmp_path / ".posted.json").read_text(encoding="utf-8")) == [
            "news_new", "news_old",
        ]

    def test_backfills_even_without_new_items(self, env):
        tmp_path, config = env
        _seed(tmp_path, "news_old")
        _write_posted(tmp_path)

        result = main.run_post([], config)

        assert result["success"] == 1
        assert FakePoster.instances[-1].posted == ["news_old"]

    def test_skips_backfill_when_disabled(self, env):
        tmp_path, config = env
        config["retry"]["backfill_unposted"] = False
        new_txt, new_json = _seed(tmp_path, "news_new")
        _seed(tmp_path, "news_old")
        _write_posted(tmp_path)

        main.run_post([("news_new", new_txt, new_json)], config)

        assert FakePoster.instances[-1].posted == ["news_new"]

    def test_login_failure_marks_whole_queue_failed(self, env, monkeypatch):
        tmp_path, config = env
        monkeypatch.setattr(poster, "MCBBSPoster", FailingLoginPoster)
        new_txt, new_json = _seed(tmp_path, "news_new")
        _seed(tmp_path, "news_old")
        _write_posted(tmp_path)

        result = main.run_post([("news_new", new_txt, new_json)], config)

        assert result == {"success": 0, "failed": 2, "urls": []}
        assert json.loads((tmp_path / ".posted.json").read_text(encoding="utf-8")) == []


class TestUnpostedDiscovery:
    def test_collect_unposted_requires_json_and_skips_posted(self, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")  # 缺 .json，发不了
        _seed(tmp_path, "b")
        _seed(tmp_path, "c")

        items = main._collect_unposted(str(tmp_path), {"c"})

        assert items == [("b", str(tmp_path / "b.txt"), str(tmp_path / "b.json"))]

    def test_has_unposted_mcbbs_respects_switches(self, env):
        tmp_path, config = env
        _seed(tmp_path, "news_old")
        _write_posted(tmp_path)

        assert main._has_unposted_mcbbs(config) is True
        config["retry"]["backfill_unposted"] = False
        assert main._has_unposted_mcbbs(config) is False
        config["retry"]["backfill_unposted"] = True
        config["mcbbs"]["enabled"] = False
        assert main._has_unposted_mcbbs(config) is False

    def test_post_only_still_uses_same_discovery(self, env):
        tmp_path, config = env
        _seed(tmp_path, "news_a")
        _seed(tmp_path, "news_b")
        _write_posted(tmp_path, {"news_b"})

        main.run_post_only(config)

        poster_inst = FakePoster.instances[-1]
        assert poster_inst.posted == ["news_a"]
        _assert_paths_match_stems(tmp_path, poster_inst.records)


class TestConfigPath:
    def test_run_post_uses_selected_config_path(self, env, monkeypatch):
        tmp_path, config = env
        new_txt, new_json = _seed(tmp_path, "news_new")
        captured = {}

        def fake_loader(config_path=None):
            captured["path"] = config_path
            return {"base_url": "https://www.mcbbs.co", "forum_fid": 45}

        monkeypatch.setattr(poster, "load_poster_config", fake_loader)
        main.run_post([("news_new", new_txt, new_json)], config, config_path="custom.json")

        assert captured["path"] == "custom.json"

    def test_run_post_only_uses_selected_config_path(self, env, monkeypatch):
        tmp_path, config = env
        _seed(tmp_path, "news_new")
        _write_posted(tmp_path)
        captured = {}

        def fake_loader(config_path=None):
            captured["path"] = config_path
            return {"base_url": "https://www.mcbbs.co", "forum_fid": 45}

        monkeypatch.setattr(poster, "load_poster_config", fake_loader)
        main.run_post_only(config, config_path="custom.json")

        assert captured["path"] == "custom.json"
