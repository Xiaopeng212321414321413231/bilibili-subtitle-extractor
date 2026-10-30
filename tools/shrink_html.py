"""把 note.html 里的内嵌图片重压一遍，把整包压进 IMA 的 10MB 上限。

IMA 的 create_media 对 html 类型限制 10485760 字节（media_type 20）。
原始 note.html 的图是抓帧原图（720P、quality 64），134 张就能撑到 18MB。
学习资料在屏幕上通常按 820px 宽显示，把图压到 820px / quality 48 视觉上几乎无损，
体积能降到 1/3 左右。

用法：
    python tools/shrink_html.py notes/<BV>_ALL/note.html [--width 820] [--quality 48] [--target 9.8]

输出 note_light.html；若压完仍超 target（MB），自动降一档再压。
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys

from PIL import Image

PAT = re.compile(r"data:image/(jpeg|jpg|png);base64,([A-Za-z0-9+/=]+)")


def shrink(src: str, dst: str, width: int, quality: int) -> tuple:
    t = open(src, encoding="utf-8").read()
    stat = {"n": 0, "before": 0, "after": 0, "fail": 0}

    def repl(m):
        raw = base64.b64decode(m.group(2))
        stat["n"] += 1
        stat["before"] += len(raw)
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            if im.width > width:
                im = im.resize((width, max(1, int(im.height * width / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            out = buf.getvalue()
        except Exception:  # noqa: BLE001  单张图坏了就原样留着，不让整包失败
            stat["fail"] += 1
            out = raw
        stat["after"] += len(out)
        return "data:image/jpeg;base64," + base64.b64encode(out).decode()

    out = PAT.sub(repl, t)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out)
    stat["size"] = os.path.getsize(dst)
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="note.html 路径")
    ap.add_argument("--width", type=int, default=820, help="图片最大宽度（默认 820）")
    ap.add_argument("--quality", type=int, default=48, help="JPEG 质量（默认 48）")
    ap.add_argument("--target", type=float, default=9.8, help="目标体积上限 MB（默认 9.8）")
    ap.add_argument("--out", default=None, help="输出路径，默认同目录 note_light.html")
    a = ap.parse_args()

    dst = a.out or os.path.join(os.path.dirname(os.path.abspath(a.src)), "note_light.html")
    limit = a.target * 1024 * 1024

    # 超了就逐档降：宽度不变，只降质量（宽度再降字就看不清了）
    for q in (a.quality, 42, 36, 30, 24):
        st = shrink(a.src, dst, a.width, q)
        mb = st["size"] / 1048576
        print("q=%-3d %d 张图 %.1fMB -> %.1fMB | 输出 %.2fMB%s"
              % (q, st["n"], st["before"] / 1048576, st["after"] / 1048576, mb,
                 "" if st["fail"] == 0 else "  （%d 张解码失败已原样保留）" % st["fail"]))
        if st["size"] <= limit:
            print("OK:", dst)
            return 0
    print("!! 即使压到 q=24 仍超过 %.1fMB，请调小 --width 或拆分内容" % a.target, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
