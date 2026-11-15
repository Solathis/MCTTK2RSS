#!/usr/bin/env python3
"""
tools/preview_bili_html.py — 把已翻译 JSON 渲染成可直接用浏览器查看的专栏预览页

用途：投稿前肉眼确认排版（标题、双语引用块、列表、图片、转义），不用真的发到 B站，
也不用先把产物写进 output/。预览页是**近似**外观：真正的排版由专栏编辑器决定，
这里只保证结构标签与阅读顺序，方便快速判断「这段中文在专栏里会不会难看」。

用法：
  uv run python tools/preview_bili_html.py                    # 自动找 output/news_*.json
  uv run python tools/preview_bili_html.py output/news_x.json --open
  uv run python tools/preview_bili_html.py --no-modules       # 不带配置里的头尾模块
  uv run python tools/preview_bili_html.py --renderer opus    # 看段模型下的排版

两种正文模型都能预览（发帖阶段二选一，见 converter.convert_to_bili_html /
convert_to_bili_opus）：
  html  —— 老接口收的 HTML 字符串，产物本身就是最终形态
  opus  —— 新版编辑器的段数组，这里先还原成近似 HTML 再看

产物：output/preview_<stem>.html（opus 模式为 preview_<stem>_opus.html；output/ 已被
.gitignore 忽略，不会入库）
"""

import argparse
import glob
import html
import json
import os
import sys
import webbrowser
from pathlib import Path
from string import Template

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bili_opus import PARA_HEADING, PARA_IMAGE, PARA_LIST, PARA_QUOTE  # noqa: E402
from converter import J2MMConverter  # noqa: E402
from log_setup import log_error, log_info, log_warning, setup_logging  # noqa: E402
from utils import CONFIG_DIR  # noqa: E402

