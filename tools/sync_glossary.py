#!/usr/bin/env python3
"""tools/sync_glossary.py — 把 CFPAOrg 术语表(TSV)转换成自动词表层 glossary.auto.json

为什么不直接把 TSV 拍平成 glossary 的 terms：
  * TSV 约 8557 行，其中约 3000 行是整句游戏内提示语（含 %s / § / 句号），
    新闻正文几乎不会原样出现，塞进词表只占体积；
  * 其余行里有大量 UI 通用词（Done / No / All / Air），散文里命中后会污染译文；
  * 同一英文可能有多个官方译名（Speed → 速度 / 迅捷），必须保留歧义信息。

因此本脚本产出三层词条：
  hard        —— 多词专有名词，命中即"必须使用该译名"
  soft        —— 单词世界名词，在"原样大小写匹配"或"整段独立出现"时注入
  soft_strict —— 状态效果/附魔名（Speed→迅捷 与常用词 speed→速度 撞车），
                 只在"整段独立出现"时注入
另有 ambiguous 记录同英文多译名，供提示词附带说明。

溯源信息（术语 → TSV 原始键）单独写到 glossary.auto.index.json：
运行时不会加载，也不进提示词，只用于人工复核/调试。

手写 glossary.json 永远优先于本文件（见 scraper.load_glossary 的合并规则）。

用法:
  python tools/sync_glossary.py                     # 下载官方 TSV 并生成 glossary.auto.json
  python tools/sync_glossary.py --tsv logs/latest.tsv
  python tools/sync_glossary.py --stats             # 只打印统计，不写文件
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_URL = "https://raw.githubusercontent.com/CFPAOrg/Glossary/main/Vanilla/latest.tsv"
SOURCE_PAGE = "https://github.com/CFPAOrg/Glossary/blob/main/Vanilla/latest.tsv"
DEFAULT_OUT = "glossary.auto.json"
DEFAULT_INDEX_OUT = "glossary.auto.index.json"

# 只收"游戏世界名词"相关的命名空间。subtitles/commands/options/gui/death
# 等命名空间放的是整句或 UI 文案，注入到新闻正文里有害无益。
NS_WHITELIST = frozenset({
    "block", "item", "entity", "mob_effect", "effect", "biome", "enchantment",
    "painting", "particle", "instrument", "jukebox_song", "trim_pattern",
    "trim_material", "cat_variant", "wolf_variant", "frog_variant",
    "cow_variant", "pig_variant", "chicken_variant",
})

# 命名空间优先级：同一英文多译名时，靠前者作为主译名。
NS_PRIORITY = [
    "item", "block", "entity", "biome", "mob_effect", "effect", "enchantment",
    "painting", "particle", "instrument", "trim_pattern", "trim_material",
]

# 这些命名空间下的单词多是"状态效果/附魔名"，和常用词同形（Speed→迅捷，
# 而句中的 speed 是"速度"），只在整段独立出现时才允许注入。
NS_STANDALONE_ONLY = frozenset({"effect", "mob_effect", "enchantment"})

# 这些单字是通用英文/本项目自用词，命中即注入只会干扰译文。
STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "that",
    "this", "these", "those", "of", "in", "on", "at", "to", "for", "from",
    "by", "with", "as", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "do", "does", "did", "done", "no", "not", "yes", "all",
    "any", "more", "most", "some", "up", "down", "out", "off", "over",
    "under", "again", "new", "old", "data", "player", "players", "target",
    "targets", "minecraft", "mojang", "i", "you", "he", "she", "we",
    "they", "me", "my", "your", "our", "their", "his", "her", "them",
    "us", "who", "what", "when", "where", "how",
})

FORMAT_CODE = re.compile(r"%(\d+\$)?[sdf]|\\n|§|<[^>]+>|\{[^}]*\}")
MAX_WORDS = 3
SENTENCE_END = (".", "!", "?", ":", ",", ";")


def parse_tsv(text: str) -> list[dict]:
    """解析 TSV 文本，返回 [{key, en_us, zh_cn}, ...]"""
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    rows = []
    for row in reader:
        en = (row.get("en_us") or "").strip()
        zh = (row.get("zh_cn") or "").strip()
        if not en or not zh:
            continue
        rows.append({"key": (row.get("key") or "").strip(), "en_us": en, "zh_cn": zh})
    return rows


def namespace_of(key: str) -> str:
    """取 TSV 键的顶层命名空间，如 item.minecraft.xxx → item"""
    return key.split(".", 1)[0] if "." in key else ""


def is_term_like(en: str) -> bool:
    """判断是否像"名词术语"（而不是整句提示语 / 格式串）"""
    if FORMAT_CODE.search(en):
        return False
    if en.endswith(SENTENCE_END):
        return False
    words = en.split()
    if not words or len(words) > MAX_WORDS:
        return False
    return not (len(words) == 1 and words[0].lower() in STOP_WORDS)


def _priority(key: str) -> tuple:
    ns = namespace_of(key)
    rank = NS_PRIORITY.index(ns) if ns in NS_PRIORITY else len(NS_PRIORITY)
    return (rank, key)


def build_auto(rows: list[dict]) -> tuple[dict, dict, dict]:
    """从 TSV 行构建自动词表层，返回 (auto_glossary, index, stats)

    index 是溯源索引（术语 → TSV 键），只给人看、不进 prompt，
    所以单独落盘为 glossary.auto.index.json，不混进词表层。
    """
    kept: dict[str, list[tuple[str, str, str]]] = {}   # 小写英文 -> [(key, 原形, 中文)]
    stats = {
        "rows": len(rows),
        "dropped_namespace": 0,
        "dropped_not_term": 0,
        "dropped_untranslated": 0,
    }

    for row in sorted(rows, key=lambda r: _priority(r["key"])):
        key, en, zh = row["key"], row["en_us"], row["zh_cn"]
        if namespace_of(key) not in NS_WHITELIST:
            stats["dropped_namespace"] += 1
            continue
        if not is_term_like(en):
            stats["dropped_not_term"] += 1
            continue
        if zh == en or not re.search(r"[\u4e00-\u9fff]", zh):
            # 未汉化（Fern → Fern）或译文里根本没有汉字，说明上游还没翻译
            stats["dropped_untranslated"] += 1
            continue
        kept.setdefault(en.lower(), []).append((key, en, zh))

    hard: dict[str, str] = {}
    soft: dict[str, str] = {}
    soft_strict: dict[str, str] = {}
    ambiguous: dict[str, dict] = {}
    sources: dict[str, str] = {}
    for cands in kept.values():
        # 同名去重：优先保留首字母大写的 UI 写法，再按命名空间优先级
        cands.sort(key=lambda c: (c[1] != c[1][:1].upper() + c[1][1:], _priority(c[0])))
        key, en, zh = cands[0]
        others = sorted({z for _, _, z in cands if z != zh})
        sources[en] = key
        if len(en.split()) > 1:
            hard[en] = zh
        elif namespace_of(key) in NS_STANDALONE_ONLY:
            soft_strict[en] = zh
        else:
            soft[en] = zh
        if others:
            ambiguous[en] = {"primary": zh, "others": others}

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    auto = {
        "_comment": "由 tools/sync_glossary.py 从 CFPAOrg 术语表自动生成，请勿手改；"
                    "手写词条放在 glossary.json（优先级更高）。",
        "source": SOURCE_PAGE,
        "generated_at": now,
        "tier_policy": {
            "hard": "多词专有名词，命中即强制使用译名",
            "soft": "单词世界名词，仅在原样大小写匹配或整段独立出现时注入",
            "soft_strict": "状态效果/附魔名，仅在整段独立出现时注入",
        },
        "terms": dict(sorted(hard.items())),
        "soft": dict(sorted(soft.items())),
        "soft_strict": dict(sorted(soft_strict.items())),
        "ambiguous": dict(sorted(ambiguous.items())),
    }
    index = {
        "_comment": "溯源索引：术语 → CFPAOrg TSV 的原始键。仅供人工复核/调试，"
                    "运行时不会加载，也不进提示词。",
        "source": SOURCE_PAGE,
        "generated_at": now,
        "sources": dict(sorted(sources.items())),
    }
    stats["hard"] = len(hard)
    stats["soft"] = len(soft)
    stats["soft_strict"] = len(soft_strict)
    stats["ambiguous"] = len(ambiguous)
    stats["index"] = len(sources)
    return auto, index, stats


def fetch_tsv(url: str, timeout: int = 60) -> str:
    from urllib.request import urlopen

    with urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 固定 HTTPS 源
        return resp.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 CFPAOrg 术语表生成 glossary.auto.json")
    ap.add_argument("--tsv", help="本地 TSV 路径（默认从官方仓库下载）")
    ap.add_argument("--url", default=DEFAULT_URL, help="TSV 下载地址")
    ap.add_argument("--out", default=DEFAULT_OUT, help="词表输出文件（默认 glossary.auto.json）")
    ap.add_argument("--index-out", default=DEFAULT_INDEX_OUT,
                    help="溯源索引输出文件（默认 glossary.auto.index.json）")
    ap.add_argument("--no-index", action="store_true", help="不写溯源索引")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args(argv)

    text = Path(args.tsv).read_text(encoding="utf-8") if args.tsv else fetch_tsv(args.url)
    rows = parse_tsv(text)
    auto, index, stats = build_auto(rows)
    for name, value in stats.items():
        print(f"  {name}: {value}")

    if args.stats:
        return 0

    out = Path(args.out)
    out.write_text(json.dumps(auto, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {out} ({out.stat().st_size / 1024:.0f} KB)")

    if not args.no_index:
        index_out = Path(args.index_out)
        index_out.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {index_out} ({index_out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
