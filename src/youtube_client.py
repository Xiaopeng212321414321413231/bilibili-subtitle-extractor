"""YouTube 取素材：纯 curl_cffi 实现，不依赖 yt-dlp。

## 为什么能自己写

YouTube 的 InnerTube player API 只要「扮对客户端」就给全量数据。实测
visionos 这个 client 不要求 po_token、返回的直链也不带 `n` 参数（无需反混淆），
所以整套流程就三步：

    1. GET /watch 页面      → 拿 INNERTUBE_API_KEY、VISITOR_DATA
    2. POST /youtubei/v1/player（visionos 上下文） → streamingData + captions
    3. 直链和字幕 URL 直接下

关键在客户端上下文必须**完整**。少一个字段就从 OK 掉到 UNPLAYABLE / LOGIN_REQUIRED：

    timeZone: UTC + utcOffsetMinutes: 0   ← 最容易漏，漏了必失败
    Origin: https://www.youtube.com       ← header 里也要有
    X-Goog-Visitor-Id                     ← 从 watch 页面取
    deviceMake/deviceModel/osName/osVersion/userAgent ← 五件套缺一不可

## 依赖

只要 `curl_cffi`（1MB）。它是必需的，不是可选的：
裸 requests 的 TLS 指纹（JA3）一眼被识破，握手直接被掐断
（`SSL: UNEXPECTED_EOF_WHILE_READING`，实测连试 10 次全败）。

## 与 B站侧的映射

| B站              | 这里            |
|------------------|-----------------|
| `get_view`       | `extract`       |
| `pick_stream`    | `pick_streams`  |
| `download_video` | `download`      |
| `get_subtitles`  | `get_subtitles` |
| `load_chapters`  | `chapters`      |

字幕 segment 统一成 `{"from": float, "to": float, "text": str}`，与 bili_subtitle 一致，
`auto_chapters` / `write_transcript` 一个字都不用改。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# visionos 客户端的完整身份。改这里等同于改「我是什么设备」，
# 字段取自 yt-dlp 的实测配置（clientVersion 必须是 1.02，写成 1.1 会 404）。
_CLIENT = {
    "clientName": "VISIONOS",
    "clientVersion": "1.02",
    "deviceMake": "Apple",
    "deviceModel": "RealityDevice17,1",
    "userAgent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
    "osName": "visionOS",
    "osVersion": "26.5.23O471",
}
_CLIENT_NAME_ID = "101"          # X-YouTube-Client-Name
_PAGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LANG_PRIORITY = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US", "en-GB")


class YouTubeUnreachable(RuntimeError):
    """请求失败。调用方应降级到本地文件模式。"""


def _session():
    try:
        from curl_cffi import requests as creq
    except ImportError as e:  # pragma: no cover
        raise YouTubeUnreachable(
            "未安装 curl_cffi，无法访问 YouTube。装：pip install curl_cffi\n"
            "（它是必需的：裸 requests 的 TLS 指纹会被直接掐断）\n"
            "或改用本地文件模式：--video-file <mp4> [--subtitle-file <srt>]"
        ) from e
    return creq.Session(impersonate="chrome")


def video_id(url: str) -> str:
    """从各种形态的 YouTube 链接里抠出 11 位 ID。"""
    if re.fullmatch(r"[\w-]{11}", url or ""):
        return url
    m = re.search(r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([\w-]{11})", url or "")
    if not m:
        raise ValueError("认不出 YouTube 视频 ID：%s" % url)
    return m.group(1)


def _parse_mime(mime: str) -> tuple[str, str]:
    """`video/mp4; codecs="avc1.640028"` -> ('video/mp4', 'avc1.640028')。"""
    base, _, rest = (mime or "").partition(";")
    m = re.search(r'codecs="([^"]+)"', rest)
    return base.strip(), (m.group(1) if m else "")


def _player(vid: str, session=None, retries: int = 2) -> dict:
    """调 InnerTube player API，返回原始 JSON。

    先 GET watch 页拿 API key 与 visitorData —— 这两个每会话都不同，不能硬编码。
    """
    s = session or _session()
    last: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            page = s.get("https://www.youtube.com/watch?v=" + vid, timeout=40,
                         headers={"Accept-Language": "en-US,en;q=0.9", "User-Agent": _PAGE_UA})
            if page.status_code != 200:
                raise IOError("watch 页 HTTP %s" % page.status_code)

            m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page.text)
            if not m:
                raise IOError("页面里没找到 INNERTUBE_API_KEY（可能是同意页/验证页）")
            key = m.group(1)
            mv = re.search(r'"VISITOR_DATA":"([^"]+)"', page.text)
            visitor = mv.group(1) if mv else ""

            ctx = {"client": {**_CLIENT, "hl": "en",
                              "timeZone": "UTC", "utcOffsetMinutes": 0}}
            body = {"context": ctx, "videoId": vid,
                    "contentCheckOk": True, "racyCheckOk": True}
            hdr = {
                "Content-Type": "application/json",
                "User-Agent": _CLIENT["userAgent"],
                "Origin": "https://www.youtube.com",
                "X-YouTube-Client-Name": _CLIENT_NAME_ID,
                "X-YouTube-Client-Version": _CLIENT["clientVersion"],
            }
            if visitor:
                hdr["X-Goog-Visitor-Id"] = visitor

            r = s.post("https://www.youtube.com/youtubei/v1/player?key=" + key,
                       headers=hdr, json=body, timeout=40)
            d = r.json()
            st = (d.get("playabilityStatus") or {}).get("status")
            if st in (None, "ERROR") or r.status_code != 200:
                reason = (d.get("playabilityStatus") or {}).get("reason") or st
                raise IOError("player API 拒绝：%s" % reason)
            return d
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("player 请求失败 %d/%d：%s", attempt, retries, str(e)[:140])
            if attempt < retries:
                time.sleep(2 * attempt)

    raise YouTubeUnreachable(
        "YouTube 取不到 player 数据（%d 次）：%s\n"
        "常见原因：网络被掐（需要 curl_cffi 的 TLS 指纹）、或该视频需登录/地区限制。\n"
        "可改用本地文件：python tools/notes_from_local.py --video <mp4> --subtitle <srt>"
        % (retries, str(last)[:200])
    )


def _normalize(d: dict, vid: str) -> dict:
    """把 InnerTube 的原始返回整理成与 yt-dlp info 同构的结构。

    下游（pick_streams / get_subtitles / chapters）只认这个结构，
    所以换数据源时它们一行都不用改。
    """
    vd = d.get("videoDetails") or {}
    sd = d.get("streamingData") or {}

    formats = []
    for f in (sd.get("adaptiveFormats") or []) + (sd.get("formats") or []):
        url = f.get("url") or ""
        mime = f.get("mimeType") or ""
        base, codec = _parse_mime(mime)
        is_video, is_audio = base.startswith("video"), base.startswith("audio")
        if not url:
            continue          # 走 signatureCipher 的（本 client 不会出现）直接跳过
        formats.append({
            "format_id": str(f.get("itag") or ""),
            "url": url,
            "mimeType": mime,
            "ext": "mp4" if "mp4" in base else ("webm" if "webm" in base else ""),
            "vcodec": codec if is_video else "none",
            "acodec": codec if is_audio else "none",
            "height": f.get("height"),
            "width": f.get("width"),
            "fps": f.get("fps"),
            "tbr": (f.get("bitrate") or 0) / 1000 or None,
            "abr": (f.get("bitrate") or 0) / 1000 if is_audio else None,
            "filesize": int(f["contentLength"]) if f.get("contentLength") else None,
            "qualityLabel": f.get("qualityLabel"),
        })

    # 字幕：InnerTube 给的 captionTracks 是平铺的，按语言分组还原成 yt-dlp 的样子
    tracks = (((d.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
              .get("captionTracks") or [])
    manual: dict[str, list] = {}
    auto: dict[str, list] = {}
    for t in tracks:
        lang = t.get("languageCode") or ""
        if not lang or not t.get("baseUrl"):
            continue
        fmt = "json3" if "fmt=" not in t["baseUrl"] else "json3"
        item = {"ext": fmt, "url": t["baseUrl"] + ("&fmt=json3" if "fmt=" not in t["baseUrl"] else "")}
        (auto if t.get("kind") == "asr" else manual).setdefault(lang, []).append(item)

    chapters = []
    for c in ((d.get("chapters") or {}).get("chapters") or []):
        chapters.append({"title": c.get("title", ""),
                         "start_time": (c.get("timeRangeStartMillis") or 0) / 1000.0,
                         "end_time": 0})

    return {
        "id": vid,
        "title": vd.get("title"),
        "duration": int(vd["lengthSeconds"]) if vd.get("lengthSeconds") else None,
        "uploader": vd.get("author"),
        "channel": vd.get("channelId"),
        "view_count": int(vd["viewCount"]) if vd.get("viewCount") else None,
        "like_count": None,       # InnerTube 不直接给，要单独打 next 接口
        "upload_date": None,
        "language": None,
        "formats": formats,
        "subtitles": manual,
        "automatic_captions": auto,
        "chapters": chapters,
        "webpage_url": "https://www.youtube.com/watch?v=" + vid,
    }


def extract(url: str, retries: int = 2) -> dict:
    """取视频元信息（直链 + 字幕轨 + 章节），结构同 yt-dlp info。"""
    vid = video_id(url)
    d = _player(vid, retries=retries)
    info = _normalize(d, vid)
    logger.info("解析成功：%s（%d 个格式，%d 条字幕轨）",
                info.get("title"), len(info["formats"]),
                len(info["subtitles"]) + len(info["automatic_captions"]))
    return info


# --------------------------------------------------------------------------- #
# 选流 / 字幕 / 章节
# --------------------------------------------------------------------------- #
def pick_streams(info: dict, max_height: int = 1080,
                 prefer_vcodec: str = "av01") -> dict:
    """挑「最佳纯视频流 + 最佳纯音频流」。

    只取 DASH 分离流（合流通常封顶 720p）。选流策略按重要性排：

    1. **分辨率高的优先**
    2. **同分辨率下体积小的优先** —— 这条很关键：同一个 1080p，av01 是 381MB，
       avc1 是 965MB，差 2.5 倍。抽帧只取几帧画面，编码格式无所谓，
       PyAV 内置 dav1d 能解 AV1，没必要为「兼容性」多下 580MB。
    3. 最后才拿 prefer_vcodec 当 tie-breaker（默认 av01）。
    """
    fmts = info.get("formats") or []
    vids = [f for f in fmts
            if f.get("vcodec") not in (None, "none", "")
            and f.get("acodec") in (None, "none", "")
            and (f.get("height") or 0) <= max_height]
    auds = [f for f in fmts
            if f.get("acodec") not in (None, "none", "")
            and f.get("vcodec") in (None, "none", "")]

    def vscore(f):
        size = f.get("filesize") or 0
        return (
            f.get("height") or 0,
            -(size // 1048576) if size else 0,          # 越小越靠前
            1 if (f.get("vcodec") or "").startswith(prefer_vcodec) else 0,
        )

    return {
        "video": max(vids, key=vscore) if vids else None,
        "audio": max(auds, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0)) if auds else None,
    }


def _pick_track(tracks: dict, langs: Iterable[str]) -> tuple[str, list]:
    if not tracks:
        return "", []
    for want in langs:
        for have in tracks:
            if have == want or have.startswith(want + "-"):
                return have, tracks[have]
    first = next(iter(tracks))
    return first, tracks[first]


def _track_url(formats: list, prefer: Iterable[str] = ("json3", "srv1", "vtt")) -> str:
    by_ext = {f.get("ext"): f.get("url") for f in formats if f.get("url")}
    for ext in prefer:
        if ext in by_ext:
            return by_ext[ext]
    return next(iter(by_ext.values()), "")


def _parse_json3(data: dict) -> list[dict]:
    """json3 字幕 -> [{from, to, text}]。

    跳过两类噪音：没有 segs 的空事件（换行标记）、segs 里夹着的 "\\n"。
    """
    out = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        dur = (ev.get("dDurationMs") or 0) / 1000.0
        out.append({"from": start, "to": start + dur, "text": text})
    return out


_VTT_TS = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
)


def _parse_vtt(text: str) -> list[dict]:
    """VTT / SRT 通用解析（只差时间码分隔符，正则一并覆盖）。"""
    out = []
    for blk in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln for ln in blk.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if idx < 0:
            continue
        m = _VTT_TS.search(lines[idx])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        a = int(h1 or 0) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        b = int(h2 or 0) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        body = re.sub(r"<[^>]+>", "", " ".join(ln.strip() for ln in lines[idx + 1:])).strip()
        if body:
            out.append({"from": a, "to": b, "text": body})
    return out


def get_subtitles(info: dict, langs: Iterable[str] = LANG_PRIORITY,
                  session=None) -> dict:
    """取字幕，结构与 bilibili_client.get_subtitles 一致。

    人工字幕优先，没有再退自动字幕（ASR）。自动字幕专有名词错得明显更多，
    source 里会写明，写笔记时要标出来。
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    if manual:
        lang, fmts = _pick_track(manual, langs)
        kind, source = "manual", "YouTube 人工字幕"
    elif auto:
        lang, fmts = _pick_track(auto, langs)
        kind, source = "auto", "YouTube 自动字幕（ASR）"
    else:
        return {"available": False, "error": "该视频没有字幕轨（人工和自动都没有）"}

    url = _track_url(fmts)
    if not url:
        return {"available": False, "error": "字幕轨没有可用 URL"}

    s = session or _session()
    try:
        r = s.get(url, timeout=30, headers={"Referer": "https://www.youtube.com/",
                                            "User-Agent": _CLIENT["userAgent"]})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": "字幕下载失败：%s" % str(e)[:120]}

    if not r.content:
        # 200 但空包：这是缺 po_token 的典型表现（中文机器翻译轨最容易撞上）
        return {"available": False,
                "error": "字幕返回空包（通常是该语言轨需要 po_token，中文机器翻译轨常如此）"}

    try:
        segs = _parse_json3(r.json())
    except Exception:  # noqa: BLE001
        segs = _parse_vtt(r.text)

    if not segs:
        return {"available": False, "error": "字幕解析后为空"}

    text = "\n".join(s["text"] for s in segs)
    try:
        from .bili_subtitle import segments_to_srt
    except ImportError:
        from bili_subtitle import segments_to_srt

    logger.info("字幕获取成功（%s，%s，%d 条 / %d 字）", source, lang, len(segs), len(text))
    return {"available": True, "text": text, "source": source, "lang": lang,
            "kind": kind, "segments": segs, "srt": segments_to_srt(segs),
            "chars": len(text)}


