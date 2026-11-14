#!/usr/bin/env python3
"""utils.py — 项目公共工具函数"""

import os
import re

# ── 项目路径 ───────────────────────────────────────────
# 源码与入口脚本留在根目录；配置放 config/，词表放 glossary/。
# 各模块统一从这里取路径，避免各自 os.path.dirname(__file__) 走样。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
GLOSSARY_DIR = os.path.join(PROJECT_ROOT, "glossary")

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
