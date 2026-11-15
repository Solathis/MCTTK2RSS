"""bili — B站专栏相关的模块（投稿器 + 两套正文渲染器 + WBI 签名）

专栏有两套互斥的正文形态，各有自己的渲染器，投稿时二选一：
  html.py    老接口 `creative/draft/addupdate` 收的 HTML 正文（已真机验证）
  opus.py    新版编辑器 `dynamic/feed/article/draft/add` 收的 eva3 段落正文

放在一个包里是为了让「专栏」这件事的边界看得见：根目录只留通用模块，
投稿链路的改动范围一眼可数。共用工具仍在根目录的 utils.py。
"""

# 唯一保留超链接的地址：项目署名。
# 实测（2026-11-15 真机发布）B站 图文里点其余外链会跳到「离开 B站」提示页，读者看到的是
# 一个拦截页而不是内容，所以除署名外一律降级成纯文本；两种正文模型共用这一份策略。
SIGNATURE_LINKS = ("github.com/jiubook/MCTTK",)


def is_signature_link(url: str) -> bool:
    """是否是必须保留超链接的署名地址（其余外链统一降级成纯文本）。"""
    lowered = (url or "").lower()
    return any(pattern.lower() in lowered for pattern in SIGNATURE_LINKS)