def chapters(info: dict) -> list[dict]:
    """原生章节。UP 主自己划的，比按字幕统计硬切准得多。"""
    out = []
    for c in info.get("chapters") or []:
        try:
            out.append({"title": (c.get("title") or "").strip() or "未命名",
                        "start": float(c.get("start_time") or 0),
                        "end": float(c.get("end_time") or 0)})
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download(fmt: dict, out_path: str, session=None, retries: int = 3,
             threads: int = 8, chunk: int = 1 << 16, progress=None) -> str:
    """下载一条流，分片并发。

    **为什么要并发**：YouTube 对单条连接的限速约 150 KB/s，一个 381MB 的 1080p
    要下 45 分钟；切成 8 段各开一个连接，实测能到 2.6 MB/s，整片 2.5 分钟。
    这不是抠速度，是决定「能不能用」的差别。

    小文件（<4MB）走单连接，省掉切分开销；断点续传靠 .part + Range。

    参数：
        threads: 并发段数。8 是实测的甜点，再高容易被反爬盯上。
        progress: 可选回调，收到「已下载字节数」。
    """
    url = (fmt or {}).get("url")
    if not url:
        raise ValueError("该格式没有下载地址")

    total = int((fmt or {}).get("filesize") or 0)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # 小文件或不支持并发时走单连接
    if not total or total < 4 * 1048576 or threads <= 1:
        return _download_single(url, out_path, session, retries, chunk, progress)

    part = out_path + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have == total:
        os.replace(part, out_path)
        return out_path
    if have > total:                      # 上次的残片比预期还大，重来
        os.remove(part)
        have = 0

    # 预分配到目标大小，让各段能直接 seek 写自己的区间
    with open(part, "r+b" if have else "wb") as f:
        f.truncate(total)

    seg = total // threads
    spans = [(i * seg, (total - 1) if i == threads - 1 else (i + 1) * seg - 1)
             for i in range(threads)]

    done = {"n": 0}
    lock = threading.Lock()

    def worker(idx, start, end):
        s = session or _session()
        # 已经下过的部分跳过（断点续传）
        cur = start
        if have and cur < have:
            cur = min(have, end + 1)
        try:
            while cur <= end:
                try:
                    r = s.get(url, headers={"Referer": "https://www.youtube.com/",
                                            "User-Agent": _CLIENT["userAgent"],
                                            "Range": "bytes=%d-%d" % (cur, end)},
                              stream=True, timeout=60)
                    if r.status_code not in (200, 206):
                        raise IOError("HTTP %s" % r.status_code)
                    with open(part, "r+b") as f:
                        f.seek(cur)
                        for buf in r.iter_content(chunk_size=chunk):
                            if not buf:
                                continue
                            f.write(buf)
                            cur += len(buf)
                            with lock:
                                done["n"] += len(buf)
                                if progress:
                                    progress(done["n"])
                            if cur > end:
                                break
                    r.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("分片 %d 重试（%d/%d）：%s", idx, cur, end, str(e)[:100])
                    time.sleep(2)
        except Exception as e:  # noqa: BLE001
            logger.error("分片 %d 失败：%s", idx, str(e)[:120])

    with lock:
        done["n"] = have

    ts = [threading.Thread(target=worker, args=(i, a, b), daemon=True)
          for i, (a, b) in enumerate(spans)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]

    size = os.path.getsize(part)
    if size < total * 0.98:
        raise IOError("下载不完整：%d / %d（可重跑续传）" % (size, total))
    os.replace(part, out_path)
    dt = max(time.time() - t0, 0.01)
    logger.info("下载完成 %s（%.1f MB，%.1f MB/s，%d 线程）",
                os.path.basename(out_path), size / 1048576, size / 1048576 / dt, threads)
    return out_path


