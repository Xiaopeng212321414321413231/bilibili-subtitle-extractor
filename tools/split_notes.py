"""把一个合集的分篇 note.md 批量转成「一期一个 HTML」，直接可投知识库。

默认交付形态就是这套：每讲一个独立 HTML，图片 base64 内嵌，读者在知识库里
按 P 号点开看。合并版是可选路径，别默认走。

命名规则 `Pnn-<标题>.html`：P 号前缀保证知识库里按集数排序，标题取
meta.json 的 part（没有就退到 title），并去掉「第N章-XX-」这种课程内部编号。

用法：
    python tools/split_notes.py --bv BV12omoB4ExF
    python tools/split_notes.py --bv BV1hbM9zrEwD --out notes/custom_dir

产出：
    notes/<BV>_SPLIT/P01-xxx.html ... （每个都是独立的单文件页面）
    notes/<BV>_SPLIT/_sizes.json     （文件名 -> 字节数，供 IMA create_media 用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.md_to_html import convert  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def safe(name: str) -> str:
    """文件名里不能出现的字符换掉；连续空白压成一个空格。"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return re.sub(r"\s+", " ", name)


def part_title(meta: dict, page: int) -> str:
    """取分P标题：优先 part，去掉「第N章-XX-」前缀和 .mp4 尾巴。"""
    raw = (meta.get("part") or meta.get("title") or "P%02d" % page).strip()
    raw = re.sub(r"\.mp4$", "", raw, flags=re.I)
    raw = re.sub(r"^第\s*\d+\s*章\s*-\s*\d+\s*-\s*", "", raw)
    return raw or "P%02d" % page


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bv", required=True)
    ap.add_argument("--out", default=None, help="输出目录，默认 notes/<BV>_SPLIT")
    ap.add_argument("--pages", default=None, help="只处理部分分P，如 1-10 或 1,3,5")
    a = ap.parse_args()

    out = a.out or os.path.join(ROOT, "notes", a.bv + "_SPLIT")
    os.makedirs(out, exist_ok=True)

    # 解析 --pages
    want = None
    if a.pages:
        want = set()
        for seg in a.pages.split(","):
            seg = seg.strip()
            if "-" in seg:
                lo, hi = seg.split("-", 1)
                want.update(range(int(lo), int(hi) + 1))
            elif seg:
                want.add(int(seg))

    rows, missing = [], []
    i = 1
    while True:
        d = os.path.join(ROOT, "notes", "%s_P%02d" % (a.bv, i))
        if not os.path.isdir(d):
            # 分P号连续；连续缺 3 个就认为到底了
            if i > 1 and not os.path.isdir(os.path.join(ROOT, "notes", "%s_P%02d" % (a.bv, i + 1))):
                break
            i += 1
            continue
        i += 1
        if want and (i - 1) not in want:
            continue
        md = os.path.join(d, "note.md")
        if not os.path.isfile(md):
            missing.append(i - 1)
            continue
        try:
            meta = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        name = safe("P%02d-%s.html" % (i - 1, part_title(meta, i - 1)))
        dst = os.path.join(out, name)
        convert(md, dst)
        rows.append((i - 1, name, os.path.getsize(dst)))

    if not rows:
        print("没找到任何 note.md（分篇要先写完）", file=sys.stderr)
        return 1

    print()
    for p, n, s in rows:
        print("%-5s %-46s %6.2f MB" % ("P%02d" % p, n[:44], s / 1048576))
    json.dump({n: s for _, n, s in rows},
              open(os.path.join(out, "_sizes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    total = sum(r[2] for r in rows)
    print()
    print("产出 %d 个文件，合计 %.2f MB，最大 %.2f MB -> %s"
          % (len(rows), total / 1048576, max(r[2] for r in rows) / 1048576, out))
    if missing:
        print("!! 这些分P没有 note.md，已跳过：%s" % missing, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
