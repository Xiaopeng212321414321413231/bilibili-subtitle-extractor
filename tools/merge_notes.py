# -*- coding: utf-8 -*-
"""多篇笔记 -> 一份完整笔记：**纯机械合并**。

设计原则（2026-10-29 定稿，来自一次返工的教训）：
    每一讲单独做完、单独自洽；合并这一步**不允许任何改写**。
    agent 只能在「分篇」阶段写作，到了合并阶段就退化成脚本。

机械合并只做三件事（都是无损的格式调整）：
    1. 每篇 H1 -> `## Pnn · 标题`，篇内 `##` -> `###`，篇内 `###` -> `####`（层级重编）
    2. 图片重名（各篇都叫 fig01_xxx.jpg）-> 加 `pNN_` 前缀后复制
    3. 在最前面加一页目录（自动生成）

**删除、改写、压缩、合并重复内容，一律不做。** 17 篇各自的「核心结论」「关键数据与术语」
「附：这 N 张图是怎么来的」全部原样保留 —— 重复是分篇自洽的代价，不是缺陷。
确实想去掉逐篇的素材说明时，显式加 `--drop-appendix`（默认关）。

用法：
    python tools/merge_notes.py                          # 纯机械合并，零改写
    python tools/merge_notes.py --drop-appendix           # 去掉逐篇「图是怎么来的」
    python tools/merge_notes.py --lead notes/xxx_ALL/lead # 额外注入外部导读（可选）
"""
import os
import re
import glob
import shutil
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 章 -> 讲次。这是本脚本里**唯一的人工编排信息**（只影响排序，不影响内容）。
DEFAULT_CHAPTERS = [
    ("第 1 章  赛前必读：规则决定策略", [1]),
    ("第 2 章  排序与分治", [2, 3]),
    ("第 3 章  二分", [4]),
    ("第 4 章  前缀和与差分", [5, 6]),
    ("第 5 章  双指针：两种完全不同的变体", [7, 8]),
    ("第 6 章  区间合并", [9]),
    ("第 7 章  深度优先搜索 DFS", [10, 11]),
    ("第 8 章  广度优先搜索 BFS", [12, 13]),
    ("第 9 章  数论四连：只枚举到 √n", [14, 15, 16, 17]),
]


def split_sections(text):
    """按 '^## ' 切分，返回 (preamble, [(title, body), ...])。"""
    chunks = re.split(r"^##[ \t]+", text, flags=re.M)
    preamble = chunks[0]
    secs = []
    for c in chunks[1:]:
        title, body = (c.split("\n", 1) + [""])[:2] if "\n" in c else (c, "")
        secs.append((title.strip(), body))
    return preamble, secs


def demote_headings(body, levels=1):
    """标题整体降 levels 级；跳过 ``` 代码块内部（那里的 # 是注释，不是标题）。"""
    out, in_code = [], False
    for ln in body.split("\n"):
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            out.append(ln)
            continue
        if not in_code:
            m = re.match(r"^(#{1,5})(\s+)(.*)$", ln)
            if m:
                ln = "#" * (len(m.group(1)) + levels) + m.group(2) + m.group(3)
        out.append(ln)
    return "\n".join(out)


def fix_fig_paths(text, part_no):
    """figs/xxx.jpg -> figs/pNN_xxx.jpg（各篇图重名，必须加前缀）"""
    return re.sub(r"\]\(figs/([^)]+)\)", lambda m: "](figs/p%02d_%s)" % (part_no, m.group(1)), text)


def rewrite_preamble(pre, part_no):
    """只改第一篇 H1 行（降级 + 去统一前缀），其余逐字保留。"""
    lines = pre.rstrip("\n").split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            title = re.sub(r"^蓝桥杯\s*Python\s*(组)?\s*[｜|]\s*", "", title)  # 去掉 17 次重复的前缀
            lines[i] = "## P%02d · %s" % (part_no, title)
            break
    return fix_fig_paths("\n".join(lines), part_no)


