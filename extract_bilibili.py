# -*- coding: utf-8 -*-
"""B站视频内容提取器 v2（skill 执行脚本）

策略（效率优先）：
    1. 先探测每个分P 的字幕轨道（player/wbi/v2）；
    2. 有字幕 → 直接抓字幕 JSON，几秒完成，**不下载音频**；
    3. 无字幕 → 才降级：串行下载音频（避开 CDN 限流）+ 多进程 faster-whisper 转写。

用法：
    python extract_bilibili.py BV1xxxxxx                    # 单视频/全部分P
    python extract_bilibili.py BV1xxxxxx --probe            # 只看字幕可用性，不取内容
    python extract_bilibili.py BV1xxxxxx --pages 1-10,15    # 指定分P
    python extract_bilibili.py BV1xx BV2yy --no-whisper     # 只取字幕，无字幕就跳过
    python extract_bilibili.py BV1xx --srt --model small --workers 3

输出：
    extracted/<BV>.txt / <BV>_P<nn>.txt     正文（有字幕时来源标注 官方字幕/AI字幕）
    extracted/<BV>.json                     元信息 + 每P结果汇总
    extracted/*.srt                         仅 --srt 时输出
缓存：
    extracted/.subcache/<BV>_<cid>.json     字幕结果缓存，重跑 0 请求
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import (
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures.process import BrokenProcessPool

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "src"))

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from bili_auth import destroy as destroy_session  # noqa: E402
from bili_auth import resolve_credentials  # noqa: E402
from bili_subtitle import BiliSubtitleClient  # noqa: E402

OUT_DIR = os.path.join(BASE, "extracted")
CACHE_DIR = os.path.join(OUT_DIR, ".subcache")
MODEL_DIR = os.path.join(BASE, "models")
os.makedirs(OUT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("extract")

MIN_LIKE = 5  # 用户规则：点赞 < 5 直接跳过

# 转写 worker 的进程级全局（spawn 后由 _w_init 赋值）
_MODEL = None
_W_MODEL_NAME = "small"


# --------------------------------------------------------------------------- #
# whisper 降级（进程级并行，模型在子进程内各加载一次）
# --------------------------------------------------------------------------- #
def _w_init(model_name: str, model_dir: str) -> None:
    global _MODEL, _W_MODEL_NAME
    from faster_whisper import WhisperModel

    _W_MODEL_NAME = model_name
    _MODEL = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=model_dir)


def _w_run(job: tuple) -> tuple:
    """job = (key, audio_path, language) → (key, text, error)

    除了返回纯文本，还把**带时间戳的段**写进转写缓存（`<音频>.whisper-<模型>.json`）。
    这样后续 build_note 走 transcribe_local 时能直接命中缓存：既不必再解码一遍音频，
    也保证图文底稿用的是和批量转写完全一致的文本（此前两边各转一次，底稿还会被
    字幕重新盖掉，串台问题根治不了）。
    """
    key, audio_path, language = job
    try:
        lang = None if language in ("", "auto") else language
        kwargs = {
            "vad_filter": True,
            "beam_size": 1,
            "condition_on_previous_text": False,
        }
        if lang in (None, "zh"):
            # whisper 对中文默认吐繁体，用提示词把输出压成简体并带标点
            kwargs["initial_prompt"] = "以下是普通话内容，请用简体中文转写并加上标点符号。"
        raw, _info = _MODEL.transcribe(audio_path, language=lang, **kwargs)  # type: ignore[name-defined]
        segs = [{"from": round(float(s.start), 2), "to": round(float(s.end), 2),
                 "text": (s.text or "").strip()} for s in raw]
        segs = [s for s in segs if s["text"]]
        if lang in (None, "zh"):
            from zh_convert import to_simplified  # whisper 中文常吐繁体

            for s in segs:
                s["text"] = to_simplified(s["text"])
        if segs:
            try:
                cache = "%s.whisper-%s.json" % (audio_path, _W_MODEL_NAME)  # type: ignore[name-defined]
                tmp = cache + ".part"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"model": _W_MODEL_NAME,  # type: ignore[name-defined]
                               "segments": segs,
                               "chars": sum(len(s["text"]) for s in segs),
                               "created": time.strftime("%Y-%m-%d %H:%M:%S")},
                              fh, ensure_ascii=False)
                os.replace(tmp, cache)
            except Exception as e:  # noqa: BLE001
                log.debug("转写缓存写入失败: %s", e)
        return key, "".join(s["text"] for s in segs).strip(), ""
    except Exception as e:  # noqa: BLE001
        return key, "", str(e)[:200]


def transcribe_batch(jobs: list[tuple], model_name: str, workers: int) -> dict[str, str]:
    """jobs: [(key, path, lang)] → {key: text}

    多进程在 Windows 上依赖 spawn（子进程会重新 import `__main__`）。
    若运行环境不支持（交互式解释器、被沙箱限制等），自动退回串行，
    而不是让整批任务因 BrokenProcessPool 全部失败。
    """
    out: dict[str, str] = {}
    if not jobs:
        return out
    if workers > 1 and len(jobs) > 1:
        log.info("并行转写 %d 个音频（%d 进程，模型 %s）", len(jobs), workers, model_name)
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_w_init,
                                     initargs=(model_name, MODEL_DIR)) as pool:
                for key, text, err in pool.map(_w_run, jobs):
                    out[key] = text
                    if err:
                        log.error("%s 转写失败: %s", key, err)
            return out
        except (BrokenProcessPool, OSError, PermissionError) as e:
            log.warning("多进程不可用（%s），退回串行转写", type(e).__name__)
            out.clear()

    log.info("串行转写 %d 个音频（模型 %s）", len(jobs), model_name)
    _w_init(model_name, MODEL_DIR)
    for job in jobs:
        key, text, err = _w_run(job)
        out[key] = text
        if err:
            log.error("%s 转写失败: %s", key, err)
    return out


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def parse_pages(spec: str) -> list[int]:
    """'1-10,15,20-22' → [1..10,15,20,21,22]"""
    if not spec:
        return []
    pages: set[int] = set()
    for chunk in spec.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            pages.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            try:
                pages.add(int(chunk))
            except ValueError:
                continue
    return sorted(p for p in pages if p > 0)


def fmt_dur(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def note_dir_name(bvid: str, page_no: int) -> str:
    """图文素材目录名：`<BV>_P<NN>`。

    刻意不用视频标题做目录名 —— 标题里的 `/`、`..`、控制字符会造成路径注入，
    而 bvid + 分P号已经能唯一定位。
    """
    return f"{bvid}_P{page_no:02d}"


SOURCE_LABEL = {"cc": "官方CC字幕", "ai": "B站AI字幕"}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def process(bvid: str, client: BiliSubtitleClient, args, login_ok: bool = True) -> dict:
    log.info("=" * 62)
    view = client.get_view(bvid)
    stat = view["stat"]
    log.info("《%s》", view["title"])
    log.info("UP:%s | 赞:%s | 分P:%d | 总时长:%s", view["owner"], stat["like"], len(view["pages"]), fmt_dur(view["duration"]))

    summary = {
        "bvid": bvid,
        "title": view["title"],
        "owner": view["owner"],
        "pubdate": time.strftime("%Y-%m-%d", time.localtime(view["pubdate"] or 0)),
        "duration": view["duration"],
        "stat": stat,
        "charge_exclusive": view["charge_exclusive"],
        "pages": [],
    }

    if stat["like"] and stat["like"] < MIN_LIKE and not args.ignore_like_filter:
        log.warning("点赞 %s < %d，按规则跳过（--ignore-like-filter 可强制处理）", stat["like"], MIN_LIKE)
        summary["skipped"] = "like_too_low"
        return summary
    if view["charge_exclusive"] and not args.no_whisper:
        log.warning("充电专属视频：字幕不可取，音频需已充电账号，跳过")
        summary["skipped"] = "charge_exclusive"
        return summary

    pages = view["pages"]
    want = parse_pages(args.pages) if args.pages else []
    if want:
        picked = [p for p in pages if p["page"] in want]
        missing = sorted(set(want) - {p["page"] for p in picked})
        if missing:
            log.warning("指定分P不存在，忽略: %s", missing)
        if not picked:
            log.error("没有匹配的分P")
            return summary
    else:
        picked = pages

    # ---------- 阶段 0：探测字幕（并发） ----------
    log.info("探测字幕可用性（%d 个分P，并发 %d）...", len(picked), args.probe_workers)

    def probe_one(p: dict) -> dict:
        row = dict(p, has_sub=False, lan="", lan_doc="", kind="")
        try:
            tracks = client.list_tracks(view["aid"], p["cid"], bvid)
            best = BiliSubtitleClient.pick_track(tracks)
            if best:
                lan = str(best.get("lan", ""))
                row.update(has_sub=True, lan=lan, lan_doc=best.get("lan_doc", lan),
                           kind="ai" if lan.startswith("ai-") else "cc")
        except Exception as e:  # noqa: BLE001
            row["probe_error"] = str(e)[:120]
        return row

    if len(picked) > 1 and args.probe_workers > 1:
        with ThreadPoolExecutor(max_workers=args.probe_workers) as pool:
            rows = list(pool.map(probe_one, picked))
    else:
        rows = [probe_one(p) for p in picked]
    rows.sort(key=lambda r: r["page"])

    with_sub = [r for r in rows if r["has_sub"]]
    no_sub = [r for r in rows if not r["has_sub"]]
    log.info("字幕可用 %d/%d（官方CC %d / AI %d），需转写 %d",
             len(with_sub), len(rows),
             sum(1 for r in with_sub if r["kind"] == "cc"),
             sum(1 for r in with_sub if r["kind"] == "ai"),
             len(no_sub))

    if args.probe:
        for r in rows:
            tag = f"{SOURCE_LABEL.get(r['kind'], '字幕')}({r['lan_doc'] or r['lan']})" if r["has_sub"] else "无字幕"
            print(f"  P{r['page']:02d} | {tag:<24} | {fmt_dur(r['duration']):>6} | {r['part'][:38]}")
        summary["pages"] = rows
        return summary

    # ---------- 阶段 1：抓字幕内容 ----------
    texts: dict[int, dict] = {}
    if args.prefer_subtitle and with_sub:
        log.info("抓取字幕内容 ...")
        t0 = time.time()

        def fetch_one(r: dict) -> tuple:
            return r, client.fetch(bvid, cid=r["cid"], aid=view["aid"],
                                   prefer_lang=args.lang, force=args.force,
                                   duration=r.get("duration") or 0,
                                   verify_summary=args.verify_summary)

        if len(with_sub) > 1 and args.probe_workers > 1:
            with ThreadPoolExecutor(max_workers=args.probe_workers) as pool:
                results = list(pool.map(fetch_one, with_sub))
        else:
            results = [fetch_one(r) for r in with_sub]

        for r, res in results:
            if res.get("available"):
                texts[r["page"]] = {
                    "body": res["text"],
                    "source": SOURCE_LABEL.get(res["kind"], "字幕"),
                    "lan": res.get("lang_doc") or res.get("lang", ""),
                    "srt": res.get("srt", ""),
                    "chars": res.get("chars", 0),
                    "cached": res.get("cached", False),
                }
            else:
                r["has_sub"] = False
                r["sub_error"] = res.get("message", "")
                if res.get("reason") == "coverage_bad":
                    log.warning(
                        "P%s 字幕覆盖度与分P时长不符，判定为可疑字幕（可能是别的视频的），改走转写：%s",
                        r["page"],
                        res.get("message", "")[:160],
                    )
                elif res.get("reason") == "content_suspect":
                    log.warning(
                        "P%s 字幕内容与该视频不符（长度对得上但讲的是别的），判定串台，改走转写：%s",
                        r["page"],
                        res.get("message", "")[:160],
                    )
                elif res.get("reason") == "track_error":
                    log.warning("P%s 字幕轨道查询失败（网络问题，不是没字幕）：%s",
                                r["page"], res.get("message", "")[:160])
                else:
                    log.debug("P%s 字幕内容失败: %s", r["page"], res.get("message"))
        log.info("字幕抓取完成，用时 %.1fs，成功 %d 条", time.time() - t0, len(texts))

    # ---------- 阶段 2：缺口走 whisper ----------
    need = [r for r in rows if r["page"] not in texts]
    if need and not texts and not login_ok and not args.no_whisper:
        log.warning("全部 %d 个分P 都探测不到字幕，且登录态无效 —— 大概率是 SESSDATA 过期导致 AI 字幕不可见。", len(rows))
        log.warning("建议先刷新 Cookie 再跑（否则将进入慢速转写）：--require-subtitle 可直接中止")
    if need and not args.no_whisper and args.require_subtitle:
        log.error("--require-subtitle 已启用且存在无字幕分P，中止：%s", [r["page"] for r in need][:10])
        summary["aborted"] = "require_subtitle"
        summary["pages"] = rows
        return summary
    if need and not args.no_whisper:
        if not client.has_login:
            log.warning("未登录状态下部分 AI 字幕取不到；在 .env 配 BILIBILI_SESSDATA 可显著提升命中率")

        log.info("需转写 %d 个分P：先串行下载音频（避开 CDN 限流）", len(need))
        from bilibili_client import download_audio

        jobs: list[tuple] = []
        for r in need:
            try:
                path = download_audio(bvid, cid=r["cid"])
                jobs.append((str(r["page"]), path, args.lang))
            except Exception as e:  # noqa: BLE001
                r["transcribe_error"] = f"下载失败: {str(e)[:120]}"
                log.error("P%s 音频下载失败: %s", r["page"], str(e)[:120])
                time.sleep(2)

        if jobs:
            got = transcribe_batch(jobs, args.model, args.workers)
            for r in need:
                txt = got.get(str(r["page"]), "")
                if txt:
                    texts[r["page"]] = {
                        "body": txt,
                        "source": f"whisper-{args.model} 本地转写",
                        "lan": args.lang,
                        "srt": "",
                        "chars": len(txt),
                        "cached": False,
                    }
    elif need:
        for r in need:
            r.setdefault("sub_error", "无字幕，且 --no-whisper 已禁用转写")

    # ---------- 阶段 3：生成图文素材（不再输出纯文本）----------
    # 产物是 notes/<BV>_P<NN>/：transcript.md + frames.json + frames/ + contacts/
    # 后续由 agent 读底稿挑帧写 note.md —— 文字与画面必须成对出现。
    from types import SimpleNamespace

    from src.bili_notes import build_note as build_note_assets

    ok, fail = 0, []
    for r in rows:
        item = texts.get(r["page"])
        if not item:
            fail.append(r["page"])
            summary["pages"].append({**r, "status": "failed", "error": r.get("sub_error") or r.get("transcribe_error")})
            continue

        # 这一 P 的文本若来自转写，必须让 build_note 也跳过字幕：
        # build_note 里还会自己 fetch 一次字幕，不传这个开关的话，串台字幕会把
        # 刚转好的正确文本重新盖掉（转写结果有缓存，重复调用不会二次解码）。
        from_whisper = str(item.get("source", "")).startswith("whisper")
        ns = SimpleNamespace(
            target=bvid,
            page=str(r["page"]),
            skip_existing=args.skip_existing,
            force=args.force,
            no_whisper=args.no_whisper,
            model=args.model,
            chapters="",
            auto_n=0,
            quality=args.quality,
            frames=args.frames,
            cols=args.cols,
            force_whisper=from_whisper,
        )
        try:
            res = build_note_assets(ns, r["page"])
        except Exception as e:  # noqa: BLE001
            log.error("P%02d 图文素材生成失败：%s", r["page"], str(e)[:200])
            fail.append(r["page"])
            summary["pages"].append({**r, "status": "failed", "error": f"图文素材: {e}"})
            continue

        ok += 1
        summary["pages"].append({**r, "status": "ok", "source": item["source"],
                                 "lang": item["lan"], "chars": item["chars"],
                                 "cached": item["cached"],
                                 "frames": res.get("frames", 0),
                                 "chapters": res.get("chapters", 0),
                                 "out": f"notes/{bvid}_P{r['page']:02d}/"})

    summary["ok"] = ok
    summary["failed_pages"] = fail
    with open(os.path.join(OUT_DIR, f"{bvid}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log.info("完成 %d/%d，图文素材在 notes/", ok, len(rows))
    if fail:
        log.warning("失败分P: %s", fail)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description="B站视频 → 图文笔记素材（字幕 + 关键帧，缺一不出）。"
                    "默认下载原视频抽帧，再结合字幕，产物是 notes/<BV>_P<NN>/")
    ap.add_argument("bvids", nargs="*", help="BV号 或 https://www.bilibili.com/video/BVxxx 链接")
    ap.add_argument("--pages", default="", help="分P范围，如 1-10,15")
    ap.add_argument("--probe", action="store_true", help="只探测字幕可用性，不抓内容、不转写")
    ap.add_argument("--no-whisper", action="store_true", help="无字幕时直接跳过，不下载音频转写")
    ap.add_argument("--no-verify", dest="verify_summary", action="store_false",
                    help="跳过「字幕内容 vs 视频AI总结」交叉校验（默认开启，防串台）")
    ap.add_argument("--prefer-subtitle", dest="prefer_subtitle", action="store_true", default=True)
    ap.add_argument("--no-subtitle", dest="prefer_subtitle", action="store_false", help="强制走转写（调试用）")
    ap.add_argument("--force", action="store_true", help="忽略字幕缓存")
    ap.add_argument("--model", default="small", help="whisper 模型：base/small/medium（默认 small）")
    ap.add_argument("--workers", type=int, default=2, help="转写进程数（默认 2）")
    ap.add_argument("--probe-workers", type=int, default=4, help="字幕并发数（默认 4）")
    ap.add_argument("--lang", default="zh", help="字幕优选语言/转写语言，auto 为自动（默认 zh）")
    # —— 抽帧相关：抽帧是必经步骤，这里只调参数，不能关 ——
    ap.add_argument("--frames", type=int, default=6, help="每章候选帧数（默认 6）")
    ap.add_argument("--quality", type=int, default=64, help="清晰度上限：64=720P 32=480P（默认 64）")
    ap.add_argument("--cols", type=int, default=5, help="联系表每行格数（默认 5）")
    ap.add_argument("--skip-existing", action="store_true", help="已有 meta.json 的分P跳过（续跑用）")
    ap.add_argument("--ignore-like-filter", action="store_true", help=f"忽略点赞 < {MIN_LIKE} 跳过规则")
    ap.add_argument("--require-subtitle", action="store_true", help="任一分P无字幕就中止，不进入转写")
    ap.add_argument("--check", action="store_true", help="自检：登录态 / WBI 签名 / 字幕接口连通性")
    ap.add_argument("--debug", action="store_true", help="输出调试日志（含每次请求/重试/校验细节）")
    ap.add_argument("--log-file", default="", help="把完整日志另存到文件（便于事后定位）")
    ap.add_argument("--login", choices=["auto", "session", "env", "qr", "none"], default="auto",
                    help="auto=会话→.env→扫码；session=只用一次性会话；env=只用.env；qr=强制扫码；none=匿名")
    ap.add_argument("--keep-session", action="store_true", help="任务结束不注销会话（默认用完即焚）")
    ap.add_argument("--logout", action="store_true", help="只注销并焚毁当前一次性会话")
    ap.add_argument("--qr-out", default="", help="登录二维码 PNG 输出路径")
    ap.add_argument("--session-ttl", type=int, default=1800, help="扫码会话有效期秒数（默认 1800）")
    ap.add_argument("--qr-timeout", type=int, default=180, help="等待扫码秒数（默认 180）")
    args = ap.parse_args()

    # 调试日志：--debug 打细节，--log-file 把完整日志落盘（便于事后定位串台）
    if args.debug or args.log_file:
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if args.log_file:
            os.makedirs(os.path.dirname(os.path.abspath(args.log_file)) or ".", exist_ok=True)
            fh = logging.FileHandler(args.log_file, mode="w", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            handlers.append(fh)
        logging.basicConfig(
            level=logging.DEBUG if args.debug else logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
            handlers=handlers,
            force=True,
        )

    if args.logout:
        destroy_session()
        return

    client = BiliSubtitleClient(cache_dir=CACHE_DIR)
    auth = {"creds": {}, "source": "none", "ephemeral": False, "ttl": 0}
    if args.login != "none":
        auth = resolve_credentials(
            env_file=os.path.join(BASE, ".env"),
            login=args.login,
            qr_out=args.qr_out,
            ttl=args.session_ttl,
            qr_timeout=args.qr_timeout,
        )
        if auth.get("creds"):
            client.apply_credentials(auth["creds"])
        elif auth.get("error"):
            log.warning("认证未就绪：%s", auth["error"])

    ok, who = client.login_check()
    if not client.has_login:
        log.warning("无登录态：只能取官方 CC 字幕，B站 AI 字幕拿不到")
    elif not ok:
        log.warning("登录态无效 —— %s", who)
    else:
        log.info("登录态正常：%s%s", who,
                 "（一次性会话，任务结束自动注销）" if auth.get("ephemeral") else "")

    if args.check:
        print(f"  .env 路径      : {os.path.join(BASE, '.env')}")
        print(f"  认证来源       : {auth.get('source')} {'(一次性)' if auth.get('ephemeral') else ''}")
        print(f"  登录校验       : {'✓ ' + who if ok else '✗ ' + who}")
        print(f"  会话剩余       : {auth.get('ttl', 0) // 60} 分钟")
        print(f"  字幕缓存目录   : {CACHE_DIR}")
        print(f"  whisper 模型   : {args.model}（{MODEL_DIR}）")
        if auth.get("ephemeral") and not args.keep_session:
            destroy_session(auth["creds"])
        return

    if not args.bvids:
        ap.print_help()
        if auth.get("ephemeral") and not args.keep_session:
            destroy_session(auth["creds"])
        return

    if args.probe:
        log.info("提示：--probe 结束后仍会注销一次性会话，避免重复扫码请加 --keep-session")

    try:
        for raw in args.bvids:
            bvid = raw.strip().split("/video/")[-1].split("/")[0].split("?")[0]
            if not bvid.startswith("BV"):
                log.error("无法识别 BV 号: %s", raw)
                continue
            try:
                process(bvid, client, args, login_ok=ok)
            except KeyboardInterrupt:
                log.warning("已中断")
                break
            except Exception as e:  # noqa: BLE001
                log.error("%s 处理失败: %s", bvid, e)
    finally:
        if auth.get("ephemeral") and not args.keep_session:
            destroy_session(auth["creds"])
        elif auth.get("ephemeral"):
            log.info("已保留一次性会话（--keep-session），记得稍后 --logout")


if __name__ == "__main__":
    main()
