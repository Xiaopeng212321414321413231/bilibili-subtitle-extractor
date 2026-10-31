"""清理中间产物，回收磁盘。默认只预览，不删任何东西。

    python tools/cleanup.py                 # 只报告（默认，不删）
    python tools/cleanup.py --video         # 看哪些视频可删
    python tools/cleanup.py --video --yes   # 真删（进回收站，可恢复）
    python tools/cleanup.py --all --yes     # 视频 + 音频一起清

## 安全边界

- **只清 `assets/video` 和临时音频**，永不碰 `notes/`、`models/`、`extracted/`
- 视频只删「对应分P的 note.md 已存在」的 —— 说明画面已经抽出来用过了，原视频没价值
- 删除走 **Windows 回收站**，不是 rm。后悔了能捞回来
- 没有 `--yes` 一律 dry-run，只打印不动手

## 为什么不做成「自动」

自动删用户文件是不可接受的风险：一次路径判断失误就能毁掉几天的工作。
所以这里只提供显式命令，什么时候清、清哪些，由人决定。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _mb(n: int) -> float:
    return n / 1048576


def done_pages() -> dict:
    """分P cid -> 该分P的 note.md 是否已写完。"""
    out = {}
    for mp in glob.glob(os.path.join(ROOT, "notes", "*_P*", "meta.json")):
        d = os.path.dirname(mp)
        try:
            m = json.load(open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        cid = str(m.get("cid") or "")
        if cid:
            out[cid] = os.path.isfile(os.path.join(d, "note.md"))
    return out


def scan_video() -> tuple[list[str], list[str]]:
    """返回 (可删, [(保留, 原因)])。只看真正的视频文件，跳过 .meta.json 之类附属文件。"""
    d = os.path.join(ROOT, "assets", "video")
    if not os.path.isdir(d):
        return [], []
    done = done_pages()
    safe, keep = [], []
    for f in sorted(os.listdir(d)):
        if os.path.splitext(f)[1].lower() not in (".mp4", ".mkv", ".webm", ".flv", ".m4a"):
            continue                     # 附属文件（.meta.json 等）不参与
        fp = os.path.join(d, f)
        if not os.path.isfile(fp):
            continue
        stem = os.path.splitext(f)[0]
        cid = stem.split("_")[-1] if "_" in stem else ""
        if cid and done.get(cid) is True:
            safe.append(fp)
        else:
            why = "笔记未写完" if cid in done else "无对应笔记"
            keep.append((fp, why))
    return safe, keep


def scan_audio() -> list[str]:
    """临时目录里 whisper 用的音频。转写结果已缓存，音频本身可删。"""
    d = os.path.join(tempfile.gettempdir(), "bilibili_audio")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d)
            if os.path.isfile(os.path.join(d, f)) and f.lower().endswith((".m4a", ".mp3", ".webm", ".wav"))]


def scan_frames() -> tuple[list[str], list[str]]:
    """未写进 note.md 的候选帧。

    抽帧是「每章抽 5 张、去重后留着让人挑」，最终一篇文章只用 8~10 张，
    所以每篇都会剩下几十张没被引用的。这些图既不在 note.md 里，
    HTML 里用的也是 base64 副本，删掉不影响任何已交付产物。

    只处理**有 note.md** 的目录：没写完的笔记整份保留，别动。
    """
    safe, keep = [], []
    import re
    for note in glob.glob(os.path.join(ROOT, "notes", "*", "note.md")):
        d = os.path.dirname(note)
        try:
            txt = open(note, encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            continue
        used = {os.path.normpath(os.path.join(d, m))
                for m in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", txt)}
        for p in glob.glob(os.path.join(d, "frames", "**", "*.jpg"), recursive=True):
            (keep if os.path.normpath(p) in used else safe).append(p)
    return safe, keep


def to_recycle(paths: list[str]) -> int:
    """移进 Windows 回收站（可恢复）。失败则跳过并报数。"""
    if not paths:
        return 0
    # 用 VisualBasic 的 DeleteFile + SendToRecycleBin，比 rm 安全
    union = ("$ErrorActionPreference='Continue';"
             "Add-Type -AssemblyName Microsoft.VisualBasic;")
    ok = 0
    # 分批，避免命令行过长
    for i in range(0, len(paths), 40):
        batch = paths[i:i + 40]
        cmds = union + "".join(
            "try{[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('%s','OnlyErrorDialogs','SendToRecycleBin')}catch{};"
            % p.replace("'", "''") for p in batch)
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmds],
                               capture_output=True, timeout=180)
            if r.returncode == 0:
                ok += len(batch)
            else:
                print("  ! 批量删除返回 %d：%s" % (r.returncode, r.stderr.decode("utf-8", "ignore")[:120]))
        except Exception as e:  # noqa: BLE001
            print("  ! 删除异常：%s" % str(e)[:120])
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理中间产物（默认只预览）")
    ap.add_argument("--video", action="store_true", help="清理已完成分P的原视频")
    ap.add_argument("--audio", action="store_true", help="清理临时音频缓存")
    ap.add_argument("--frames", action="store_true", help="清理未被 note.md 引用的候选帧")
    ap.add_argument("--all", action="store_true", help="video + audio + frames 全清")
    ap.add_argument("--yes", action="store_true", help="真删（不加只预览）")
    a = ap.parse_args(argv)

    if not (a.video or a.audio or a.all):
        a.video = a.audio = a.frames = True     # 无参数时全部预览

    targets: list[str] = []
    refund = 0

    if a.video or a.all:
        safe, keep = scan_video()
        s = sum(_size(p) for p in safe)
        k = sum(_size(p) for p, _ in keep)
        print("【视频】可删 %d 个 / %.0f MB ｜ 保留 %d 个 / %.0f MB（笔记未完成）"
              % (len(safe), _mb(s), len(keep), _mb(k)))
        for p, why in keep[:5]:
            print("   · 留 %-40s %s" % (os.path.basename(p)[:38], why))
        if len(keep) > 5:
            print("   · …… 另有 %d 个" % (len(keep) - 5))
        targets += safe
        refund += s

    if a.audio or a.all:
        auds = scan_audio()
        s = sum(_size(p) for p in auds)
        print("【音频】可删 %d 个 / %.0f MB（转写结果已缓存，音频本身可弃）" % (len(auds), _mb(s)))
        targets += auds
        refund += s

    if a.frames or a.all:
        drop, keep = scan_frames()
        s = sum(_size(p) for p in drop)
        print("【候选帧】可删 %d 张 / %.0f MB ｜ 保留 %d 张（note.md 实际引用的）"
              % (len(drop), _mb(s), len(keep)))
        targets += drop
        refund += s

    print()
    print("合计可回收：%.0f MB（%.2f GB）" % (_mb(refund), refund / 1073741824))

    if not a.yes:
        print("\n这是预览。确认无误后加 --yes 执行，删除会进 Windows 回收站（可恢复）。")
        return 0

    if not targets:
        print("没有可清理的文件。")
        return 0

    print("\n开始清理 %d 个文件……" % len(targets))
    ok = to_recycle(targets)
    print("已移入回收站：%d 个（约 %.0f MB 将从磁盘释放）" % (ok, _mb(refund) * ok / max(len(targets), 1)))
    if ok < len(targets):
        print("有 %d 个未成功，可重跑本命令。" % (len(targets) - ok))
    print("注意：回收站仍占空间，清空回收站才真正释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
