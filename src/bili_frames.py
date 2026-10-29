"""只做「拿画面」：B站 DASH 视频流 -> 指定时间点精确抽帧 -> 去重 -> 联系表。

设计来源与改造
--------------
思路借自 VideoDevour（`backend/algorithm/{media_profile,video_handler,image_processor}.py`）：

* 按章节在时间轴内 **均匀采样固定帧数**（默认 10 帧/章），而不是 1fps 密集抽帧
  —— 它的注释写得很直白：密集抽帧后 VLM 要在海量近似帧里挑一张，又慢又没区分度。
* 采样点取每段的 **中点**，避开章节切分处的转场/黑帧。
* 抽完做相似度去重，避免同一张 PPT 反复出现。

但实现换成更轻的路径（它那套在我们这台机器上跑不起来，也没必要）：

* 它先把整片转成 720p/10fps 再抽帧（`prepare_video`），1 小时视频要几分钟纯转码；
  这里改用 PyAV 直接 seek 到目标时间点解码单帧，不落中间视频。
* 它依赖 ffmpeg CLI + opencv(headless) + scikit-image(SSIM)；这里只依赖 av + PIL，
  去重改成 dHash（64 位感知哈希，汉明距离阈值判重），对「同一张幻灯片」同样有效。
* 不下载音轨：图文笔记的画面与文本来自两条独立链路（文本用字幕），
  视频流只要视频轨，体积约为音视频合体的绝对值下限。

对外接口
--------
    pick_stream() / download_video()   # 拿画面源
    probe_duration()                   # 时长
    grab_frames()                      # 精确抽帧
    dedupe_frames()                    # dHash 去重
    chapter_slots() / even_chapters()  # 采样点编排
    contact_sheet()                    # 联系表（给我自己快速看候选帧）
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

log = logging.getLogger("bili_frames")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAME_ROOT = os.path.join(ROOT, "assets", "frames")
VIDEO_ROOT = os.path.join(ROOT, "assets", "video")

# 视频轨偏好：qn 越小越清晰。64=720P 32=480P 16=360P（未登录通常只给到 32）
QN_NAME = {127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
           112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P", 16: "360P"}


@dataclass
class Frame:
    ts: float          # 视频内时间戳（秒）
    index: int         # 该章节内候选序号
    path: str          # 本地 jpg 路径
    width: int = 0
    height: int = 0
    hash64: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 视频流
# ---------------------------------------------------------------------------

def pick_stream(videos: list[dict], max_qn: int = 64) -> dict:
    """在 DASH video 轨里挑一条：先卡清晰度上限，再取码率最高的一条。

    B站同一清晰度会给 avc1(兼容) 与 hev1(更省带宽) 两条，两者都能解；
    这里不刻意区分编码，按 (清晰度, 码率) 排序取最优即可。
    """
    usable = [v for v in videos if int(v.get("id") or 0) <= max_qn]
    if not usable:
        usable = videos
    return max(usable, key=lambda v: (int(v.get("id") or 0), int(v.get("bandwidth") or 0)))


def download_video(bvid: str, cid: int, max_qn: int = 64, tag: str = "") -> dict:
    """下载**仅视频轨**到 assets/video/，返回路径与元信息。

    复用字幕客户端同一条网络链路（Cookie / 代理 / 直连兜底 / WBI 签名），
    所以代理挂掉时的行为与字幕请求一致，不会出现「字幕能取、视频取不到」。
    """
    from src.bilibili_client import API_PLAYURL, API_PLAYURL_WBI, _sub_client

    client = _sub_client()
    params = {"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1}
    data = client._request(API_PLAYURL, params)
    if data.get("code") != 0:
        log.debug("playurl 返回 %s，改用 WBI 签名接口", data.get("code"))
        data = client._request(API_PLAYURL_WBI, params, signed=True)
    if data.get("code") != 0:
        raise ValueError(f"获取视频流失败: {data.get('code')} {data.get('message')}")

    dash = (data.get("data") or {}).get("dash") or {}
    videos = dash.get("video") or []
    if not videos:
        raise ValueError("该分P没有可下载的视频流（可能是充电专属/互动视频）")
    best = pick_stream(videos, max_qn)

    os.makedirs(VIDEO_ROOT, exist_ok=True)
    suffix = f"_{tag}" if tag else ""
    dest = os.path.join(VIDEO_ROOT, f"{bvid}_{cid}{suffix}.mp4")

    qn = int(best.get("id") or 0)
    meta = {"bvid": bvid, "cid": cid, "qn": qn, "qn_name": QN_NAME.get(qn, str(qn)),
            "width": best.get("width"), "height": best.get("height"),
            "codecs": best.get("codecs"), "bandwidth": best.get("bandwidth"), "path": dest}

    # 缓存判断不能只看"文件在不在"：同一分P 换清晰度时（480P -> 1080P）文件名不变，
    # 只按存在性判断会把旧的低清文件当成命中，抽出来的图永远糊。用旁路 meta 记清晰度。
    side = dest + ".meta.json"
    cached_qn = 0
    if os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as f:
                cached_qn = int(json.load(f).get("qn") or 0)
        except Exception:  # noqa: BLE001
            cached_qn = 0

    if os.path.exists(dest) and os.path.getsize(dest) > 1024 * 1024 and cached_qn >= qn:
        meta.update(bytes=os.path.getsize(dest), reused=True, qn=cached_qn)
        meta["qn_name"] = QN_NAME.get(cached_qn, str(cached_qn))
        log.info("视频已存在，跳过下载: %s（%s，%.1f MB）", dest,
                 meta["qn_name"], meta["bytes"] / 1048576)
        return meta
    if os.path.exists(dest) and cached_qn and cached_qn < qn:
        log.info("缓存清晰度 %s 低于可用 %s，重新下载", QN_NAME.get(cached_qn, cached_qn),
                 QN_NAME.get(qn, qn))

    urls = [u for u in ([best.get("baseUrl")] + list(best.get("backupUrl") or [])) if u]
    log.info("下载视频流 %s %s（%d kbps，%d 个备用源）",
             QN_NAME.get(qn, qn), f"{best.get('width')}x{best.get('height')}",
             (best.get("bandwidth") or 0) // 1000, len(urls) - 1)

    last: Optional[Exception] = None
    for i, url in enumerate(urls):
        tmp = dest + ".part"
        t0 = time.time()
        try:
            with client.s.get(url, stream=True, timeout=120,
                              headers={"Referer": "https://www.bilibili.com/"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 18):
                        f.write(chunk)
                        done += len(chunk)
                        if total and done % (8 << 20) < (1 << 18):
                            log.info("  %5.1f%% / %.0f MB  %.1f MB/s", done * 100 / total,
                                     total / 1048576, done / 1048576 / max(1e-6, time.time() - t0))
            os.replace(tmp, dest)
            meta["bytes"] = os.path.getsize(dest)
            meta["seconds"] = round(time.time() - t0, 1)
            meta["reused"] = False
            try:
                with open(side, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
            log.info("视频下载完成: %.1f MB，用时 %.1fs", meta["bytes"] / 1048576, meta["seconds"])
            return meta
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("视频源 %d/%d 失败: %s", i + 1, len(urls), str(e)[:120])
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    raise RuntimeError(f"所有视频源均下载失败: {last}")


def probe_duration(video_path: str) -> float:
    import av

    with av.open(video_path) as c:
        if c.duration:
            return c.duration / 1_000_000
        vs = c.streams.video[0]
        return float(vs.duration * vs.time_base) if vs.duration else 0.0


# ---------------------------------------------------------------------------
# 抽帧
# ---------------------------------------------------------------------------

def grab_frames(video_path: str, timestamps: Iterable[float], out_dir: str,
                max_width: int = 1280, quality: int = 88,
                prefix: str = "") -> list[Frame]:
    """在指定时间点各取一帧，落盘 jpg。

    实现要点：顺序 seek（时间戳先排序）比乱序 seek 快得多；`seek(..., backward=True)`
    会把容器定位到目标点之前最近的关键帧，再解码到目标时刻，因此能取到任意时刻的准确画面。
    """
    import av
    from PIL import Image  # noqa: F401  仅用于类型与 to_image 的依赖确认

    os.makedirs(out_dir, exist_ok=True)
    ts_sorted = sorted({round(float(t), 2) for t in timestamps})
    frames: list[Frame] = []
    t_start = time.time()

    with av.open(video_path) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        try:
            c.streams.video[0].codec_context.skip_frame = "DEFAULT"
        except Exception:  # noqa: BLE001
            pass

        for i, ts in enumerate(ts_sorted, 1):
            name = f"{prefix}{ts:09.2f}.jpg"
            path = os.path.join(out_dir, name)
            if os.path.exists(path) and os.path.getsize(path) > 8 * 1024:
                with Image.open(path) as im:
                    frames.append(Frame(ts=ts, index=i, path=path, width=im.width, height=im.height))
                continue

            c.seek(int(ts * av.time_base), backward=True)
            img = None
            for frame in c.decode(vs):
                ft = frame.time
                if ft is None:
                    continue
                if ft + 1e-3 >= ts:
                    img = frame.to_image()
                    break
            if img is None:
                log.warning("时间点 %.2fs 未解出画面（可能超出流末尾）", ts)
                continue

            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, max(2, round(img.height * ratio))), 
                                 resample=getattr(Image, "LANCZOS", Image.BICUBIC))
            img.convert("RGB").save(path, "JPEG", quality=quality, optimize=True)
            frames.append(Frame(ts=ts, index=i, path=path, width=img.width, height=img.height))

    cost = time.time() - t_start
    log.info("抽帧完成：%d/%d 张，用时 %.1fs（%.2fs/张）",
             len(frames), len(ts_sorted), cost, cost / max(1, len(ts_sorted)))
    return frames


def dhash(img, size: int = 8) -> int:
    """64 位差值哈希：比较相邻像素明暗，对压缩/缩放不敏感。

    灰度图的 `tobytes()` 就是逐像素字节序列，比 getdata() 快也不会踩 Pillow 的弃用告警。
    """
    from PIL import Image

    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = g.tobytes()
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe_frames(frames: list[Frame], threshold: int = 6) -> list[Frame]:
    """按画面相似度去重，保留每组的第一张（时间最早的那张）。

    阈值 6/64 是经验值：同一张幻灯片（含鼠标移动、光标闪烁）通常 ≤4，
    翻页/切镜通常 ≥12。阈值调高会误删含轻微变化的同页（比如代码多敲了一行），
    调低则去不干净——宁可留一张几乎一样的，也别把真的换页删了。
    """
    from PIL import Image

    kept: list[Frame] = []
    for f in frames:
        with Image.open(f.path) as im:
            f.hash64 = dhash(im)
        if all(_hamming(f.hash64, k.hash64) > threshold for k in kept):
            kept.append(f)
        else:
            dup = next(k for k in kept if _hamming(f.hash64, k.hash64) <= threshold)
            log.info("去重丢弃 %.2fs（与 %.2fs 画面近似）", f.ts, dup.ts)
    log.info("去重：%d -> %d 张", len(frames), len(kept))
    return kept


def contact_sheet(items: list[tuple], out_path: str, cols: int = 4,
                  thumb_w: int = 340) -> str:
    """把候选帧拼成一张联系表：每格左上角标编号、顶部标所属章节、底部标时间戳。

    为什么要这一步：逐张看 40 多张图很费眼，一张联系表就能判断
    「哪几格是有效画面（PPT/代码）、哪几格是过渡或人脸」，再定点放大确认。

    Args:
        items: [(label, title, ts, path), ...]；也接受 (label, ts, path) 三元组
    """
    from PIL import Image, ImageDraw

    if not items:
        raise ValueError("没有可拼的帧")
    norm = []
    for it in items:
        if len(it) == 3:
            label, ts, path = it
            norm.append((label, "", ts, path))
        else:
            norm.append(tuple(it[:4]))
    cols = max(1, min(cols, len(norm)))
    rows = (len(norm) + cols - 1) // cols

    with Image.open(norm[0][3]) as im0:
        ratio = thumb_w / im0.width
        thumb_h = max(2, round(im0.height * ratio))

    pad, label_h, title_h = 6, 18, 16
    cell_h = thumb_h + label_h + title_h
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (cell_h + pad) + pad),
                      (245, 246, 248))
    draw = ImageDraw.Draw(sheet)

    for n, (label, title, ts, path) in enumerate(norm):
        r, c = divmod(n, cols)
        x = pad + c * (thumb_w + pad)
        y = pad + r * (cell_h + pad)
        with Image.open(path) as im:
            sheet.paste(im.convert("RGB").resize((thumb_w, thumb_h), Image.LANCZOS), (x, y))
        mm, ss = divmod(int(ts), 60)
        draw.text((x + 3, y + thumb_h + 2), f"{label}  {mm:02d}:{ss:02d}", fill=(30, 34, 42))
        if title:
            draw.text((x + 3, y + thumb_h + 2 + label_h), title[:26], fill=(120, 128, 140))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.save(out_path, "JPEG", quality=86, optimize=True)
    log.info("联系表已生成: %s（%d 格）", out_path, len(norm))
    return out_path


# ---------------------------------------------------------------------------
# 采样点编排
# ---------------------------------------------------------------------------

def chapter_slots(start: float, end: float, count: int, guard: float = 2.0) -> list[float]:
    """章节内均匀取 count 个采样点，用每段中点，并避开章节首尾的转场区。

    中点采样（而不是等距取左端点）是为了让候选帧分布在整章，
    而不是全都堆在章节开头那几秒的过渡画面上。
    """
    length = max(0.0, end - start)
    if length <= 0.5:
        return [start]
    head, tail = start + min(guard, length * 0.1), end - min(guard, length * 0.1)
    span = max(0.2, tail - head)
    step = span / count
    pts = [head + step * (i + 0.5) for i in range(count)]
    return [round(min(max(p, start), end), 2) for p in pts]


def even_chapters(duration: float, n: int) -> list[dict]:
    """无章节信息时按总时长均分（兜底，仅用于快速验证抽帧链路）。"""
    step = duration / n
    return [{"title": f"第{i + 1}段", "start": round(i * step, 2),
             "end": round((i + 1) * step if i + 1 < n else duration, 2)} for i in range(n)]


def load_chapters(path: str) -> list[dict]:
    """读取手工/AI 给定的章节 JSON: [{"title":..., "start":12.5, "end":88.0}]"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    chapters = raw.get("chapters") if isinstance(raw, dict) else raw
    out = []
    for c in chapters:
        out.append({"title": str(c["title"]).strip(),
                    "start": float(c["start"]), "end": float(c["end"])})
    return out


