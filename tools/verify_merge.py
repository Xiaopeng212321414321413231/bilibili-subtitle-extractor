# -*- coding: utf-8 -*-
"""合并产物保真校验：四维硬校验，任一维有丢失就以退出码 1 结束。

用途：`tools/merge_notes.py` 跑完后**必须**紧接着跑这个。字数不作数
（101% 也可能删了大段又补了废话），只有下面四维全过才算合并成功。

    ① 标题   分篇每个 `^## ` 标题，必须原样出现在合并版（只允许层级变化）
    ② 段落   分篇每个非代码段落，归一化空白后必须能在合并版里检索到
    ③ 代码块 分篇每个代码围栏内的正文（≥20 字），去空白后必须能检索到
    ④ 图片   MD 引用数 == 实际文件数 == HTML 内嵌数，且没有外链图

用法：
    python tools/verify_merge.py --bv BV1hbM9zrEwD
    python tools/verify_merge.py --bv BV1hbM9zrEwD --all notes/xxx_ALL   # 指定合并目录
"""
import os
import re
import sys
import glob
import argparse


def norm(s):
    return re.sub(r"\s+", "", s)


def load_part_headings(text):
    return [h.strip() for h in re.findall(r"^##\s+(.+?)\s*$", text, re.M)]


def pinpoint(para, merged_norm):
    """段落整体匹配不上时，定位到具体缺失的那一行，方便排查。"""
    for line in para.split("\n"):
        ln = norm(line)
        if len(ln) >= 12 and ln not in merged_norm:
            return line.strip()[:60]
    return para.strip()[:50].replace("\n", " ")


def paragraphs_no_code(text):
    t = re.sub(r"```.*?```", "", text, flags=re.S)
    out = []
    for para in re.split(r"\n\s*\n", t):
        p = para.strip()
        if len(p) < 25:
            continue
        if p.startswith("#") or p.startswith("---") or p.startswith("!["):
            continue
        out.append(p)
    return out


def code_blocks(text):
    out = []
    for m in re.finditer(r"```(\w*)\n(.*?)```", text, re.S):
        body = m.group(2)
        if len(re.sub(r"\s", "", body)) >= 20:
            out.append(body)
    return out


def main():
    api = argparse.ArgumentParser()
    api.add_argument("--bv", default="BV1hbM9zrEwD")
    api.add_argument("--all", default=None, help="合并产物目录，默认 notes/<BV>_ALL")
    api.add_argument("-q", "--quiet", action="store_true", help="只输出结论")
    args = api.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    all_dir = args.all or os.path.join(root, "notes", args.bv + "_ALL")
    md_path = os.path.join(all_dir, "note.md")
    html_path = os.path.join(all_dir, "note.html")
    if not os.path.isfile(md_path):
        print("找不到合并版: %s" % md_path)
        return 2

    merged = open(md_path, encoding="utf-8").read()
    merged_norm = norm(merged)
    merged_heads = set(re.findall(r"^#{2,5}\s+(.+?)\s*$", merged, re.M))

    parts = sorted(glob.glob(os.path.join(root, "notes", args.bv + "_P[0-9][0-9]", "note.md")))
    if not parts:
        print("找不到分篇笔记: notes/%s_PNN/note.md" % args.bv)
        return 2

    res = []
    # ① 标题
    tot = 0
    miss = []
    for p in parts:
        for h in load_part_headings(open(p, encoding="utf-8").read()):
            tot += 1
            if h not in merged_heads:
                miss.append("%s: %s" % (os.path.basename(os.path.dirname(p)), h))
    res.append(("① 标题", tot, len(miss), miss))

    # ② 段落
    tot = 0
    miss = []
    for p in parts:
        for para in paragraphs_no_code(open(p, encoding="utf-8").read()):
            tot += 1
            if norm(para) not in merged_norm:
                miss.append("%s: %s" % (os.path.basename(os.path.dirname(p)), pinpoint(para, merged_norm)))
    res.append(("② 段落", tot, len(miss), miss))

    # ③ 代码块
    tot = 0
    miss = []
    for p in parts:
        for body in code_blocks(open(p, encoding="utf-8").read()):
            tot += 1
            if norm(body) not in merged_norm:
                miss.append("%s: %s…" % (os.path.basename(os.path.dirname(p)), body.strip()[:40].replace("\n", " / ")))
    res.append(("③ 代码块", tot, len(miss), miss))

    # ④ 图片
    refs = re.findall(r"\]\(figs/([^)]+)\)", merged)
    missing_file = [r for r in refs if not os.path.isfile(os.path.join(all_dir, "figs", r))]
    n_embedded = 0
    n_external = 0
    img_bad = []
    warn = []
    if missing_file:
        img_bad.append("缺文件 %d 个: %s" % (len(missing_file), missing_file[:3]))
    if os.path.isfile(html_path):
        # HTML 比 md 旧时里面的内容必然对不上，这不是合并出错，先提示重新生成
        if os.path.getmtime(html_path) < os.path.getmtime(md_path):
            warn.append("note.html 比 note.md 旧，已跳过内嵌检查；先重新生成 HTML 再验收")
        else:
            html = open(html_path, encoding="utf-8").read()
            n_embedded = html.count("data:image")
            n_external = len(re.findall(r'<img[^>]+src="(?!data:)', html))
            if n_embedded != len(refs):
                img_bad.append("HTML 内嵌 %d != MD 引用 %d" % (n_embedded, len(refs)))
            if n_external:
                img_bad.append("有 %d 张外链图（未内嵌，等于坏图）" % n_external)
    res.append(("④ 图片", len(refs), len(img_bad), img_bad))

    ok = all(lost == 0 for _, _, lost, _ in res)
    if not args.quiet:
        print("合并版: %s" % md_path)
        print("分篇数: %d\n" % len(parts))
        print("%-8s %8s %8s  %s" % ("维度", "总量", "丢失", "结果"))
        print("-" * 46)
        for name, total, lost, detail in res:
            print("%-8s %8d %8d  %s" % (name, total, lost, "OK" if lost == 0 else "!! 有丢失"))
        print()
    for w in warn:
        print("[提示] %s" % w)
    if ok:
        print("验收通过：四维零丢失（%s 字，分篇合计 %s 字）" % (
            f"{len(merged):,}", f"{sum(len(open(p, encoding='utf-8').read()) for p in parts):,}"))
        return 0
    for name, _, lost, detail in res:
        if lost:
            print("\n[%s] 丢失 %d 项：" % (name, lost))
            for d in detail[:8]:
                print("  - %s" % d)
    print("\n验收失败：合并产物被改写过，重跑 tools/merge_notes.py，不要手工修补。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
