"""test_translation_retry.py — 模型返回被截断时的抢救、拆分重试与有界退出"""

import json

import scraper
from scraper import (
    _parse_batch_translation,
    _salvage_translation_items,
    translate_blocks,
)

# 模拟真实故障：输出撞上 max_tokens，JSON 尾部被切掉（第 3 条只写了一半）
TRUNCATED = (
    '{"translations":['
    '{"id":"t0000","translated_text":"译一"},'
    '{"id":"t0001","translated_text":"译二"},'
    '{"id":"t0002","translated_text":"译'
)


def _cfg(parse_retries=2):
    return {
        "prompts": {"translate_blocks_system": "sys"},
        "concurrency": {"translation_workers": 1, "batch_max_chars": 10000, "batch_max_items": 10},
        "retry": {"translation": {"parse_retries": parse_retries}},
        "glossary": {"verify": False},
    }


def _blocks(texts):
    return [
        {"id": f"b{i + 1:04d}", "type": "p", "source_text": t} for i, t in enumerate(texts)
    ]


def _complete(batch, prefix="译"):
    return json.dumps(
        {"translations": [{"id": it["id"], "translated_text": f"{prefix}{it['id']}"} for it in batch]},
        ensure_ascii=False,
    )


# ── 从残缺 JSON 里抢救 ───────────────────────────────

class TestSalvage:
    def test_keeps_complete_entries_only(self):
        salvaged = _salvage_translation_items(TRUNCATED)
        assert [item["id"] for item in salvaged] == ["t0000", "t0001"]
        assert salvaged[0]["translated_text"] == "译一"

    def test_handles_escaped_quotes(self):
        raw = '{"translations":[{"id":"t0000","translated_text":"他说\\"猛\\""}]}'
        assert _salvage_translation_items(raw)[0]["translated_text"] == '他说"猛"'

    def test_ignores_prose_and_plain_text(self):
        assert _salvage_translation_items("模型抽风了，什么都没有") == []
        assert _salvage_translation_items("") == []

    def test_salvage_used_by_parser(self, capsys):
        batch = [{"id": f"t{i:04d}", "text": f"t{i}"} for i in range(3)]
        result = _parse_batch_translation(TRUNCATED, batch)
        assert result == {0: "译一", 1: "译二"}
        assert "抢救" in capsys.readouterr().out


# ── 拆分重试 ────────────────────────────────────────

class TestSplitRetry:
    def test_retries_only_missing_items(self, monkeypatch):
        calls = []

        def fake_translate(text, **kwargs):
            batch = json.loads(text)
            calls.append([item["id"] for item in batch])
            return TRUNCATED if len(batch) > 2 else _complete(batch)

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = translate_blocks(_blocks(["a", "b", "c"]), config=_cfg())
        # 首轮抢救出 2 条，只剩 1 条重试
        assert [b["translated_text"] for b in blocks] == ["译一", "译二", "译t0002"]
        assert len(calls) == 2
        assert calls[1] == ["t0002"]

    def test_large_batch_splits(self, monkeypatch):
        calls = []

        def fake_translate(text, **kwargs):
            batch = json.loads(text)
            calls.append(len(batch))
            # 只有首轮（10 条）会被截断；拆分后的子批次正常返回
            return TRUNCATED if len(batch) >= 10 else _complete(batch)

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = translate_blocks(_blocks([f"t{i}" for i in range(10)]), config=_cfg())
        assert all(b.get("translated_text") for b in blocks), "拆分重试后必须全部拿到译文"
        assert calls[0] == 10
        assert sum(calls[1:]) == 8, "只重试缺的 8 条（3 条已抢救）"
        assert len(calls) == 3, "10 条 → 拆成 4+4 两批"

    def test_zero_retries_keeps_old_behaviour(self, monkeypatch):
        calls = []

        def fake_translate(text, **kwargs):
            calls.append(1)
            return TRUNCATED

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = translate_blocks(_blocks(["a", "b", "c"]), config=_cfg(parse_retries=0))
        assert [b.get("translated_text") for b in blocks] == ["译一", "译二", None]
        assert len(calls) == 1, "parse_retries=0 时不该重试"


# ── 有界退出 ────────────────────────────────────────

class TestRetryIsBounded:
    def test_gives_up_after_depth_limit(self, monkeypatch):
        calls = []

        def fake_translate(text, **kwargs):
            calls.append(1)
            return TRUNCATED

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = translate_blocks(_blocks([f"t{i}" for i in range(10)]), config=_cfg(parse_retries=1))
        # 抢救出的 2 条仍保留，其余放弃；1 次首轮 + 2 次拆分
        assert [b.get("translated_text") for b in blocks][:2] == ["译一", "译二"]
        assert len(calls) == 3
        assert all("unused" not in str(b) for b in blocks)

    def test_no_exception_when_everything_fails(self, monkeypatch):
        monkeypatch.setattr(scraper, "translate_text", lambda *a, **k: "")
        blocks = translate_blocks(_blocks(["a", "b"]), config=_cfg())
        assert [b.get("translated_text") for b in blocks] == [None, None]


# ── 回退闸门 ────────────────────────────────────────

class TestLineFallbackGate:
    """拆分重试不接受"按行对齐"，否则模型的一段解释会被当成译文"""

    def test_split_items_reject_prose(self, monkeypatch):
        def fake_translate(text, **kwargs):
            return "这是一段解释性文字，不是译文 JSON"

        monkeypatch.setattr(scraper, "translate_text", fake_translate)
        blocks = translate_blocks(_blocks(["a", "b"]), config=_cfg(parse_retries=1))
        assert [b.get("translated_text") for b in blocks] == [None, None]

    def test_single_item_batch_still_allows_plain_lines(self, monkeypatch):
        # 原批次就只有 1 条时的兼容行为：一行纯文本仍视为译文
        monkeypatch.setattr(scraper, "translate_text", lambda *a, **k: "只有一行译文")
        blocks = translate_blocks(_blocks(["a"]), config=_cfg())
        assert blocks[0]["translated_text"] == "只有一行译文"
