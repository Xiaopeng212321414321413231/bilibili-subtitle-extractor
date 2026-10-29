"""B站视频 -> 图文笔记「素材包」：字幕优先取文 -> 自动分章 -> 分批抽帧 -> 底稿 + 联系表。

三条设计原则
------------
1. **文本永远先试 B站 字幕**：命中缓存的 P02 只要 1.8 秒，术语还比 ASR 准；
   只有字幕不可用（无轨道 / 未登录 / 充电专属）才下载音轨做本地转写。
   顺序不能反：转写 4 分钟起步，而字幕往往几百毫秒。
2. **画面按章节分批取**：取完一章立刻落盘（frames.json 增量写），长视频可以中断续跑；
   字幕拿不到时也能先转写再抽帧，两条链路互不阻塞。
3. **不调用任何 LLM/VLM API**：分章靠统计（词汇衔接 + 话语标记），选图与成文交给人/agent。
   这样零成本、可复现，也不依赖某家模型的可用性。

用法
----
    python -m src.bili_notes BV1hbM9zrEwD --page 2
    python -m src.bili_notes <url> --chapters notes/xxx_chapters.json --frames 8
    python -m src.bili_notes BV1hbM9zrEwD --page 2 --no-frames      # 只要文本与分章
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("bili_notes")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_DIR = os.path.join(ROOT, "notes")
MODEL_DIR = os.path.join(ROOT, "models")

# 话语标记：出现在块首说明大概率在起新话题/新步骤。
# 这是纯规则的先验，用来把"词汇衔接骤降"的边界往真正的话题句上拉。
MARKERS = ("接下来", "那么", "然后", "首先", "其次", "第二步", "第三步", "第2步", "第3步",
           "再看", "我们来看", "下面", "总结", "最后", "所以", "但是", "另外", "回到")
MARKER_BONUS = 0.06


# ---------------------------------------------------------------------------
# 文本：字幕优先，转写兜底
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    """中文二元组 + 英文词，作为"这一段在讲什么"的粗糙但够用的特征。"""
    t = (text or "").lower()
    toks = re.findall(r"[a-z][a-z0-9_]{2,}", t)
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", t)
    toks += [cjk[i:i + 2] for i in range(len(cjk) - 1)]
    return toks


def _cos(a, b) -> float:
    (ca, na), (cb, nb) = a, b
    if not ca or not cb:
        return 0.0
    if len(ca) > len(cb):
        ca, cb = cb, ca
    dot = sum(v * cb.get(k, 0) for k, v in ca.items())
    return dot / (na * nb)


def _vec(text: str):
    c = Counter(_tokens(text))
    return c, math.sqrt(sum(v * v for v in c.values())) or 1.0


def transcribe_local(bvid: str, cid: int, model_name: str = "small") -> dict:
    """本地 faster-whisper 转写（仅当字幕不可用时调用）。"""
    from src.bilibili_client import download_audio

    t0 = time.time()
    audio = download_audio(bvid, cid)
    os.makedirs(MODEL_DIR, exist_ok=True)
    from faster_whisper import WhisperModel

    # 模型已经在本地就强制离线加载：否则 huggingface_hub 会走代理去校验 etag，
    # 代理抽风时直接 502，把「本地明明有模型」变成「转写失败」。
    local_only = os.path.isdir(os.path.join(MODEL_DIR, f"models--Systran--faster-whisper-{model_name}"))
    model = WhisperModel(model_name, device="cpu", compute_type="int8",
                         download_root=MODEL_DIR, local_files_only=local_only)
    segs, info = model.transcribe(
        audio, language="zh", vad_filter=True, beam_size=1,
        condition_on_previous_text=False,
        initial_prompt="以下是普通话内容，请用简体中文转写并加上标点符号。",
    )
    out = []
    for s in segs:
        txt = (s.text or "").strip()
        if txt:
            out.append({"from": round(float(s.start), 2), "to": round(float(s.end), 2), "text": txt})
    try:
        from src.zh_convert import to_simplified

        for s in out:
            s["text"] = to_simplified(s["text"])
    except Exception as e:  # noqa: BLE001
        log.debug("繁简转换不可用: %s", e)
    return {"source": "whisper", "model": model_name, "segments": out,
            "chars": sum(len(s["text"]) for s in out),
            "cost": round(time.time() - t0, 1), "audio": audio,
            "language": getattr(info, "language", "zh")}


def get_transcript(client, bvid: str, cid: int, duration: int, allow_whisper: bool = True,
                   model: str = "small", lang: str = "zh", force: bool = False) -> dict:
    """先取字幕；不可用且允许时才转写。返回值统一成 {source, segments, chars, cost}。"""
    t0 = time.time()
    r = client.fetch(bvid, cid, prefer_lang=lang, force=force, duration=duration)
    if r.get("available") and r.get("segments"):
        segs = [{"from": float(s["from"]), "to": float(s["to"]), "text": s["text"].strip()}
                for s in r["segments"] if s.get("text", "").strip()]
        log.info("字幕命中：%d 段 / %d 字 / %.1fs（kind=%s%s）", len(segs),
                 sum(len(s["text"]) for s in segs), time.time() - t0,
                 r.get("kind"), "，缓存" if r.get("cached") else "")
        return {"source": "subtitle", "kind": r.get("kind"), "segments": segs,
                "chars": sum(len(s["text"]) for s in segs),
                "cost": round(time.time() - t0, 1), "cached": bool(r.get("cached"))}

    reason = r.get("reason", "unknown")
    log.warning("字幕不可用（%s：%s）", reason, (r.get("message") or "")[:100])
    if not allow_whisper:
        raise SystemExit(f"字幕不可用且已禁用转写：{reason}")
    log.info("转入本地转写兜底（模型 %s，长视频可能需要数分钟）…", model)
    t = transcribe_local(bvid, cid, model)
    t["fallback_reason"] = reason
    log.info("转写完成：%d 段 / %d 字 / %.1fs", len(t["segments"]), t["chars"], t["cost"])
    return t


# ---------------------------------------------------------------------------
# 分章：词汇衔接骤降 + 话语标记，纯统计不调模型
# ---------------------------------------------------------------------------

def _merge_blocks(segments: list[dict], max_chars: int = 130) -> list[dict]:
    """把逐句字幕合并成话语块：太碎的句子算不出稳定的词汇分布。

    每块记住自己覆盖的片段区间 [si, ei)，边界吸附时要在片段级再切一次。
    """
    blocks, buf, start, end, size, si = [], [], None, 0.0, 0, 0
    for idx, s in enumerate(segments):
        if start is None:
            start, si = s["from"], idx
        buf.append(s["text"])
        end = s["to"]
        size += len(s["text"])
        if size >= max_chars:
            blocks.append({"start": start, "end": end, "text": "".join(buf),
                           "si": si, "ei": idx + 1})
            buf, start, size = [], None, 0
    if buf:
        blocks.append({"start": start, "end": end, "text": "".join(buf),
                       "si": si, "ei": len(segments)})
    return blocks


def auto_chapters(segments: list[dict], duration: float, target: int = 0,
                  min_sec: float = 90.0, max_chapters: int = 14,
                  max_len: float = 720.0) -> list[dict]:
    """按「相邻话语块词汇衔接的局部低谷」切章。

    TextTiling 的简化版：相邻块的余弦相似度掉到局部极小，说明话题换了。
    深度 = 两侧平均相似度 − 该点相似度；再给块首是话语标记（"接下来/总结"）的边界加分，
    把切点往真正的话题句上拉。选完后再把过长章节按章内最高分边界二次切开
    （不然一章能拉到 15 分钟，配图密度就废了）。
    """
    blocks = _merge_blocks(segments)
    n = len(blocks)
    if n < 6:
        return _even(duration, max(1, min(4, n)))

    vecs = [_vec(b["text"]) for b in blocks]
    sims = [0.0] + [_cos(vecs[i - 1], vecs[i]) for i in range(1, n)]  # sims[i] = 块 i-1 与 i 的衔接

    scored: dict[int, float] = {}
    w = 2
    for i in range(1, n):
        # 首尾窗口会退化成空，用当前值兜底，保证 base 恒有定义
        left = sims[max(1, i - w):i] or [sims[i]]
        right = sims[i + 1:i + 1 + w] or [sims[i]]
        base = (sum(left) / len(left) + sum(right) / len(right)) / 2
        depth = base - sims[i]
        bonus = MARKER_BONUS if blocks[i]["text"].startswith(MARKERS) else 0.0
        scored[i] = depth + bonus

    if not target:
        target = max(4, min(max_chapters, round(duration / 420)))  # 默认约 7 分钟一章

    def t_of(i: int) -> float:
        return blocks[i]["start"]

    picked: set[int] = set()

    def can_add(i: int) -> bool:
        t = t_of(i)
        if t < min_sec or t > duration - min_sec or scored.get(i, 0) <= 0:
            return False
        return all(abs(t - t_of(j)) >= min_sec for j in picked)

    for i in sorted(scored, key=lambda k: scored[k], reverse=True):
        if len(picked) >= target - 1:
            break
        if can_add(i):
            picked.add(i)

    # 过长章节二次切分：取章内得分最高、且与既有边界不冲突的候选
    for _round in range(max_chapters):
        bounds = [0] + sorted(picked) + [n]

        def span_len(k: int) -> float:
            a, b = bounds[k], bounds[k + 1]
            end = blocks[b]["start"] if b < n else duration
            return end - blocks[a]["start"]

        k = max(range(len(bounds) - 1), key=span_len)
        if span_len(k) <= max_len or len(picked) >= max_chapters - 1:
            break
        a, b = bounds[k], bounds[k + 1]
        interior = [i for i in range(a + 1, b) if can_add(i)]
        if not interior:
            break
        picked.add(max(interior, key=lambda x: scored[x]))

    # 块边界 -> 片段边界：吸附到窗口内第一个以话语标记开头的片段
    cuts: list[int] = [0]
    for i in sorted(picked):
        si = _snap_cut(blocks, segments, i)
        # 吸附后可能与上一个切口挤在一起，挤掉了就跳过这个切口
        if segments[si]["from"] - segments[cuts[-1]]["from"] >= min_sec * 0.6:
            cuts.append(si)
    cuts.append(len(segments))

    chapters = []
    for idx in range(len(cuts) - 1):
        a, b = cuts[idx], cuts[idx + 1]
        seg = segments[a:b]
        if not seg:
            continue
        head = _head_text(seg)
        chapters.append({
            "title": _condense(head),
            "start": round(seg[0]["from"], 2),
            "end": round(segments[b]["from"] if b < len(segments) else duration, 2),
            "first": head[:40],
            "keywords": _keywords([{"text": "".join(s["text"] for s in seg)}], blocks),
        })
    return chapters


def _snap_cut(blocks: list[dict], segments: list[dict], i: int, limit_blocks: int = 2) -> int:
    """把块边界换算成片段边界，并吸附到最近的「起句点」。

    AI 字幕按短语切、没有句末标点，块边界经常落在半句中间
    （"分制的一个思想好分钟思想呢…"）。这里在窗口内找第一个以话语标记开头的片段，
    找到就把章节起点挪过去；找不到就保持原位——宁可截断，也不硬造通顺句子。
    """
    for bi in range(i, min(len(blocks), i + limit_blocks)):
        b = blocks[bi]
        for si in range(b["si"], b["ei"]):
            if segments[si]["text"].startswith(MARKERS):
                return si
    return blocks[i]["si"]


_LEAD_FILLER = re.compile(
    r"^(?:那么|然后|接下来|首先|其次|我们|你看|大家看|就是说|这个|那个|所以|但是"
    r"|好|嗯|呃|额|呢|啊|哦|噢|诶|哎|嘛|吧)+")

# 整段只有虚词/连接词的片段（"那么"、"呢"、"呃呃"）：算章节起点没意义，拼标题时直接跳过。
_FILLER_ONLY = re.compile(r"^[呢嗯呃额啊哦噢诶哎嘛吧那么然后好这个那个就是所以但是对立哦]+$")


def _head_text(seg: list[dict], take: int = 3, skip: int = 3) -> str:
    """取章节开头几个片段拼成标题素材，跳过纯虚词片段。"""
    picked, used = [], 0
    for s in seg[:skip + take]:
        t = (s["text"] or "").strip()
        if not t:
            continue
        if _FILLER_ONLY.match(t) and len(picked) == 0:
            continue      # 开头的"那么""呢"不参与标题
        picked.append(t)
        used += 1
        if used >= take:
            break
    return "".join(picked) if picked else (seg[0]["text"] if seg else "")


def _condense(text: str, limit: int = 18) -> str:
    """用首句压一个能看的临时标题：去掉句首口头语，切到第一个标点。

    关键词拼标题在中文上不成立（二元组切出来是"边这/它所"这种碎片），
    所以宁可取首句——至少它一定是这段的原话，人改起来也快。
    """
    t = _LEAD_FILLER.sub("", (text or "").strip())
    m = re.match(r"^(.{6,}?)[，。！？,;；]", t)
    if m:
        t = m.group(1)
    t = t.strip("，。！？、 ")
    return t[:limit] + ("…" if len(t) > limit else "")


def _keywords(chapter_blocks: list[dict], all_blocks: list[dict], top: int = 4) -> list[str]:
    """章内高频、全局不常见的二元组（简易 TF-IDF），只进 meta 供人参考，不用来拼标题。"""
    in_c = Counter()
    for b in chapter_blocks:
        in_c.update(set(_tokens(b["text"])))
    in_all = Counter()
    for b in all_blocks:
        in_all.update(set(_tokens(b["text"])))
    n = len(all_blocks) or 1
    scored = []
    for term, tf in in_c.items():
        if len(term) < 2 or tf < 2:
            continue
        idf = math.log((n + 1) / (in_all[term] + 0.5))
        scored.append((tf * idf, term))
    scored.sort(reverse=True)
    return [t for _, t in scored[:top]]


def _even(duration: float, n: int) -> list[dict]:
    step = duration / max(1, n)
    return [{"title": f"第{i + 1}章", "start": round(i * step, 2),
             "end": round((i + 1) * step, 2), "first": "", "keywords": []} for i in range(n)]


def load_chapters(path: str) -> list[dict]:
    from src.bili_frames import _parse_time

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("chapters") if isinstance(raw, dict) else raw
    out = []
    for c in items:
        out.append({"title": str(c.get("title") or "未命名"),
                    "start": _parse_time(c["start"]), "end": _parse_time(c["end"])})
    return sorted(out, key=lambda x: x["start"])


# ---------------------------------------------------------------------------
# 底稿
# ---------------------------------------------------------------------------

def _mmss(sec: float) -> str:
    m, s = divmod(int(max(0, sec)), 60)
    return f"{m:02d}:{s:02d}"


def write_transcript(chapters: list[dict], segments: list[dict], duration: float,
                     meta: dict, out_path: str) -> str:
    """章节化底稿：每章带时间范围 + 逐句原话（带时间戳）。

    这是成文的唯一事实来源——写笔记时只允许从这里取数字、术语与结论。
    """
    lines = [f"# {meta['title']}\n",
             f"> 视频：{meta['url']} ｜ 时长 {_mmss(duration)} ｜ 文本来源：{meta['text_source']}"
             f"（{len(segments)} 段 / {meta['chars']} 字，耗时 {meta['text_cost']}s）\n"]
    for i, c in enumerate(chapters, 1):
        lines.append(f"\n## {i:02d} {c['title']}")
        lines.append(f"\n*{_mmss(c['start'])} – {_mmss(min(c['end'], duration))}"
                     f"（{int(min(c['end'], duration) - c['start'])} 秒）*\n")
        if c.get("keywords"):
            lines.append("关键词：" + " / ".join(c["keywords"]) + "\n")
        for s in segments:
            if c["start"] <= s["from"] < c["end"]:
                lines.append(f"- **[{_mmss(s['from'])}]** {s['text']}")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_pages(spec: str) -> list[int]:
    """解析分P范围：`1-17` / `1,3,5` / `1-5,8`。"""
    out: list[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m src.bili_notes",
        description="B站视频 -> 图文笔记素材包（字幕优先、分批抽帧、不调 API）")
    ap.add_argument("target", help="BV号 或视频链接")
    ap.add_argument("--page", type=int, default=1, help="单个分P序号（默认 1）")
    ap.add_argument("--pages", default="", help="批量分P：1-17 / 1,3,5（给了就忽略 --page）")
    ap.add_argument("--skip-existing", action="store_true", help="已有 meta.json 的分P直接跳过")
    ap.add_argument("--out", default="", help="输出目录（批量时不能指定，按P自动建目录）")
    ap.add_argument("--chapters", default="", help="手工章节 JSON（给了就不自动分章）")
    ap.add_argument("--auto-chapters", type=int, default=0, dest="auto_n",
                    help="强制章节数（默认按 7 分钟/章推算）")
    ap.add_argument("--frames", type=int, default=6, help="每章候选帧数（默认 6）")
    ap.add_argument("--quality", type=int, default=64, help="清晰度上限：64=720P 32=480P（默认 64）")
    ap.add_argument("--cols", type=int, default=5, help="联系表每行格数（默认 5）")
    ap.add_argument("--no-frames", action="store_true", help="只出文本与分章，不下载视频")
    ap.add_argument("--no-whisper", action="store_true", help="字幕不可用时直接报错，不转写")
    ap.add_argument("--model", default="small", help="转写模型 base/small/medium（默认 small）")
    ap.add_argument("--force", action="store_true", help="忽略字幕缓存")
    ap.add_argument("--audit", action="store_true",
                    help="合集内一致性体检（对比各分P词汇分布，揪出串台字幕）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(message)s", force=True)

    pages = parse_pages(args.pages) if args.pages else [args.page]
    single = len(pages) == 1

    if args.audit:
        from src.bilibili_client import _sub_client

        m = re.search(r"(BV[0-9A-Za-z]{10})", args.target)
        if not m:
            log.error("无法从参数里解析出 BV 号")
            return 2
        all_pages = [p["page"] for p in _sub_client().get_view(m.group(1)).get("pages", [])]
        _print_audit(audit_collection(_sub_client(), m.group(1), all_pages))
        if single:
            return 0

    summary = []
    for i, pno in enumerate(pages, 1):
        if not single:
            log.info("\n===== [%d/%d] 第 %d P =====", i, len(pages), pno)
        try:
            summary.append(_run(args, pno, args.out if single else ""))
        except SystemExit as e:
            log.error("第 %d P 跳过：%s", pno, e)
            summary.append({"page": pno, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.error("第 %d P 失败：%s", pno, str(e)[:160])
            summary.append({"page": pno, "error": str(e)[:160]})

    if not single:
        ok = [s for s in summary if not s.get("error")]
        log.info("\n===== 批量完成：成功 %d/%d，总字数 %d =====",
                 len(ok), len(summary), sum(s.get("chars", 0) for s in ok))
        log.info("%-4s %-24s %7s %4s %5s %6s", "P", "标题", "字数", "章", "帧", "耗时")
        for s in summary:
            if s.get("error"):
                log.info("P%-3d 失败: %s", s["page"], s["error"][:56])
            else:
                log.info("P%-3d %-24s %7d %4d %5d %5ss%s", s["page"], s.get("title", "")[:22],
                         s.get("chars", 0), s.get("chapters", 0), s.get("frames", 0),
                         s.get("cost", 0), " (跳过)" if s.get("skipped") else "")

        # 批处理跑完自动体检一次：串台字幕往往要跟兄弟分P对比才看得出来
        try:
            from src.bilibili_client import _sub_client

            bvid = re.search(r"(BV[0-9A-Za-z]{10})", args.target).group(1)
            all_pages = [p["page"] for p in _sub_client().get_view(bvid).get("pages", [])]
            _print_audit(audit_collection(_sub_client(), bvid, all_pages))
        except Exception as e:  # noqa: BLE001
            log.debug("合集体检跳过: %s", e)
    return 0


def audit_collection(client, bvid: str, pages: list[int],
                     min_affinity: float = 0.45) -> list[dict]:
    """第三层校验：合集内一致性。

    B站 的 AI 总结（conclusion/get）只有部分分P才有；拿不到时 fetch() 的第二层
    内容校验会被整个跳过，只剩「覆盖度」在把关——长度正好相近的串台就会溜进来
    （P14 就是这么中招的：拿到的是另一个 20 分钟视频的字幕，末段恰好 1255s）。

    退一步用「同一合集其他分P的语言分布」当参照：同一个老师讲同一门课的相邻分P，
    词表高度重叠（实测 0.57~0.84）；串台字幕会断崖式掉到 0.3 以下。
    """
    from src.bili_subtitle import text_affinity

    view = client.get_view(bvid)
    cid_of = {p["page"]: p["cid"] for p in view.get("pages", [])}
    title_of = {p["page"]: p.get("part", "") for p in view.get("pages", [])}

    texts: dict[int, str] = {}
    for pno in pages:
        cid = cid_of.get(pno)
        if not cid:
            continue
        path = client._cache_path(bvid, cid) if client.cache_dir else ""
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
            segs = j.get("segments") or []
            texts[pno] = j.get("raw_text") or "".join(s.get("text", "") for s in segs)
        except Exception:  # noqa: BLE001
            continue

    rows = []
    for pno, txt in texts.items():
        others = "".join(t for k, t in texts.items() if k != pno)
        aff = text_affinity(txt, others) if others else 1.0
        rows.append({"page": pno, "title": title_of.get(pno, ""), "chars": len(txt),
                     "affinity": round(aff, 3), "ok": aff >= min_affinity})
    return sorted(rows, key=lambda r: r["page"])


def _print_audit(rows: list[dict]) -> None:
    if not rows:
        return
    log.info("\n===== 合集内一致性（与其余分P的词汇亲和度，阈值 0.45）=====")
    log.info("%-4s %-24s %7s %7s %s", "P", "标题", "字数", "亲和度", "")
    bad = []
    for r in rows:
        flag = "" if r["ok"] else "  <-- 疑似串台/字幕不属于本P"
        log.info("P%-3d %-24s %7d %7.3f%s", r["page"], r["title"][:22], r["chars"],
                 r["affinity"], flag)
        if not r["ok"]:
            bad.append(r["page"])
    if bad:
        log.warning("建议重取这些分P：%s（--pages %s --force）",
                    ",".join(f"P{p}" for p in bad), ",".join(str(p) for p in bad))


def _run(args, page_no: int, out_override: str = "") -> dict:
    from src.bili_frames import (chapter_slots, contact_sheet, dedupe_frames,
                                 download_video, grab_frames, probe_duration)
    from src.bilibili_client import _sub_client

    client = _sub_client()
    m = re.search(r"(BV[0-9A-Za-z]{10})", args.target)
    if not m:
        raise SystemExit("无法从参数里解析出 BV 号")
    bvid = m.group(1)

    view = client.get_view(bvid)
    pages = view.get("pages") or []
    if not pages:
        raise SystemExit("该视频没有分P信息")
    page = next((p for p in pages if p.get("page") == page_no), None)
    if page is None:
        raise SystemExit(f"该视频没有第 {page_no} P（共 {len(pages)} P）")
    cid, duration = int(page["cid"]), int(page.get("duration") or 0)
    part = page.get("part") or (view.get("title") or bvid)
    title = (view.get("title") or bvid) + (f" P{page_no}" if len(pages) > 1 else "")
    log.info("目标：%s ｜ P%d ｜ %s ｜ 时长 %s", bvid, page_no, part, _mmss(duration))

    out_dir = out_override or os.path.join(NOTES_DIR, f"{bvid}_P{page_no:02d}")
    meta_path = os.path.join(out_dir, "meta.json")
    if args.skip_existing and os.path.exists(meta_path):
        old = json.load(open(meta_path, encoding="utf-8"))
        log.info("已有 meta.json，跳过：%s", out_dir)
        return {"page": page_no, "title": old.get("part", part), "chars": old.get("chars", 0),
                "chapters": old.get("chapters", 0), "frames": old.get("frames", 0),
                "cost": old.get("text_cost", 0), "skipped": True}
    os.makedirs(out_dir, exist_ok=True)

    # 1) 文本：字幕优先，转写兜底
    tr = get_transcript(client, bvid, cid, duration,
                        allow_whisper=not args.no_whisper, model=args.model, force=args.force)

    # 2) 分章
    if args.chapters:
        chapters = load_chapters(args.chapters)
        log.info("使用手工章节：%d 章", len(chapters))
    else:
        chapters = auto_chapters(tr["segments"], duration, target=args.auto_n)
        log.info("自动分章：%d 章（词汇衔接 + 话语标记）", len(chapters))
    for i, c in enumerate(chapters, 1):
        log.info("  %02d %-42s %s–%s", i, c["title"][:40], _mmss(c["start"]), _mmss(c["end"]))

    meta = {"bvid": bvid, "cid": cid, "page": page_no, "part": part, "title": title,
            "url": f"https://www.bilibili.com/video/{bvid}/?p={page_no}",
            "duration": duration, "text_source": tr["source"],
            "chars": tr["chars"], "text_cost": tr["cost"],
            "segments": len(tr["segments"]), "chapters": len(chapters)}

    # 3) 底稿
    tpath = write_transcript(chapters, tr["segments"], duration, meta,
                             os.path.join(out_dir, "transcript.md"))
    log.info("章节底稿：%s", tpath)

    # 4) 分批抽帧（字幕阶段已完成，这里才开始下视频）
    if not args.no_frames:
        # 不按分P加后缀：cid 本身就能唯一定位一条流，加了后缀反而会让缓存失效、重复下载
        vm = download_video(bvid, cid, max_qn=args.quality)
        real_dur = probe_duration(vm["path"])
        log.info("画面源：%s %s，%.1f MB，实际时长 %s", vm["qn_name"],
                 f"{vm['width']}x{vm['height']}", vm["bytes"] / 1048576, _mmss(real_dur))

        frames_dir = os.path.join(out_dir, "frames")
        built: dict[str, dict] = {}
        for i, c in enumerate(chapters, 1):
            end = min(c["end"], real_dur)
            if end - c["start"] < 2:
                continue
            slots = chapter_slots(c["start"], end, args.frames)
            raw = grab_frames(vm["path"], slots, os.path.join(frames_dir, f"c{i:02d}"))
            kept = dedupe_frames(raw)
            built[f"c{i:02d}"] = {"index": i, "title": c["title"], "start": c["start"],
                                  "end": end, "candidates": len(raw),
                                  "frames": [f.as_dict() for f in kept]}
            log.info("  [%d/%d] %s：%d 候选 -> %d 保留（已落盘）",
                     i, len(chapters), c["title"][:28], len(raw), len(kept))
            # 每章都刷一次盘：长视频中断后可以直接续跑
            with open(os.path.join(out_dir, "frames.json"), "w", encoding="utf-8") as f:
                json.dump(built, f, ensure_ascii=False, indent=2)

        # 5) 联系表：按章分组，各表格数尽量均衡（避免最后一张只剩一两格）
        items = []
        for key in sorted(built):
            ch = built[key]
            for n, fr in enumerate(ch["frames"], 1):
                items.append((f"{key[-2:]}-{n}", ch["title"], fr["ts"], fr["path"]))
        sheets = []
        if items:
            per_max = max(1, args.cols * 4)
            n_sheets = max(1, math.ceil(len(items) / per_max))
            per = math.ceil(len(items) / n_sheets)
            for gi in range(0, len(items), per):
                sp = contact_sheet(items[gi:gi + per], os.path.join(out_dir, "contacts",
                                                                    f"sheet_{gi // per + 1}.jpg"),
                                   cols=args.cols)
                sheets.append(sp)
        meta["frames"] = sum(len(v["frames"]) for v in built.values())
        meta["contacts"] = sheets
        log.info("联系表：%d 张（%s）", len(sheets), out_dir)

    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({**meta, "chapter_list": chapters}, f, ensure_ascii=False, indent=2)

    print(f"\n完成：{out_dir}")
    print(f"  文本 {tr['source']} {len(tr['segments'])} 段 / {tr['chars']} 字 / {tr['cost']}s"
          f" ｜ 章节 {len(chapters)}")
    if not args.no_frames:
        print(f"  候选帧 {meta.get('frames', 0)} 张 ｜ 联系表 {len(meta.get('contacts', []))} 张")
    print("  下一步：看 contacts/ 挑帧 -> 写 note.md（只许从 transcript.md 取事实）")
    return {"page": page_no, "part": part, "title": part, "chars": tr["chars"],
            "chapters": len(chapters), "frames": meta.get("frames", 0),
            "cost": tr["cost"], "source": tr["source"]}


if __name__ == "__main__":
    raise SystemExit(main())