def _download_single(url: str, out_path: str, session=None, retries: int = 3,
                     chunk: int = 1 << 16, progress=None) -> str:
    """单连接下载（小文件用）。.part 断点续传，成功后原子替换。"""
    s = session or _session()
    part = out_path + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"Referer": "https://www.youtube.com/", "User-Agent": _CLIENT["userAgent"]}
    if have:
        headers["Range"] = "bytes=%d-" % have

    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = s.get(url, headers=headers, stream=True, timeout=60)
            try:
                if r.status_code == 416:
                    os.replace(part, out_path)
                    return out_path
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                with open(part, mode) as f:
                    for buf in r.iter_content(chunk_size=chunk):
                        f.write(buf)
                        if progress:
                            progress(f.tell())
            finally:
                r.close()
            os.replace(part, out_path)
            logger.info("下载完成 %s（%.1f MB）",
                        os.path.basename(out_path), os.path.getsize(out_path) / 1048576)
            return out_path
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("下载失败 %d/%d：%s", attempt, retries, str(e)[:120])
            time.sleep(min(2 ** attempt, 10))
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have:
                headers["Range"] = "bytes=%d-" % have

    raise RuntimeError("下载失败（%d 次）：%s" % (retries, last))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="探测 YouTube 视频（不写文件）")
    ap.add_argument("url")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        info = extract(a.url)
    except (YouTubeUnreachable, ValueError) as e:
        print("\n[失败]", e)
        return 2

    print()
    print("标题   :", info.get("title"))
    print("时长   :", "%d 分 %d 秒" % divmod(int(info.get("duration") or 0), 60))
    print("频道   :", info.get("uploader"))
    print("观看   :", info.get("view_count"))
    print()
    print("人工字幕:", list(info.get("subtitles") or {}) or "无")
    print("自动字幕:", list(info.get("automatic_captions") or {}) or "无")
    chs = chapters(info)
    print("原生章节:", "%d 个" % len(chs))
    for c in chs[:6]:
        print("   %6.0fs  %s" % (c["start"], c["title"]))
    v, au = pick_streams(info)["video"], pick_streams(info)["audio"]
    print()
    print("选流   :", "视频 %sp" % (v.get("height") if v else "无"),
          "| 音频 %skbps" % (round(au.get("abr") or 0) if au else "无"))
    print("格式数 :", len(info.get("formats") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