def _parse_time(v) -> float:
    """支持 12.5 / "12.5" / "03:20" / "1:02:03" 三种写法。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if ":" not in s:
        return float(s)
    parts = [float(p) for p in s.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m src.bili_frames",
        description="B站视频取画面：DASH 视频轨 -> 定点抽帧 -> 去重 -> 联系表（不调 API）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="只下载视频轨（仅视频，不含音轨）")
    d.add_argument("bvid")
    d.add_argument("--page", type=int, default=1)
    d.add_argument("--cid", type=int, default=0, help="已知 cid 时可直接给，省一次 view 请求")
    d.add_argument("--quality", type=int, default=64, help="64=720P 32=480P（默认 64）")

    g = sub.add_parser("grab", help="从本地视频定点抽帧")
    g.add_argument("video", help="本地视频路径，或 BV 号（自动先下载）")
    g.add_argument("--page", type=int, default=1, help="当第一个参数是 BV 号时生效")
    g.add_argument("--times", default="", help="时间点，逗号分隔：12.5,60,03:20")
    g.add_argument("--chapters", default="", help="章节 JSON（给 start/end，按章均匀取帧）")
    g.add_argument("--frames", type=int, default=6, help="每章候选帧数（默认 6）")
    g.add_argument("--even", type=int, default=0, help="均分 N 段（无章节文件时的兜底）")
    g.add_argument("--out", default="", help="输出目录（默认 assets/frames/<视频名>）")
    g.add_argument("--cols", type=int, default=5, help="联系表每行格数")
    g.add_argument("--max-width", type=int, default=1280)
    g.add_argument("--no-dedupe", action="store_true", help="跳过去重")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)

    if args.cmd == "download":
        cid = args.cid
        if not cid:
            from src.bilibili_client import _sub_client

            pages = _sub_client().get_view(args.bvid).get("pages") or []
            page = next((p for p in pages if p.get("page") == args.page), None)
            if not page:
                raise SystemExit(f"该视频没有第 {args.page} P")
            cid = int(page["cid"])
        m = download_video(args.bvid, cid, max_qn=args.quality)
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return 0

    # grab
    video = args.video
    if video.upper().startswith("BV") or "bilibili.com" in video:
        import re as _re

        bvid = _re.search(r"(BV[0-9A-Za-z]{10})", video).group(1)
        from src.bilibili_client import _sub_client

        pages = _sub_client().get_view(bvid).get("pages") or []
        page = next((p for p in pages if p.get("page") == args.page), None)
        if not page:
            raise SystemExit(f"该视频没有第 {args.page} P")
        video = download_video(bvid, int(page["cid"]))["path"]

    if not os.path.exists(video):
        raise SystemExit(f"视频不存在: {video}")

    out_dir = args.out or os.path.join(FRAME_ROOT, os.path.splitext(os.path.basename(video))[0])
    duration = probe_duration(video)
    log.info("视频：%s（%d:%02d）", os.path.basename(video), int(duration) // 60, int(duration) % 60)

    plan: list[tuple[str, float, float]] = []
    if args.chapters:
        chapters = load_chapters(args.chapters)
        for i, c in enumerate(chapters, 1):
            plan.append((f"c{i:02d} {c['title']}", c["start"], min(c["end"], duration)))
    elif args.even:
        for i, c in enumerate(even_chapters(duration, args.even), 1):
            plan.append((f"c{i:02d} {c['title']}", c["start"], c["end"]))
    elif args.times:
        for t in args.times.split(","):
            ts = _parse_time(t)
            plan.append((f"t{int(ts):06d}", ts, ts))
    else:
        raise SystemExit("需要 --times / --chapters / --even 之一来指定抽帧位置")

    manifest: dict[str, dict] = {}
    items: list[tuple] = []
    for i, (label, start, end) in enumerate(plan, 1):
        slots = [start] if end <= start + 0.5 else chapter_slots(start, end, args.frames)
        sub_dir = os.path.join(out_dir, label.split()[0])
        raw = grab_frames(video, slots, sub_dir, max_width=args.max_width)
        kept = raw if args.no_dedupe else dedupe_frames(raw)
        manifest[label.split()[0]] = {
            "label": label, "start": start, "end": end,
            "candidates": len(raw), "frames": [f.as_dict() for f in kept]}
        for n, f in enumerate(kept, 1):
            items.append((f"{label.split()[0][-2:]}-{n}", label, f.ts, f.path))
        log.info("  %s：%d 候选 -> %d 保留", label[:34], len(raw), len(kept))

    with open(os.path.join(out_dir, "frames.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    sheets = []
    if items:
        import math

        per_max = max(1, args.cols * 4)
        n_sheets = max(1, math.ceil(len(items) / per_max))
        per = math.ceil(len(items) / n_sheets)   # 均衡分配，避免最后一张只剩一两格
        for gi in range(0, len(items), per):
            sheets.append(contact_sheet(items[gi:gi + per],
                                        os.path.join(out_dir, "contacts", f"sheet_{gi // per + 1}.jpg"),
                                        cols=args.cols))
    print(f"完成：{out_dir}")
    print(f"  保留 {len(items)} 张 ｜ 联系表 {len(sheets)} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
