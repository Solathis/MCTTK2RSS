"""bili — B站专栏相关的三个模块（投稿器 + 两套正文渲染器）

专栏有两套互斥的正文形态，各有自己的渲染器，投稿时二选一：
  html.py    老接口 `creative/draft/addupdate` 收的 HTML 正文（已真机验证）
  opus.py    新版编辑器 `dynamic/feed/article/draft/add` 收的 eva3 段落正文

放在一个包里是为了让「专栏」这件事的边界看得见：根目录只留通用模块，
投稿链路的改动范围一眼可数。共用工具仍在根目录的 utils.py。
"""