# 近似 B站专栏的阅读样式：720px 正文宽度、1.8 行高、引用块浅底。
# 用 Template 而不是 str.format，避免 CSS 里的花括号需要转义。
_PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  body {
    margin: 0; background: #f4f5f7; color: #18191c;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Microsoft YaHei", sans-serif;
  }
  .notice {
    max-width: 720px; margin: 16px auto 0; padding: 10px 14px;
    background: #fff7e6; border: 1px solid #ffd591; border-radius: 6px;
    font-size: 13px; line-height: 1.7; color: #874d00;
  }
  article {
    max-width: 720px; margin: 16px auto 48px; padding: 32px 40px;
    background: #fff; border-radius: 8px;
  }
  h1 { font-size: 24px; line-height: 1.5; margin: 0 0 12px; font-weight: 600; }
  p { font-size: 16px; line-height: 1.8; margin: 0 0 16px; word-break: break-word; }
  em { color: #61666d; }
  strong { font-weight: 600; }
  blockquote {
    margin: 0 0 16px; padding: 8px 16px; border-left: 3px solid #e3e5e7;
    background: #f7f8fa; border-radius: 0 4px 4px 0;
  }
  blockquote p { font-size: 15px; line-height: 1.7; color: #61666d; margin: 0 0 8px; }
  blockquote p:last-child { margin-bottom: 0; }
  ul { margin: 0 0 16px; padding-left: 24px; }
  li { font-size: 16px; line-height: 1.8; margin-bottom: 6px; }
  li blockquote { margin: 6px 0 0; }
  figure { margin: 0 0 16px; text-align: center; }
  figure img { max-width: 100%; height: auto; border-radius: 6px; }
  pre { margin: 0 0 16px; padding: 12px 16px; background: #f6f8fa; border-radius: 6px; }
  code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 14px; }
  a { color: #00a1d6; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="notice">
  MCTTK 专栏排版预览（近似样式，非 B站官方排版）：正式投稿时排版由专栏编辑器决定，
  正文图片会在发帖阶段重托管到 B站 CDN，这里显示的仍是原站外链。
  <br>来源：$source
</div>
<article>
$body
</article>
</body>
</html>
""")


def build_preview_page(fragment: str, *, title: str, source: str) -> str:
    """把正文片段包进带样式的预览页。

    fragment 是我们自己生成的 HTML（可信）；title / source 来自 JSON 与文件系统，
    一律转义，避免标题里的尖括号把页面结构写坏。
    """
    return _PAGE_TEMPLATE.substitute(
        title=html.escape(title or "未命名文章", quote=True),
        source=html.escape(source, quote=True),
        body=fragment,
    )


def load_modules(path: Path | None) -> dict | None:
    """读取模块配置；文件不存在或坏 JSON 时返回 None（预览不必因此失败）。"""
    if path is None or not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log_warning("[预览] 模块配置读取失败，本次不带模块: %s（%s）", path, e)
        return None


def find_default_inputs(output_dir: Path) -> list[Path]:
    """默认输入：output 下的 news_*.json（排除 .state.json 之类的非文章 JSON）。"""
    return sorted(Path(p) for p in glob.glob(os.path.join(str(output_dir), "news_*.json")))


def _nodes_to_html(nodes: list[dict]) -> str:
    """段落节点 → 行内 HTML（文字 / 链接，粗体包 <strong>）。"""
    out = []
    for node in nodes or []:
        if node.get("node_type") == 4:
            link = node.get("link") or {}
            label = html.escape(link.get("show_text") or "", quote=True)
            url = html.escape(link.get("link") or "", quote=True)
            text = f'<a href="{url}">{label}</a>'
            if (link.get("style") or {}).get("bold"):
                text = f"<strong>{text}</strong>"
            out.append(text)
            continue
        word = node.get("word") or {}
        text = html.escape(word.get("words") or "", quote=True).replace("\n", "<br>")
        if (word.get("style") or {}).get("bold"):
            text = f"<strong>{text}</strong>"
        out.append(text)
    return "".join(out)


def _paragraph_nodes(paragraph: dict) -> list[dict]:
    return (paragraph.get("text") or {}).get("nodes") or []


def _quote_group(paragraph: dict) -> str | None:
    return (paragraph.get("format") or {}).get("combine_hash")


def _list_html(paragraphs: list[dict], start: int) -> tuple[str, int]:
    """把一段连续的列表段还原成嵌套 <ul>（与 HTML 渲染器同样的嵌套算法）。"""
    group = _quote_group(paragraphs[start])
    parts: list[str] = []
    levels: list[int] = []
    index = start
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if paragraph.get("para_type") != PARA_LIST or _quote_group(paragraph) != group:
            break
        fmt = paragraph.get("format") or {}
        level = int((fmt.get("list_format") or {}).get("level") or 1)
        while levels and level < levels[-1]:
            parts.append("</li></ul>")
            levels.pop()
        if not levels:
            parts.append("<ul>")
            levels.append(level)
        elif level > levels[-1]:
            # 上一项 <li> 保持打开，子列表直接放进去
            parts.append("<ul>")
            levels.append(level)
        else:
            parts.append("</li>")
        parts.append(f"<li>{_nodes_to_html(_paragraph_nodes(paragraph))}")
        index += 1
    while levels:
        parts.append("</li></ul>")
        levels.pop()
    return "".join(parts), index


def paragraphs_to_html(paragraphs: list[dict]) -> str:
    """eva3 段数组 → 近似 HTML，供浏览器预览（正式排版仍由专栏编辑器决定）。

    分组照抄编辑器的 combine_hash 语义：相邻且同组的引用段并成一个 <blockquote>，
    列表段并成一个 <ul>——否则预览会把一个引用框拆成一堆小框，看着比实际更糟。
    """
    parts: list[str] = []
    index = 0
    total = len(paragraphs)
    while index < total:
        paragraph = paragraphs[index]
        ptype = paragraph.get("para_type")
        if ptype == PARA_QUOTE:
            group = _quote_group(paragraph)
            body = []
            while index < total:
                current = paragraphs[index]
                if current.get("para_type") != PARA_QUOTE or _quote_group(current) != group:
                    break
                body.append(f"<p>{_nodes_to_html(_paragraph_nodes(current))}</p>")
                index += 1
            parts.append("<blockquote>" + "".join(body) + "</blockquote>")
        elif ptype == PARA_LIST:
            rendered, index = _list_html(paragraphs, index)
            parts.append(rendered)
        elif ptype == PARA_HEADING:
            parts.append(f"<h1>{_nodes_to_html(_paragraph_nodes(paragraph))}</h1>")
            index += 1
        elif ptype == PARA_IMAGE:
            pics = (paragraph.get("pic") or {}).get("pics") or []
            for pic in pics:
                src = html.escape(pic.get("url") or "", quote=True)
                alt = html.escape(pic.get("comment") or "", quote=True)
                parts.append(f'<figure><img src="{src}" alt="{alt}"></figure>')
            index += 1
        else:
            parts.append(f"<p>{_nodes_to_html(_paragraph_nodes(paragraph))}</p>")
            index += 1
    return "\n".join(part for part in parts if part)


def render_json_file(json_path: Path, out_path: Path, modules: dict | None = None,
                     renderer: str = "html") -> Path:
    """渲染单篇文章为预览页，返回产物路径。

    renderer=opus 时先转成 eva3 段数组，再还原成近似 HTML——两种模型的预览入口
    保持同一个函数，切换只看一个参数。
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "blocks" not in data:
        raise ValueError(f"{json_path} 不是单篇文章 JSON（缺少 blocks）")

    converter = J2MMConverter(modules)
    if renderer == "opus":
        fragment = paragraphs_to_html(converter.convert_to_bili_opus(data))
    else:
        fragment = converter.convert_to_bili_html(data)
    title = data.get("translated_title") or data.get("title") or json_path.stem

    page = build_preview_page(fragment, title=title, source=f"{json_path}（{renderer} 渲染）")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把已翻译 JSON 渲染成浏览器可看的 B站专栏预览页",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tools/preview_bili_html.py\n"
            "  python tools/preview_bili_html.py output/news_x.json --open\n"
            "  python tools/preview_bili_html.py --no-modules\n"
            "  python tools/preview_bili_html.py --renderer opus\n"
        ),
    )
    parser.add_argument("json_files", nargs="*", help="要预览的 JSON（默认 output/news_*.json）")
    parser.add_argument("-o", "--out", help="输出文件（仅单个输入时可用）")
    parser.add_argument("-m", "--modules", help="模块配置（默认 config/modules_config.json）")
    parser.add_argument("--no-modules", action="store_true", help="不带头尾模块，只看正文")
    parser.add_argument("--renderer", choices=("html", "opus"), default="html",
                        help="正文模型：html=老接口（默认），opus=段模型")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = parser.parse_args()

    setup_logging()

    output_dir = Path("output")
    inputs = [Path(p) for p in args.json_files] or find_default_inputs(output_dir)
    if not inputs:
        log_error("[预览] 没有找到 JSON：%s（先运行 main.py --scrape-only 生成）", output_dir)
        return 1

    modules_path = None
    if not args.no_modules:
        modules_path = Path(args.modules) if args.modules else Path(CONFIG_DIR) / "modules_config.json"
    modules = load_modules(modules_path)
    if modules:
        log_info("[预览] 已加载模块配置: %s（正文模型 %s）", modules_path, args.renderer)
    else:
        log_info("[预览] 不带头尾模块（正文模型 %s）", args.renderer)

    if args.out and len(inputs) > 1:
        log_error("[预览] 多个输入时不能用 -o 指定单个输出文件")
        return 1

    results = []
    suffix = "" if args.renderer == "html" else f"_{args.renderer}"
    for json_path in inputs:
        out_path = Path(args.out) if args.out else output_dir / f"preview_{json_path.stem}{suffix}.html"
        try:
            page = render_json_file(json_path, out_path, modules, renderer=args.renderer)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log_error("[预览] 渲染失败: %s（%s）", json_path, e)
            continue
        log_info("[预览] %s → %s", json_path.name, page)
        results.append(page)

    if not results:
        return 1

    for page in results:
        log_info("[预览] 浏览器打开: %s", page.resolve().as_uri())
    if args.open:
        webbrowser.open(results[0].resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
