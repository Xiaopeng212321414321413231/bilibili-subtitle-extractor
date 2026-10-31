"""YouTube 取素材：元信息 / 字幕 / 视频流，接口与 bilibili_client 对齐。

## 为什么不是「简单调 yt-dlp」

yt-dlp 真正值钱的不是 CLI，而是它内部那套跟 YouTube 斗了十几年的东西：
签名解算、`n` 参数（限速）反混淆、po_token、多 player_client 轮换、字幕格式回落。
这些自己写不现实 —— 所以这里**借它的解析**，但**自己掌控下载**：

    yt-dlp  extract_info()  -> 已解 n 的直链 + 字幕轨清单 + 章节
    我们    requests 下载    -> 断点续传、代理、重试策略跟本项目其他模块一致

## 网络现实

YouTube 在部分网络下 TLS 会被随机中断（`SSL: UNEXPECTED_EOF_WHILE_READING`），
表现为「偶尔通一次、连续失败」。这不是 bug，是链路本身不稳。所以本模块：

- 多 player_client 轮试（tv / android / ios / web_safari / mweb）
- 每次重试之间退避，指数增长
- 全部失败时抛 `YouTubeUnreachable`，由调用方决定降级到本地文件
  （`--video-file` / `--subtitle-file`），而不是傻等

## 与 B站侧的映射

| B站            | 这里                      |
|----------------|---------------------------|
| `get_view`     | `extract`                 |
| `pick_stream`  | `pick_streams`            |
| `download_video` | `download`              |
| `get_subtitles`| `get_subtitles`           |
| `load_chapters`| `chapters`（原生优先）    |

字幕 segment 统一成 `{"from": float, "to": float, "text": str}`，与 bili_subtitle 一致，
这样 `auto_chapters` / `write_transcript` 一个字都不用改。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# 依次尝试的客户端。tv/android 常常比 web 更容易过；web_safari 兜底。
DEFAULT_CLIENTS = ("tv", "android", "ios", "web_safari", "mweb")

# 字幕语言优先级：中文优先（本项目使用者的场景），再英文，最后随便挑一条
LANG_PRIORITY = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US", "en-GB")


class YouTubeUnreachable(RuntimeError):
    """所有客户端、所有重试都失败。调用方应降级到本地文件路径。"""


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
def extract(url: str, clients: Iterable[str] = DEFAULT_CLIENTS,
            retries: int = 2, timeout: int = 20) -> dict:
    """拿视频元信息（含已解 n 的直链、字幕轨、章节）。

    按 clients 顺序轮试，每个客户端重试 retries 次，失败退避后换下一个。
    全部失败抛 YouTubeUnreachable —— 别在这里吞异常，调用方要知道「是网络问题，
    不是视频问题」，才能决定降级还是放弃。

    Args:
        url: 视频 URL。
        clients: player_client 候选序列。
        retries: 每个客户端的重试次数。
        timeout: 单次 socket 超时（秒）。给太小会在慢链路上误判。

    Returns:
        yt-dlp 的 info dict（未裁剪，含 formats / subtitles / chapters）。
    """
    try:
        import yt_dlp
    except ImportError as e:  # pragma: no cover
        raise YouTubeUnreachable(
            "未安装 yt-dlp，无法解析 YouTube。装：pip install yt-dlp；"
            "或改用本地文件模式（--video-file）"
        ) from e

    errs: list[str] = []
    for ci, client in enumerate(clients):
        for attempt in range(1, retries + 1):
            opts = {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": timeout,
                "retries": 1,
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info:
                    logger.info("解析成功（client=%s，第 %d 次尝试）", client, attempt)
                    return info
            except Exception as e:  # noqa: BLE001
                msg = str(e).replace("\n", " ")[:160]
                errs.append("client=%s try%d: %s" % (client, attempt, msg))
                logger.debug("解析失败 %s", errs[-1])
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
        # 换下一个客户端前稍等，避免连续撞同一层封锁
        if ci < len(list(clients)) - 1:
            time.sleep(1.5)

    raise YouTubeUnreachable(
        "YouTube 不可达（%d 次尝试全部失败）。最近一次：%s\n"
        "多半是链路问题（TLS 被中断）。可改用本地文件：\n"
        "  --video-file <已下好的 mp4>  [--subtitle-file <srt/vtt>]"
        % (len(errs), errs[-1] if errs else "-")
    )


def pick_streams(info: dict, max_height: int = 1080,
                 prefer_vcodec: str = "avc1") -> dict:
    """从 formats 里挑「最佳纯视频流 + 最佳纯音频流」。

    对齐 bili_frames.pick_stream：只要 DASH 分离流，不要合流（合流通常是 720p 上限）。
    prefer_vcodec 默认 avc1(H.264) —— PyAV 解码最稳，av1/vp9 偶尔要额外解码器。

    Returns:
        {"video": format dict, "audio": format dict}，缺失的分项为 None。
    """
    fmts = info.get("formats") or []

    vids = [f for f in fmts
            if f.get("vcodec") not in (None, "none")
            and f.get("acodec") in (None, "none")
            and (f.get("height") or 0) <= max_height]

    def vscore(f: dict) -> tuple:
        return (
            f.get("height") or 0,
            1 if (f.get("vcodec") or "").startswith(prefer_vcodec) else 0,
            f.get("tbr") or 0,
        )

    auds = [f for f in fmts
            if f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none")]
    audio = max(auds, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0)) if auds else None

    return {
        "video": max(vids, key=vscore) if vids else None,
        "audio": audio,
    }


# --------------------------------------------------------------------------- #
# 字幕
# --------------------------------------------------------------------------- #
def _pick_track(tracks: dict, langs: Iterable[str]) -> tuple[str, list]:
    """按语言优先级挑一条字幕轨，返回 (语言码, 该语言的格式列表)。"""
    if not tracks:
        return "", []
    for want in langs:
        for have in tracks:
            if have == want or have.startswith(want + "-"):
                return have, tracks[have]
    # 都没命中就取第一条，聊胜于无
    first = next(iter(tracks))
    return first, tracks[first]


def _track_url(formats: list, prefer: Iterable[str] = ("json3", "srv1", "vtt")) -> str:
    """在一条字幕轨的多个格式里选一个，优先 json3（带结构，最好解析）。"""
    by_ext = {f.get("ext"): f.get("url") for f in formats if f.get("url")}
    for ext in prefer:
        if ext in by_ext:
            return by_ext[ext]
    return next(iter(by_ext.values()), "")


def _parse_json3(data: dict) -> list[dict]:
    """YouTube json3 字幕 -> [{from, to, text}]。

    json3 的结构是 events[]，每个 event 有 tStartMs / dDurationMs / segs[{utf8}]。
    要注意两类噪音：没有 segs 的空事件（换行标记）、segs 里夹着的 "\\n"。
    """
    out: list[dict] = []
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
    """VTT / SRT 通用解析（两者只差时间码的分隔符和头部，正则一并覆盖）。"""
    out: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for blk in blocks:
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
        body = " ".join(ln.strip() for ln in lines[idx + 1:])
        body = re.sub(r"<[^>]+>", "", body).strip()  # 去掉 karaoke 之类的内联标签
        if body:
            out.append({"from": a, "to": b, "text": body})
    return out


def get_subtitles(info: dict, langs: Iterable[str] = LANG_PRIORITY,
                  session=None) -> dict:
    """取字幕，返回结构与 bilibili_client.get_subtitles 一致。

    优先人工字幕，没有再用自动字幕（YouTube 的 ASR）。自动字幕的专有名词
    识别率明显低于人工字幕，source 里会写明，写笔记时要标出来。

    Returns:
        {"available": True, "text", "source", "lang", "segments", "srt"} 或
        {"available": False, "error"}
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

    if session is None:
        import requests
        session = requests.Session()

    try:
        r = session.get(url, timeout=30, headers={"Referer": "https://www.youtube.com/"})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": "字幕下载失败：%s" % str(e)[:120]}

    body = r.text
    try:
        segs = _parse_json3(r.json())
    except Exception:  # noqa: BLE001  不是 json 就按 vtt/srt 解析
        segs = _parse_vtt(body)

    if not segs:
        return {"available": False, "error": "字幕解析后为空（格式可能是 srv3/srv2）"}

    text = "\n".join(s["text"] for s in segs)
    try:
        from .bili_subtitle import segments_to_srt
    except ImportError:  # 脚本方式运行
        from bili_subtitle import segments_to_srt

    logger.info("字幕获取成功（%s，%s，%d 条 / %d 字）",
                source, lang, len(segs), len(text))
    return {
        "available": True,
        "text": text,
        "source": source,
        "lang": lang,
        "kind": kind,
        "segments": segs,
        "srt": segments_to_srt(segs),
        "chars": len(text),
    }


