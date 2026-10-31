"""从本地文件生成图文素材 —— 完全不依赖任何平台的 API。

用在两种情况：
1. 平台接口不通（比如 YouTube 的 TLS 被掐），但用户手上有视频文件；
2. 素材本来就在本地（录屏、网课下载件、别人传过来的 mp4）。

它做的事和 `src.bili_notes` 完全一样：字幕（或转写）→ 自动分章 → 按章抽帧 →
联系表 → transcript.md / frames.json / meta.json。**产物结构一致**，
所以后面的写笔记、`md_to_html`、`split_notes`、传知识库全都不用改。

用法：
    python tools/notes_from_local.py --video in.mp4 --name unsloth-finetune
    python tools/notes_from_local.py --video in.mp4 --subtitle in.srt --name xxx
    python tools/notes_from_local.py --video in.mp4 --audio-only   # 手上有音轨就省解码

字幕给了就用字幕（快、准），没给就本地 whisper 转写。srt / vtt 都吃。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.bili_frames import (chapter_slots, contact_sheet, dedupe_frames,  # noqa: E402
                             grab_frames, probe_duration)
from src.bili_notes import (auto_chapters, load_chapters, transcribe_audio,  # noqa: E402
                            write_transcript)
from src.youtube_client import _parse_vtt  # noqa: E402  同一套 srt/vtt 解析

log = logging.getLogger("local_notes")


def read_subtitle(path: str) -> list[dict]:
    """读 srt / vtt，返回 [{from,to,text}]。"""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    segs = _parse_vtt(text)
    if not segs:
        raise ValueError(
            "字幕解析为空：%s\n"
            "支持 SRT / WebVTT；如果是别的格式（ass、json3），先转成 srt 再喂。" % path
        )
    return segs


def _mmss(sec: float) -> str:
    sec = int(max(0, sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


def build(args) -> dict:
    video = os.path.abspath(args.video)
    if not os.path.isfile(video):
        raise SystemExit("视频不存在：%s" % video)

    out_dir = os.path.join(ROOT, "notes", args.name)
    os.makedirs(out_dir, exist_ok=True)

    duration = probe_duration(video)
    log.info("视频：%s（%.1f MB，时长 %s）",
             os.path.basename(video), os.path.getsize(video) / 1048576, _mmss(duration))

    # 1) 文本：有字幕用字幕，没有才转写
    if args.subtitle:
        segs = read_subtitle(args.subtitle)
        tr = {"source": "字幕文件（%s）" % os.path.splitext(args.subtitle)[1].lstrip("."),
              "segments": segs, "chars": sum(len(s["text"]) for s in segs), "cost": 0}
        log.info("字幕：%d 段 / %d 字", len(segs), tr["chars"])
    elif args.audio_only and args.audio:
        tr = transcribe_audio(os.path.abspath(args.audio), args.model)
        log.info("转写：%d 段 / %d 字", len(tr["segments"]), tr["chars"])
    else:
        # 直接喂视频给 whisper：faster-whisper 内部会用 PyAV 解码，不必先抽音轨
        tr = transcribe_audio(video, args.model)
        log.info("转写：%d 段 / %d 字（来源 %s）", len(tr["segments"]), tr["chars"], tr["source"])

    # 2) 分章
    if args.chapters:
        chapters = load_chapters(args.chapters)
        log.info("手工章节：%d 章", len(chapters))
    else:
        chapters = auto_chapters(tr["segments"], duration, target=args.auto_n)
        log.info("自动分章：%d 章", len(chapters))
    for i, c in enumerate(chapters, 1):
        log.info("  %02d %-42s %s–%s", i, c["title"][:40], _mmss(c["start"]), _mmss(c["end"]))

    title = args.title or os.path.splitext(os.path.basename(video))[0]
    meta = {
        "bvid": "", "cid": 0, "page": 1, "part": title, "title": title,
        "url": args.source_url or video,
        "duration": duration, "text_source": tr["source"],
        "chars": tr["chars"], "text_cost": tr["cost"],
        "segments": len(tr["segments"]), "chapters": len(chapters),
        "local_video": video,
    }

    # 3) 底稿
    tpath = write_transcript(chapters, tr["segments"], duration, meta,
                             os.path.join(out_dir, "transcript.md"))
    log.info("底稿：%s", tpath)

    # 4) 按章抽帧
    frames_dir = os.path.join(out_dir, "frames")
    built: dict[str, dict] = {}
    for i, c in enumerate(chapters, 1):
        end = min(c["end"], duration)
        if end - c["start"] < 2:
            continue
        slots = chapter_slots(c["start"], end, args.frames)
        raw = grab_frames(video, slots, os.path.join(frames_dir, "c%02d" % i))
        kept = dedupe_frames(raw)
        built["c%02d" % i] = {
            "index": i, "title": c["title"], "start": c["start"], "end": end,
            "candidates": len(raw), "frames": [f.as_dict() for f in kept],
        }
        log.info("  [%d/%d] %s：%d 候选 -> %d 保留",
                 i, len(chapters), c["title"][:28], len(raw), len(kept))
        with open(os.path.join(out_dir, "frames.json"), "w", encoding="utf-8") as f:
            json.dump(built, f, ensure_ascii=False, indent=2)   # 每章刷盘，中断可续跑

    # 5) 联系表
    items = []
    for key in sorted(built):
        ch = built[key]
        for n, fr in enumerate(ch["frames"], 1):
            items.append(("%s-%d" % (key[-2:], n), ch["title"], fr["ts"], fr["path"]))
    sheets = []
    if items:
        per_max = max(1, args.cols * 4)
        n_sheets = max(1, math.ceil(len(items) / per_max))
        per = math.ceil(len(items) / n_sheets)
        for gi in range(0, len(items), per):
            sheets.append(contact_sheet(items[gi:gi + per],
                                        os.path.join(out_dir, "contacts", "sheet_%d.jpg" % (gi // per + 1)),
                                        cols=args.cols))

    meta["frames"] = sum(len(v["frames"]) for v in built.values())
    meta["contacts"] = sheets
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({**meta, "chapter_list": chapters}, f, ensure_ascii=False, indent=2)

    print("\n完成：%s" % out_dir)
    print("  文本 %s %d 段 / %d 字" % (tr["source"], len(tr["segments"]), tr["chars"]))
    print("  章节 %d ｜ 候选帧 %d ｜ 联系表 %d" % (len(chapters), meta["frames"], len(sheets)))
    print("  下一步：看 contacts/ 挑帧 -> 写 note.md（只许从 transcript.md 取事实）")
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从本地视频 + 字幕生成图文素材")
    ap.add_argument("--video", required=True, help="本地视频文件（mp4/mkv/webm 均可）")
    ap.add_argument("--subtitle", default="", help="字幕文件（srt/vtt），有就用，没有则转写")
    ap.add_argument("--audio", default="", help="单独的音频文件（配合 --audio-only）")
    ap.add_argument("--audio-only", action="store_true", help="只转写 --audio 指定的音频")
    ap.add_argument("--name", required=True, help="输出目录名（notes/<name>/）")
    ap.add_argument("--title", default="", help="标题，默认取文件名")
    ap.add_argument("--source-url", default="", help="来源链接，写进底稿便于溯源")
    ap.add_argument("--chapters", default="", help="手工章节文件（JSON）")
    ap.add_argument("--frames", type=int, default=5, help="每章抽帧数（默认 5）")
    ap.add_argument("--cols", type=int, default=5, help="联系表列数（默认 5）")
    ap.add_argument("--auto-n", type=int, default=0, help="自动分章数，0=自动决定")
    ap.add_argument("--model", default="small", help="whisper 模型（默认 small）")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