def load_parts(bv, out_figs):
    """读取全部分篇，返回 {讲次: dict}。图片顺带复制到合并目录。"""
    data = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "notes", "*_P[0-9][0-9]", "note.md"))):
        d = os.path.dirname(p)
        m = re.search(r"_P(\d+)$", d)
        if not m:
            continue
        if not os.path.basename(d).startswith(bv + "_"):
            continue
        no = int(m.group(1))
        text = open(p, encoding="utf-8").read()
        pre, secs = split_sections(text)

        # 复制本篇图片，加 pNN_ 前缀
        n_fig = 0
        fig_dir = os.path.join(d, "figs")
        if os.path.isdir(fig_dir):
            for fn in sorted(os.listdir(fig_dir)):
                if fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    shutil.copy2(os.path.join(fig_dir, fn),
                                 os.path.join(out_figs, "p%02d_%s" % (no, fn)))
                    n_fig += 1
        data[no] = dict(pre=pre, secs=secs, n_fig=n_fig, chars=len(text), path=d)
    return data


def main():
    api = argparse.ArgumentParser()
    api.add_argument("--bv", default="BV1hbM9zrEwD")
    api.add_argument("--out", default=None, help="输出目录，默认 notes/<BV>_ALL")
    api.add_argument("--drop-appendix", action="store_true",
                     help="去掉每篇末尾「附：这 N 张图是怎么来的」（默认保留）")
    api.add_argument("--lead", default=None,
                     help="可选：外部导读目录，ch01.md/ch02.md… 按章注入；不传则零人工内容")
    args = api.parse_args()

    out_dir = args.out or os.path.join(ROOT, "notes", args.bv + "_ALL")
    out_figs = os.path.join(out_dir, "figs")
    out_md = os.path.join(out_dir, "note.md")
    os.makedirs(out_figs, exist_ok=True)
    for fn in os.listdir(out_figs):          # 清掉上一轮产物，避免残留图
        if re.match(r"^p\d{2}_", fn):
            os.remove(os.path.join(out_figs, fn))

    data = load_parts(args.bv, out_figs)
    if not data:
        raise SystemExit("没找到分篇笔记：notes/%s_PNN/note.md" % args.bv)

    chapters = [(name, [p for p in parts if p in data]) for name, parts in DEFAULT_CHAPTERS]
    covered = {p for _, ps in chapters for p in ps}
    left = sorted(set(data) - covered)
    if left:                                  # 分组表没覆盖到的讲次，追加到末尾
        chapters.append(("第 %d 章  其他" % (len(chapters) + 1), left))

    out = []
    ap = out.append
    total_src = sum(v["chars"] for v in data.values())
    ap("# %s｜%d 讲合并笔记\n" % (args.bv, len(data)))
    ap("> 由 %d 篇独立笔记**机械合并**而成：正文一字未改，只重编了标题层级、给图片加了讲次前缀。\n"
       % len(data))
    ap("**目录**\n")
    for ci, (cname, parts) in enumerate(chapters, 1):
        ap("- **%s**（%s）" % (cname, "、".join("P%02d" % p for p in parts)))
    ap("\n---\n")

    total_fig = 0
    for ci, (cname, parts) in enumerate(chapters, 1):
        ap("\n# %s\n" % cname)
        if args.lead:                          # 可选导读，独立文件，不算合并产物
            lf = os.path.join(args.lead, "ch%02d.md" % ci)
            if os.path.isfile(lf):
                ap(open(lf, encoding="utf-8").read().strip() + "\n")
        for i, pno in enumerate(parts):
            d = data[pno]
            total_fig += d["n_fig"]
            if i > 0:                          # 第一讲紧跟章标题，不再多插分隔线
                ap("\n---\n")
            ap(rewrite_preamble(d["pre"], pno) + "\n")
            for title, body in d["secs"]:
                if args.drop_appendix and title.startswith("附："):
                    continue
                ap("### %s\n" % title)
                ap(fix_fig_paths(demote_headings(body, 1), pno).rstrip() + "\n")

    md = "\n".join(out)
    md = re.sub(r"\n{4,}", "\n\n\n", md)
    open(out_md, "w", encoding="utf-8").write(md)

    n_h2 = len(re.findall(r"^## ", md, re.M))
    n_h3 = len(re.findall(r"^### ", md, re.M))
    n_code = len(re.findall(r"^```", md, re.M)) // 2
    n_figref = len(re.findall(r"!\[", md))
    print("写出: %s" % out_md)
    print("分篇合计 %d 字 -> 合并 %d 字（%.0f%%）" % (total_src, len(md), len(md) / total_src * 100))
    print("章 %d | 讲 %d | 二级小节 %d | 三级小节 %d" % (len(chapters), len(data), n_h2, n_h3))
    print("代码块 %d | 图引用 %d | 复制图 %d" % (n_code, n_figref, total_fig))


if __name__ == "__main__":
    main()