# --------------------------------------------------------------------------- #
# 章节
# --------------------------------------------------------------------------- #
def chapters(info: dict) -> list[dict]:
    """YouTube 原生章节，直接映射成本项目 chapters 的结构。

    YouTube 的章节是 UP 主自己划的，比我们按字幕统计硬切准得多 —— 有就一定要用。
    """
    out = []
    for c in info.get("chapters") or []:
        try:
            out.append({
                "title": (c.get("title") or "").strip() or "未命名",
                "start": float(c.get("start_time") or 0),
                "end": float(c.get("end_time") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download(fmt: dict, out_path: str, session=None, retries: int = 3,
             chunk: int = 1 << 16) -> str:
    """下载一条流到 out_path（.part 断点续传，成功后原子替换）。

    URL 是 yt-dlp 已经解过 n 参数的直链，有效期几小时；过期会 403，
    届时重新 extract 一次即可。

    Returns:
        本地文件路径。
    """
    import requests

    url = (fmt or {}).get("url")
    if not url:
        raise ValueError("该格式没有下载地址")

    if session is None:
        session = requests.Session()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    part = out_path + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0

    headers = {"Referer": "https://www.youtube.com/", "User-Agent": "Mozilla/5.0"}
    if have:
        headers["Range"] = "bytes=%d-" % have

    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with session.get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 416:      # 已经下完
                    os.replace(part, out_path)
                    return out_path
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                with open(part, mode) as f:
                    for buf in r.iter_content(chunk_size=chunk):
                        f.write(buf)
            size = os.path.getsize(part)
            expect = (fmt or {}).get("filesize") or (fmt or {}).get("filesize_approx")
            if expect and size < expect * 0.95:
                raise IOError("下载不完整：%d / %d" % (size, expect))
            os.replace(part, out_path)
            logger.info("下载完成 %s（%.1f MB）", os.path.basename(out_path), size / 1048576)
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
# CLI：只做探测，方便排查「是网络问题还是视频问题」
# --------------------------------------------------------------------------- #
def _cli(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="探测 YouTube 视频（不写任何文件）")
    ap.add_argument("url")
    ap.add_argument("--clients", default=",".join(DEFAULT_CLIENTS))
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=20)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        info = extract(a.url, clients=a.clients.split(","),
                       retries=a.retries, timeout=a.timeout)
    except YouTubeUnreachable as e:
        print("\n[不可达]", e)
        return 2

    print()
    print("标题   :", info.get("title"))
    print("时长   :", "%d 分 %d 秒" % divmod(int(info.get("duration") or 0), 60))
    print("频道   :", info.get("uploader"), "/", info.get("channel"))
    print("点赞   :", info.get("like_count"))
    print("观看   :", info.get("view_count"))
    print("发布   :", info.get("upload_date"))
    print("语言   :", info.get("language"))
    print()
    print("人工字幕:", list((info.get("subtitles") or {}).keys()))
    print("自动字幕:", list((info.get("automatic_captions") or {}).keys())[:10])
    chs = chapters(info)
    print("原生章节:", "%d 个" % len(chs))
    for c in chs[:6]:
        print("   %6.0fs  %s" % (c["start"], c["title"]))
    st = pick_streams(info)
    v, au = st["video"], st["audio"]
    print()
    print("选流   :", "视频 %sp" % (v.get("height") if v else "无"),
          "| 音频 %skbps" % (round(au.get("abr") or 0) if au else "无"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
