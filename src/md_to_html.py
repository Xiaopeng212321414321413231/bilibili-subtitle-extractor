"""极简 Markdown -> 单文件 HTML（图片内嵌 base64），用于把图文笔记做成可直接看的页面。

只支持笔记里实际用到的语法子集：标题、段落、引用、有序/无序列表、代码块、表格、
图片、水平线、粗体、行内代码。刻意不引第三方库——渲染结果可预测，也不给 skill 加依赖。
"""
from __future__ import annotations

import base64
import html
import os
import re
import sys

_CSS = """
/* 刻意只用中性灰阶：笔记是拿来读的，不是拿来炫的。
   彩色强调会增加视觉噪音，也会让「哪些是重点」这个判断被颜色替代掉。
   需要强调就用字号、粗细、留白——这些比颜色更稳。 */
:root { --ink:#1f2328; --sub:#5b6472; --line:#e6e8ec; --bg:#fbfbfc; }
* { box-sizing: border-box; }
body {
  margin:0; background:#f4f5f7; color:var(--ink);
  font: 16px/1.85 -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
        "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.page { max-width: 820px; margin: 0 auto; background:#fff;
        padding: 56px 64px 72px; box-shadow: 0 1px 3px rgba(16,24,40,.06); }
h1 { font-size: 30px; line-height:1.35; letter-spacing:-.01em; margin: 0 0 18px; }
h2 { font-size: 21px; margin: 46px 0 14px; padding-top: 22px; border-top:1px solid var(--line); }
h3 { font-size: 17px; margin: 28px 0 10px; }
p { margin: 12px 0; }
blockquote { margin: 18px 0; padding: 14px 18px; background:#fafbfc;
             border-left: 3px solid var(--line); color:#28322f; border-radius: 2px; }
blockquote p { margin: 4px 0; }
ul, ol { margin: 12px 0; padding-left: 22px; }
li { margin: 6px 0; }
img { display:block; width:100%; height:auto; margin: 22px 0 8px;
      border:1px solid var(--line); border-radius: 6px; background:#fff; }
em { display:block; color: var(--sub); font-size: 13.5px; font-style: normal;
     margin: 0 0 22px; padding-left: 2px; }
code { font-family: "Cascadia Code", Consolas, "SF Mono", Menlo, monospace;
       font-size: .9em; background:#f2f4f6; padding: 1px 5px; border-radius: 3px; }
pre { background:#f7f8fa; border:1px solid var(--line); border-radius: 6px;
      padding: 16px 18px; overflow-x:auto; margin: 16px 0; }
pre code { background:none; padding:0; font-size: 13.5px; line-height: 1.7; }
table { border-collapse: collapse; width:100%; margin: 18px 0; font-size: 14.5px; }
th, td { border-bottom:1px solid var(--line); padding: 9px 12px; text-align:left; }
th { background:#fafbfc; font-weight: 600; color:#374151; }
hr { border:0; border-top:1px solid var(--line); margin: 34px 0; }
strong { font-weight: 650; }
a { color: var(--accent); text-decoration: none; border-bottom:1px solid rgba(47,111,94,.3); }
@media (max-width: 760px) { .page { padding: 28px 20px 40px; } h1 { font-size: 24px; } }
"""


def _inline(text: str) -> str:
    """行内：先转义，再还原 **粗体**、`代码`、[链接](url)。"""
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


_MISSING: list = []          # 找不到源文件的图，最后统一告警（不许静默降级成外链坏图）


def _img_tag(src: str, alt: str, base_dir: str) -> str:
    path = src if os.path.isabs(src) else os.path.join(base_dir, src)
    if not os.path.exists(path):
        _MISSING.append(src)
        return f'<img alt="{html.escape(alt)}" src="{html.escape(src)}">'
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "jpeg"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f'<img alt="{html.escape(alt)}" src="data:image/{mime};base64,{b64}">'


def convert(md_path: str, out_path: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(md_path))
    _MISSING.clear()
    with open(md_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 代码块
        if line.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue

        # 图片 + 紧随其后的斜体说明
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", line.strip())
        if m:
            out.append(_img_tag(m.group(2), m.group(1), base_dir))
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines) and lines[i].strip().startswith("*") and lines[i].strip().endswith("*"):
                cap = lines[i].strip().strip("*")
                out.append(f"<em>{_inline(cap)}</em>")
                i += 1
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(
                r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            header = cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(
                f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 水平线
        if line.strip() in ("---", "***", "___"):
            out.append("<hr>")
            i += 1
            continue

        # 引用块
        if line.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + "".join(
                f"<p>{_inline(b)}</p>" for b in buf if b) + "</blockquote>")
            continue

        # 列表
        if re.match(r"^\s*([-*]|\d+\.)\s+", line):
            ordered = bool(re.match(r"^\s*\d+\.", line))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]))
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
            continue

        # 空行/段落
        if not line.strip():
            i += 1
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|>|\s*([-*]|\d+\.)\s|```|\||!\[|---)", lines[i]):
            buf.append(lines[i])
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")

    body = "\n".join(out)
    title = ""
    m = re.search(r"<h1>(.*?)</h1>", body)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1))

    doc = (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">'
           f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
           f'<body><div class="page">{body}</div></body></html>')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python -m src.md_to_html <note.md> <note.html>")
        raise SystemExit(2)
    print("已生成:", convert(sys.argv[1], sys.argv[2]))
    if _MISSING:
        print("!! 有 %d 张图找不到源文件，已降级成外链（打开页面会是坏图）：" % len(_MISSING))
        for s in _MISSING[:10]:
            print("   -", s)
        if len(_MISSING) > 10:
            print("   ... 还有 %d 张" % (len(_MISSING) - 10))
        raise SystemExit(1)
