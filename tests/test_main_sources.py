"""test_main_sources.py — 各来源的候选都必须进入状态比对

回归：`_filter_and_check_state()` 曾只收集 `minecraft_api` 与 `feedback` 两类
来源，`version_manifest` 来源的候选既不参与类型过滤也不参与状态比对，直接被
静默丢弃——Piston 版本清单补齐 Java 版本日志的链路因此整条空转。
（用 `--dry-run` 可复现：日志里「多来源合并去重后共 9 条候选」，
紧随其后的「状态检查」却只有 5 条。）
"""

import json
from pathlib import Path

import main

API_URL = "https://www.minecraft.net/zh-hans/article/minecraft-java-edition-26-3"
MANIFEST_URL = "https://www.minecraft.net/zh-hans/article/minecraft-26-4-snapshot-8"
FEEDBACK_URL = "https://feedback.minecraft.net/hc/en-us/articles/50285693907213"


def _config(tmp_path, news_types=None):
    return {
        "output": {"save_dir": str(tmp_path / "output")},
        "retry": {"max_article_attempts": 2},
        "version_manifest": {"enabled": False},
        "news_types": news_types or {},
    }


def _state_file(tmp_path, state=None):
    out = tmp_path / "output"
    out.mkdir(parents=True, exist_ok=True)
    path = out / ".state.json"
    path.write_text(
        json.dumps(state if state is not None else {"posted_urls": [], "last_run": None}),
        encoding="utf-8",
    )
    return str(path)


def _news():
    return [
        {"url": API_URL, "title": "Minecraft Java Edition 26.3", "_source": "minecraft_api"},
        {
            "url": MANIFEST_URL,
            "title": "Minecraft 26.4 Snapshot 8",
            "_source": "version_manifest",
            "_version_type": "java_snapshot",
        },
        {"url": FEEDBACK_URL, "title": "Minecraft Beta and Preview - 26.70.24", "_source": "feedback"},
    ]


def _new_urls(result):
    assert result is not None, "非首次运行时应返回 (new_news, state, posted_urls)"
    new_news, _, _ = result
    return [item["url"] for item in new_news]


class TestVersionManifestCandidates:
    def test_manifest_candidate_is_treated_as_new(self, tmp_path):
        urls = _new_urls(
            main._filter_and_check_state(_news(), _config(tmp_path), _state_file(tmp_path))
        )
        assert MANIFEST_URL in urls, "版本清单候选必须进入待处理列表"
        assert API_URL in urls

    def test_manifest_candidate_is_skipped_when_already_posted(self, tmp_path):
        state_file = _state_file(tmp_path, {"posted_urls": [MANIFEST_URL], "last_run": None})
        urls = _new_urls(main._filter_and_check_state(_news(), _config(tmp_path), state_file))
        assert MANIFEST_URL not in urls, "已处理过的版本清单候选不应重复处理"
        assert API_URL in urls

    def test_manifest_candidate_respects_news_types(self, tmp_path):
        config = _config(tmp_path, news_types={"java_snapshot": False, "java_release": True})
        urls = _new_urls(main._filter_and_check_state(_news(), config, _state_file(tmp_path)))
        assert API_URL in urls
        assert MANIFEST_URL not in urls, "news_types 关掉 java_snapshot 后不应再处理快照候选"

    def test_first_run_marks_manifest_candidate_as_posted(self, tmp_path):
        state_file = _state_file(tmp_path, {"posted_urls": [], "_first_run": True})

        assert main._filter_and_check_state(_news(), _config(tmp_path), state_file) is None

        posted = json.loads(Path(state_file).read_text(encoding="utf-8"))["posted_urls"]
        assert MANIFEST_URL in posted, "首次运行必须把版本清单候选一起标记，否则下次会补发历史文章"
