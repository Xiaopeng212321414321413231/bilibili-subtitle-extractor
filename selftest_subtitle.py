# -*- coding: utf-8 -*-
"""bili_subtitle 自检（不依赖登录态，离线可跑）。

用法：
    python selftest_subtitle.py            # 离线逻辑测试
    python selftest_subtitle.py --live BV1xxxx   # 额外做一次真实网络探测

全部使用临时目录，不写 extracted/，不动既有数据。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "src"))

import bili_subtitle as B  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


# --------------------------------------------------------------------------- #
def t_mixin_key() -> None:
    print("\n[1] WBI mixin key（对照官方文档示例）")
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    mixin = B.mixin_key_from(img, sub)
    check("mixin key = ea1db124af3c7062474693fa704f4ff8", mixin == "ea1db124af3c7062474693fa704f4ff8", mixin)

    # 注入已知 mixin，避免签名过程联网取 nav
    c = B.BiliSubtitleClient()
    c._mixin_key = mixin
    c._mixin_at = time.time()
    p = c._signed_params({"aid": 123, "cid": 456})
    check("签名含 wts/w_rid", "wts" in p and len(p["w_rid"]) == 32)
    check("非法字符被清理", "!" not in c._signed_params({"q": "a!b'c(d)"})["q"])
    check("签名可复现（同参数同 w_rid）",
          c._signed_params({"aid": 1})["w_rid"] == c._signed_params({"aid": 1})["w_rid"])


def t_pick_track() -> None:
    print("\n[2] 轨道优先级：中文人工 > 中文AI > 其他人工 > 其他AI")
    tracks = [
        {"lan": "en-US", "lan_doc": "英文（美国）"},
        {"lan": "ai-en", "lan_doc": "英文（自动生成）"},
        {"lan": "ai-zh", "lan_doc": "中文（自动生成）"},
        {"lan": "zh-CN", "lan_doc": "中文（中国）"},
    ]
    best = B.BiliSubtitleClient.pick_track(tracks)
    check("选中 zh-CN", best and best["lan"] == "zh-CN", str(best))
    only_ai = [t for t in tracks if t["lan"].startswith("ai")]
    check("无人工时选 ai-zh", B.BiliSubtitleClient.pick_track(only_ai)["lan"] == "ai-zh")
    check("空列表返回 None", B.BiliSubtitleClient.pick_track([]) is None)
    check("仅英文时退化选 en", B.BiliSubtitleClient.pick_track([tracks[0]])["lan"] == "en-US")


def t_text() -> None:
    print("\n[3] 文本处理：去重、合段、SRT")
    lines = ["我们来看这个表", "我们来看这个表", "这里要注意，", "因为样本量不足。"]
    dedup = B._dedupe_lines(lines)
    check("连续重复行被去掉", len(dedup) == 3, str(dedup))
    para = B._paragraphize(dedup, max_len=80)
    check("中文行间不加空格", "我们来看这个表这里要注意，" in para.replace("\n\n", ""), para)
    check("未到句末不合段", para.count("\n\n") == 0, repr(para))
    para2 = B._paragraphize(["第一句。", "第二句。"])
    check("句末标点断段", para2.count("\n\n") == 1, repr(para2))
    para3 = B._paragraphize(["一" * 50, "二" * 50, "三" * 50], max_len=60)
    check("超长强制断段", para3.count("\n\n") == 1, repr(para3)[:60])

    segs = [{"from": 0.0, "to": 2.5, "text": "第一句"}, {"from": 2.5, "to": 65.25, "text": "第二句"}]
    srt = B.segments_to_srt(segs)
    check("SRT 时间格式", "00:00:00,000 --> 00:00:02,500" in srt, srt.splitlines()[1])
    check("SRT 序号递增", srt.startswith("1\n") and "\n2\n" in srt)


def t_url() -> None:
    print("\n[4] 字幕 URL 规范化")
    n = B._norm_url
    check("// 开头补 https", n("//aisubtitle.hdslb.com/a.json") == "https://aisubtitle.hdslb.com/a.json")
    check("裸域名补 https", n("aisubtitle.hdslb.com/a.json").startswith("https://"))
    check("http 原样保留", n("http://x/a.json") == "http://x/a.json")
    check("空值返回空", n("") == "")


class _Fake(B.BiliSubtitleClient):
    """把网络层替换成假实现，验证 fetch 的编排逻辑。"""

    def __init__(self, tracks, body, ai_url="", duration=5, track_exc=None, summary="", **kw):  # noqa: D107
        super().__init__(**kw)
        self._tracks = tracks
        self._body = body
        self._ai_url = ai_url
        self._duration = duration
        self._track_exc = track_exc
        self._summary = summary
        self.calls = []

    def get_summary(self, bvid, cid, up_mid=0):  # 自测绝不联网
        return self._summary

    def list_tracks(self, aid, cid, bvid="", raise_on_error=False):
        self.calls.append(("tracks", cid))
        if self._track_exc is not None and raise_on_error:
            raise self._track_exc
        return self._tracks

    def get_view(self, bvid="", aid=0):  # 自测绝不联网
        self.calls.append(("view", bvid))
        return {
            "aid": aid or 1,
            "bvid": bvid,
            "pages": [{"page": 1, "cid": 111, "duration": self._duration, "part": "P1", "title": "P1"}],
        }

    def _resolve_url(self, track, aid, cid):
        self.calls.append(("resolve", track.get("lan")))
        if track.get("subtitle_url"):
            return B._norm_url(track["subtitle_url"])
        return self._ai_url

    def _download_body(self, url):
        self.calls.append(("download", url))
        return self._body


def t_fetch(tmp: str) -> None:
    print("\n[5] fetch 编排：AI 字幕二次取 URL + 缓存")
    body = [{"from": i, "to": i + 1, "text": f"第{i}句字幕"} for i in range(5)]
    tracks = [{"lan": "ai-zh", "lan_doc": "中文（自动生成）", "subtitle_url": ""}]
    c = _Fake(tracks, body, ai_url="https://aisubtitle.hdslb.com/x.json", cache_dir=tmp)
    r = c.fetch("BVtest", cid=111, aid=1)
    check("AI 轨道 url 为空时走二次解析", r.get("available") is True and ("resolve", "ai-zh") in c.calls)
    check("kind 标记为 ai", r.get("kind") == "ai")
    check("分段数正确", len(r["segments"]) == 5)
    check("正文字符统计", r["chars"] == sum(len(s["text"]) for s in body))
    check("缓存已落盘", os.path.exists(os.path.join(tmp, "BVtest_111.json")))

    c2 = _Fake(tracks, body, ai_url="https://aisubtitle.hdslb.com/x.json", cache_dir=tmp)
    r2 = c2.fetch("BVtest", cid=111, aid=1, duration=5)
    check("二次命中缓存且不发请求", r2.get("cached") is True and not c2.calls, str(c2.calls))

    c3 = _Fake([], body, cache_dir=tmp)
    r3 = c3.fetch("BVnone", cid=9, aid=1)
    check("无轨道时 available=False", r3["available"] is False and r3["reason"] == "no_track", str(r3))

    c4 = _Fake(tracks, [], ai_url="", cache_dir=tmp)
    r4 = c4.fetch("BVempty", cid=9, aid=1)
    check("body 为空时不报错、判失败", r4["available"] is False and r4["reason"] == "fetch_failed", str(r4))

    # ---- 覆盖度校验：字幕只覆盖视频开头一小段时必须弃用 ----
    # 真实案例：BV1hbM9zrEwD P02 是 3680 秒，B站返回的字幕只到 298 秒，
    # 内容完全是另一个视频的。宁可回落到转写，也不能输出错文本。
    short_body = [{"from": i, "to": i + 1, "text": f"第{i}句"} for i in range(60)]
    c5 = _Fake(tracks, short_body, ai_url="https://a/x.json", duration=600, cache_dir=tmp)
    r5 = c5.fetch("BVshort", cid=111, aid=1)
    check("覆盖不足判定不可用", r5["available"] is False, str(r5)[:120])
    check("失败原因标为 coverage_bad", r5.get("reason") == "coverage_bad", str(r5.get("reason")))
    check("可疑字幕不写缓存", not os.path.exists(os.path.join(tmp, "BVshort_111.json")))

    # 反方向：字幕比视频还长（真实案例 BV1yNxM6wEiP，713s 的视频给回 1203s 的字幕 = 168%）
    over_body = [{"from": i, "to": i + 1, "text": f"第{i}句"} for i in range(1000)]
    c5b = _Fake(tracks, over_body, ai_url="https://a/x.json", duration=600, cache_dir=tmp)
    r5b = c5b.fetch("BVover", cid=111, aid=1)
    check("覆盖超出判定不可用", r5b["available"] is False and r5b.get("reason") == "coverage_bad", str(r5b)[:120])
    check("覆盖超出不写缓存", not os.path.exists(os.path.join(tmp, "BVover_111.json")))

    # 覆盖充分（末段 99%）应正常通过
    full_body = [{"from": i, "to": i + 1, "text": f"第{i}句"} for i in range(600)]
    c6 = _Fake(tracks, full_body, ai_url="https://a/x.json", duration=600, cache_dir=tmp)
    r6 = c6.fetch("BVfull", cid=111, aid=1)
    check("覆盖充分正常可用", r6["available"] is True and len(r6["segments"]) == 600)

    # 坏缓存（历史上写进去的短字幕）应被识别并清掉后重取
    bad_path = os.path.join(tmp, "BVstale_111.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        json.dump({"available": True, "segments": short_body, "text": "x"}, f)
    c7 = _Fake(tracks, full_body, ai_url="https://a/x.json", duration=600, cache_dir=tmp)
    r7 = c7.fetch("BVstale", cid=111, aid=1)
    check("坏缓存被丢弃并重取", r7["available"] is True and len(r7["segments"]) == 600, str(r7)[:100])

    # 轨道查询报错时不能伪装成「该分P无字幕」（会误导去重新扫码）
    c8 = _Fake([], body, cache_dir=tmp, track_exc=RuntimeError("请求失败 x/player/v2: HTTP 404"))
    r8 = c8.fetch("BVerr", cid=9, aid=1)
    check("轨道查询失败报 track_error", r8.get("reason") == "track_error", str(r8)[:120])

    # cid 漏传（=0）必须兜底成第一个分P，不能拿 cid=0 去查轨道
    c9 = _Fake(tracks, body, ai_url="https://a/x.json", duration=5, cache_dir=tmp)
    r9 = c9.fetch("BVnocid", aid=1)
    check("cid=0 兜底为首页 cid", ("tracks", 111) in c9.calls, str(c9.calls[:3]))
    check("cid 兜底后能正常取字幕", r9.get("available") is True, str(r9)[:100])

    # ---- 内容交叉校验：抓「长度相仿但讲的是别的视频」的串台 ----
    # 真实案例：BV1UWgA6SEwu 是《从 LLM 到 Agent》，却拿回香港电影解说字幕，
    # 覆盖度 97.8% 完全过关，只有和「该视频 AI 总结」比对词汇才能发现。
    print("\n[5c] 内容交叉校验（字幕 vs 视频AI总结）")
    summ = "从LLM到Agent的AI发展脉络，涵盖Token化处理、RAG、Function Calling与Sub-agent等核心技术"
    same = "从 LLM 到 Agent，先讲 Token 化处理，再讲 RAG 检索增强和 Function Calling 工具调用"
    other = "这是一部根据真实事件改编的电影，票房只停留在一千多万，却拿到金像奖十六项提名"
    check("同主题文本亲和度高", B.text_affinity(same, summ) > 0.30, f"{B.text_affinity(same, summ):.3f}")
    check("无关文本亲和度低", B.text_affinity(other, summ) < 0.10, f"{B.text_affinity(other, summ):.3f}")
    check("空文本不误伤", B.text_affinity("", summ) == 1.0)

    film_segs = [{"from": i, "to": i + 1, "text": other[i * 4 : i * 4 + 20] or "电影"} for i in range(600)]
    c10 = _Fake(tracks, film_segs, ai_url="https://a/x.json", duration=600, summary=summ, cache_dir=tmp)
    r10 = c10.fetch("BVfilm", cid=111, aid=1)
    check("串台字幕判为 content_suspect", r10.get("reason") == "content_suspect", str(r10)[:140])
    check("串台字幕不写缓存", not os.path.exists(os.path.join(tmp, "BVfilm_111.json")))

    good_segs = [{"from": i, "to": i + 1, "text": same} for i in range(600)]
    c11 = _Fake(tracks, good_segs, ai_url="https://a/x.json", duration=600, summary=summ, cache_dir=tmp)
    r11 = c11.fetch("BVgood", cid=111, aid=1)
    check("内容匹配时正常通过", r11.get("available") is True, str(r11)[:120])
    check("结果带 affinity 字段", isinstance(r11.get("affinity"), float), str(r11.get("affinity")))


def t_parse_pages() -> None:
    print("\n[6] 分P解析")
    import extract_bilibili as E  # noqa: PLC0415

    check("1-3,5 → [1,2,3,5]", E.parse_pages("1-3,5") == [1, 2, 3, 5])
    check("中文逗号兼容", E.parse_pages("1，2") == [1, 2])
    check("空串 → []", E.parse_pages("") == [])
    check("非法输入被忽略", E.parse_pages("a,b-2") == [] or E.parse_pages("a,b-2") == [2])
    check("笔记目录名带分P号", E.note_dir_name("BV1", 1) == "BV1_P01")
    check("笔记目录名补零到两位", E.note_dir_name("BV1abc", 12) == "BV1abc_P12")


def t_live(bvid: str) -> None:
    print(f"\n[7] 真实网络探测 {bvid}")
    c = B.BiliSubtitleClient.from_env(os.path.join(BASE, ".env"))
    ok, who = c.login_check()
    print(f"  登录态: {'✓' if ok else '✗'} {who}")
    v = c.get_view(bvid)
    print(f"  视频: {v['title'][:36]} | 分P:{len(v['pages'])} | 赞:{v['stat']['like']}")
    with tempfile.TemporaryDirectory() as tmp:
        c.cache_dir = tmp
        r = c.fetch(bvid, cid=v["pages"][0]["cid"], aid=v["aid"])
    if r.get("available"):
        print(f"  字幕: ✓ {r['kind']} {r.get('lang_doc')} {r['chars']} 字")
        print("  " + (r["text"][:120].replace("\n", " / ")))
    else:
        print(f"  字幕: ✗ {r.get('message')}")
        if not ok:
            print("  → 符合预期：无登录态时 AI 字幕不可见，需刷新 SESSDATA")
    check("view 能拿到 aid/cid", v["aid"] > 0 and v["pages"][0]["cid"] > 0)


def t_download_body() -> None:
    """起一个本地 HTTP 服务，用真实 B站字幕 JSON 结构验证下载 + 解析。"""
    print("\n[5b] 字幕 JSON 下载与解析（本地 HTTP）")
    import http.server
    import threading

    payload = json.dumps(
        {
            "font_size": 0.4,
            "font_color": "#FFFFFF",
            "background_alpha": 0.5,
            "body": [
                {"from": 0.0, "to": 3.12, "sid": 1, "content": "大家好，今天讲回归分析。"},
                {"from": 3.12, "to": 6.4, "sid": 2, "content": "大家好，今天讲回归分析。"},
                {"from": 6.4, "to": 9.0, "sid": 3, "content": "先看这个散点图"},
                {"from": 9.0, "to": 11.5, "sid": 4, "content": ""},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # 静默
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/sub.json"
    try:
        c = B.BiliSubtitleClient()
        segs = c._download_body(url)
        check("解析出 3 段（空 content 被丢）", len(segs) == 3, str(len(segs)))
        check("from/to 正确", segs[0]["from"] == 0.0 and abs(segs[1]["to"] - 6.4) < 1e-6)
        check("重复内容保留在原始分段", segs[0]["text"] == segs[1]["text"])
    finally:
        srv.shutdown()


def t_auth(tmp: str) -> None:
    """一次性会话：保存/读取/过期自动焚毁/覆写删除/凭证注入。"""
    print("\n[7] 扫码会话（一次性凭证）")
    import bili_auth as A

    # 指向临时目录，绝不碰真实会话文件
    A.SESSION_DIR = tmp
    A.SESSION_FILE = os.path.join(tmp, "session.json")
    A.PENDING_FILE = os.path.join(tmp, "pending.json")
    A.PENDING_REVOKE_FILE = os.path.join(tmp, "pending_revoke.json")

    A.wipe_session()
    check("初始无会话", A.load_session() is None)

    creds = {"sessdata": "fake-sess", "bili_jct": "fake-jct", "dedeuserid": "123", "buvid3": "b3"}
    A.save_session(creds, ttl=60)
    got = A.load_session()
    check("保存后可读回", got and got["sessdata"] == "fake-sess")
    check("TTL 约 60 秒", 55 <= A.remaining_ttl() <= 60, str(A.remaining_ttl()))

    A.save_session(creds, ttl=60)
    saved = A.load_session() or {}
    check("会话含 expires 整数字段", isinstance(saved.get("expires"), int))
    # 回归：曾把 expires/created 当 cookie 塞给 requests，触发
    # 'int' object has no attribute 'startswith'
    try:
        A._session(saved)
        check("整数字段不会被误当 cookie", True)
    except Exception as e:  # noqa: BLE001
        check("整数字段不会被误当 cookie", False, str(e)[:60])

    A.save_session(creds, ttl=-1)  # 已过期
    check("过期会话读不到", A.load_session() is None)
    check("过期文件被当场焚毁", not os.path.exists(A.SESSION_FILE))

    A.save_session(creds, ttl=60)
    check("wipe 返回已删除", A.wipe_session() is True)
    check("wipe 后文件消失", not os.path.exists(A.SESSION_FILE))

    # 注销失败的待办必须能在 wipe 之后存活，否则凭证永远注销不掉
    A._save_pending_revoke(creds)
    pend_ok = os.path.exists(A.PENDING_REVOKE_FILE)
    A.wipe_session()
    check("待注销记录写入", pend_ok)
    check("wipe 不清除待注销记录", os.path.exists(A.PENDING_REVOKE_FILE))
    A._shred(A.PENDING_REVOKE_FILE)

    # 凭证注入到 requests 会话
    c = B.BiliSubtitleClient()
    c.apply_credentials(creds)
    jar = {k.name: k.value for k in c.s.cookies}
    check("SESSDATA 注入正确", jar.get("SESSDATA") == "fake-sess", str(jar.keys()))
    check("DedeUserID 键名正确", jar.get("DedeUserID") == "123")
    check("has_login 置位", c.has_login is True)
    c.apply_credentials({})
    check("清空凭证后 has_login=False", c.has_login is False)

    # 二维码 URL 渲染（不联网，用假 url）
    try:
        p = A.render_qr("https://example.com/test", os.path.join(tmp, "qr.png"))
        check("二维码 PNG 生成", os.path.exists(p) and os.path.getsize(p) > 500)
    except ImportError:
        check("二维码库缺失（跳过）", True)


def t_zh() -> None:
    """繁简转换：whisper 默认吐繁体，必须压成简体。"""
    print("\n[6] 繁简转换")
    sys.path.insert(0, os.path.join(BASE, "src"))
    from zh_convert import backend, to_simplified

    trad = "同學你好。通過前一週的學習，市場研究中獲取二手數據。"
    out = to_simplified(trad)
    check("繁体转简体", out == "同学你好。通过前一周的学习，市场研究中获取二手数据。", out)
    check("简体保持不变", to_simplified("同学你好，这是简体中文") == "同学你好，这是简体中文")
    check("英文不受影响", to_simplified("hello world, fine") == "hello world, fine")
    check("空串安全", to_simplified("") == "")
    check("转换后端可用", backend() in ("opencc", "builtin"), backend())
    check("无繁体时不改动标点", to_simplified("好，那我们开始。") == "好，那我们开始。")


def t_audio(path: str) -> None:
    """可选：用一段本地音频验证 whisper 降级链路（不联网）。"""
    print(f"\n[8] 转写链路实测：{path}")
    import extract_bilibili as E

    if not os.path.exists(path):
        check("音频文件存在", False, path)
        return
    res = E.transcribe_batch([("A", path, "zh")], "base", 1)
    text = res.get("A", "")
    from zh_convert import T2S

    leftover = [ch for ch in text if ch in T2S]
    check("转写出非空文本", len(text) > 10, f"{len(text)} 字")
    check("输出为简体（无残留繁体）", not leftover, "".join(leftover[:10]))
    print("  样例:", text[:80])


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 64)
    print("bili_subtitle 自检")
    print("=" * 64)
    t_mixin_key()
    t_pick_track()
    t_text()
    t_url()
    t_download_body()
    with tempfile.TemporaryDirectory() as tmp:
        t_fetch(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        t_auth(tmp)
    t_parse_pages()
    t_zh()
    if "--audio" in sys.argv:
        i = sys.argv.index("--audio")
        t_audio(sys.argv[i + 1] if len(sys.argv) > i + 1 else "")
    if "--live" in sys.argv:
        i = sys.argv.index("--live")
        t_live(sys.argv[i + 1] if len(sys.argv) > i + 1 else "BV1GJ411x7h7")

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
