#!/usr/bin/env python3
"""utils.py — 项目公共工具函数"""

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Callable

# ── 项目路径 ───────────────────────────────────────────
# 源码与入口脚本留在根目录；配置放 config/，词表放 glossary/。
# 各模块统一从这里取路径，避免各自 os.path.dirname(__file__) 走样。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
GLOSSARY_DIR = os.path.join(PROJECT_ROOT, "glossary")


# ── 产物与状态（MCBBS / B站 两个平台共用）───────────────

def find_image(news_dir: str, stem: str) -> str | None:
    """找文章同名题图（支持常见后缀），找不到返回 None。"""
    for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
        img_path = os.path.join(news_dir, stem + ext)
        if os.path.isfile(img_path):
            return img_path
    return None


def load_posted(state_file: str) -> set:
    """读取「已发布」记录（文件名 stem 集合）。文件不存在时返回空集合。"""
    if os.path.isfile(state_file):
        with open(state_file, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def _atomic_write_json(path: str, payload, tmp_prefix: str) -> None:
    """原子写 JSON。排序/裁剪由调用方负责，这里只管落盘方式。

    先写同目录临时文件再 os.replace：直接 `open(..., "w")` 会先把文件截断，
    这时进程被 kill / 断电就留下半截 JSON，下次读取直接 json 解析炸掉。
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=tmp_prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def save_posted(state_file: str, posted: set) -> None:
    """写入「已发布」记录。排序后落盘，diff 才稳定。"""
    _atomic_write_json(state_file, sorted(posted), ".posted-")


# ── MCBBS 帖子记录（发帖之后：tid、高亮、审核状态）──────

THREAD_RECORDS_NAME = ".mcbbs_threads.json"


def thread_records_path(save_dir: str) -> str:
    """帖子记录文件路径。

    和 `.posted.json`（发了没发）是两个语义：这里记的是「发出去之后怎么样了」，
    比如帖子 ID、有没有过审、高亮提交过什么颜色。
    """
    return os.path.join(save_dir, THREAD_RECORDS_NAME)


def load_thread_records(path: str) -> dict:
    """读取帖子记录（stem → 记录 dict）；文件缺失或内容损坏都返回空 dict。

    记录只用于补高亮和排障：读不到最多多花一次请求重新对齐，不值得让整轮发布
    因为一个坏文件停下来，所以这里不抛异常（`load_posted` 抛，是因为它决定发不发）。
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_thread_records(path: str, records: dict) -> None:
    """写入帖子记录。按 stem 排序后落盘，diff 才稳定。"""
    ordered = {stem: records[stem] for stem in sorted(records)}
    _atomic_write_json(path, ordered, ".threadrecords-")

MODULE_TYPE_MAP = {
    'module_java_snapshot_header': 'java_snapshot',
    'module_java_snapshot_footer': 'java_snapshot',
    'module_java_prerelease_header': 'java_prerelease',
    'module_java_prerelease_footer': 'java_prerelease',
    'module_java_rc_header': 'java_rc',
    'module_java_rc_footer': 'java_rc',
    'module_java_release_header': 'java_release',
    'module_java_release_footer': 'java_release',
    'module_bedrock_beta_header': 'bedrock_beta',
    'module_bedrock_beta_footer': 'bedrock_beta',
    'module_bedrock_release_header': 'bedrock_release',
    'module_bedrock_release_footer': 'bedrock_release',
    'module_commentary_header': 'commentary',
    'module_commentary_footer': 'commentary',
    'module_normal_header': 'normal',
    'module_normal_footer': 'normal',
}


def load_dotenv(project_dir: str = None) -> None:
    """加载同目录下的 .env 文件到环境变量（已存在的变量不覆盖）"""
    if project_dir is None:
        project_dir = PROJECT_ROOT
    env_path = os.path.join(project_dir, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def classify_article_type(
    title: str,
    *,
    chinese: bool = False,
    commentary: bool = False,
    fallback: str | None = "other",
) -> str | None:
    """
    统一的文章类型分类核心逻辑。

    Args:
        title: 文章标题
        chinese: True 时额外检测中文关键词（快照/预发布/候选/预览/基岩）
        commentary: True 时额外检测"时评/commentary"类型
        fallback: 无匹配时的返回值（"other"、"normal" 或 None）
    """
    t = title or ""
    t_lower = t.lower()

    # Java 版本（优先级高）
    if "snapshot" in t_lower or (chinese and "快照" in t):
        return "java_snapshot"
    if (
        "pre-release" in t_lower
        or "pre release" in t_lower
        or "prerelease" in t_lower
        or (chinese and "预发布" in t)
    ):
        return "java_prerelease"
    if "release candidate" in t_lower or (chinese and "候选" in t):
        return "java_rc"

    # 基岩版本
    if "beta" in t_lower or "preview" in t_lower or "预览" in t:
        return "bedrock_beta"
    if "bedrock" in t_lower or "基岩" in t:
        return "bedrock_release"

    # 时评（可选）
    if commentary and ("时评" in t or "commentary" in t_lower):
        return "commentary"

    # Java 正式版
    if "java edition" in t_lower or "java版" in t or re.search(r'\b1\.\d+(\.\d+)?\b', t):
        return "java_release"

    return fallback


# ── Markdown 行内语法扫描（BBCode / HTML / 段落三套产物共用）──

def find_closing_bracket(text: str, open_pos: int, opener: str, closer: str) -> int:
    """返回与 text[open_pos] 处 opener 匹配的 closer 下标；找不到返回 -1。"""
    depth = 0
    for i in range(open_pos, len(text)):
        ch = text[i]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def rewrite_md_links(text: str, render_link: Callable[[str, str], str]) -> str:
    """按配对扫描 Markdown 链接 ``[文字](URL)``，把每个链接交给 render_link 格式化。

    BBCode / HTML / 段落三种产物只有最终格式不同，扫描逻辑必须共用一份：
    链接文字允许含嵌套方括号（如 ``[9月30日：生物及更多内容！[预览版/测试版]](url)``），
    URL 允许含括号（如 ``https://en.wikipedia.org/wiki/A_(b)``）；
    因此不能用简单的正则匹配，需要按括号配对扫描。
    """
    if not text:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != '[':
            out.append(text[i])
            i += 1
            continue
        # 图片语法已在上一步转成相应标记，这里跳过 ![ 避免被当成链接
        if i > 0 and text[i - 1] == '!':
            out.append(text[i])
            i += 1
            continue
        # 链接文字：方括号需配对，例如 [a[b]c]
        text_end = find_closing_bracket(text, i, '[', ']')
        if text_end < 0:
            out.append(text[i:])
            break
        if text_end + 1 >= n or text[text_end + 1] != '(':
            # 不是 Markdown 链接，原样保留
            out.append(text[i:text_end + 1])
            i = text_end + 1
            continue
        # URL：圆括号需配对（标题行等可含换行，各方言里压成空格）
        url_end = find_closing_bracket(text, text_end + 1, '(', ')')
        if url_end < 0:
            out.append(text[i:text_end + 1])
            i = text_end + 1
            continue
        label = text[i + 1:text_end].replace('\n', ' ')
        url = text[text_end + 2:url_end].replace('\n', ' ')
        if label and url:
            out.append(render_link(label, url))
        else:
            out.append(text[i:url_end + 1])
        i = url_end + 1
    return ''.join(out)
