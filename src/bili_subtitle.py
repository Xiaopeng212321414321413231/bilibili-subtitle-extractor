# -*- coding: utf-8 -*-
"""B站字幕直取模块（v2）。

设计目标：能把字幕拿到就绝不下载音频。
    player/wbi/v2（WBI 签名）→ 取字幕轨道
    → 轨道 subtitle_url 为空且是 AI 字幕时，走 ai/subtitle/search/stat 二次取 URL
    → 下载字幕 JSON → 转成分段 / SRT / 可读正文

关键改进（相对 v1 的 info["subtitle"]["list"]）：
  1. view 接口基本不返回字幕列表，必须按 cid 查 player 接口；
  2. 新版 AI 字幕在 player 里 url 为空，必须补一次 ai/subtitle/search/stat；
  3. player/wbi/v2 需 w_rid 签名（img_key+sub_key），否则偶发 -352 / 空字幕；
  4. 结果落盘缓存，同一 cid 第二次 0 请求；
  5. 并发探测多分P，避免 60P 课程串行等 1 分钟。

对外接口：
    client = BiliSubtitleClient.from_env()
    view   = client.get_view("BV1xx")            # 元信息 + pages
    result = client.fetch("BV1xx", cid=123)      # 单分P字幕（含缓存）
    rows   = client.probe("BV1xx")               # 全部分P字幕可用性探测
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger("bili_sub")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# WBI 混淆表（与 web 端一致）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_ILLEGAL = re.compile(r"[!'()*]")

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYER_WBI = "https://api.bilibili.com/x/player/wbi/v2"
API_PLAYER = "https://api.bilibili.com/x/player/v2"
API_AI_STAT = "https://api.bilibili.com/x/player/v2/ai/subtitle/search/stat"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
# 该视频的 AI 总结：用来交叉校验字幕内容是不是真的属于这个视频
API_CONCLUSION = "https://api.bilibili.com/x/web-interface/view/conclusion/get"

# 充电专属 / 付费视频：字幕与音频都拿不到，直接判死
_CHARGE_FLAGS = ("is_upower_exclusive", "is_upower_play")


class RiskyBanned(RuntimeError):
    """HTTP 412 / -412：被风控，重试无意义，应立即放弃该接口。"""


class _FatalHttp(RuntimeError):
    """4xx（400/401/403/404/410）：请求本身有问题，重试无意义。"""


# --------------------------------------------------------------------------- #
# 内容一致性校验
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> set[str]:
    """粗粒度切词：英文/数字单词 + 中文二元字组合（不依赖分词库）。"""
    t = (text or "").lower()
    toks = set(re.findall(r"[a-z][a-z0-9_]{2,}", t))
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", t)
    toks |= {cjk[i : i + 2] for i in range(len(cjk) - 1)}
    return toks


def text_affinity(a: str, b: str) -> float:
    """两段文本的词汇重合度（0~1），用较短一方做分母。

    用来比对「字幕正文」和「B站 给这个视频的 AI 总结」：两者都源自同一个
    视频，正常重合度很高；若拿到的是别的视频的字幕，会低到接近 0。
    实测：正确样本 0.35 / 0.38，串台的电影解说字幕 0.04。
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 1.0  # 无法判断时不误伤
    return len(ta & tb) / min(len(ta), len(tb))


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def mixin_key_from(img_key: str, sub_key: str) -> str:
    """由 img_key + sub_key 推出 mixin key（纯函数，便于离线校验）。"""
    raw = (img_key or "") + (sub_key or "")
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


def _srt_time(sec: float) -> str:
    if sec < 0:
        sec = 0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        return "https://" + url
    return url


def _dedupe_lines(lines: Iterable[str]) -> list[str]:
    """去掉 AI 字幕常见的「连续重复行」。"""
    out: list[str] = []
    for line in lines:
        t = (line or "").strip()
        if not t:
            continue
        if out and t == out[-1]:
            continue
        out.append(t)
    return out


def _paragraphize(lines: Iterable[str], max_len: int = 80) -> str:
    """把逐行字幕合并成可读段落：遇句末标点或长度到阈值就断行。"""
    paras: list[str] = []
    buf = ""
    for line in lines:
        t = (line or "").strip()
        if not t:
            continue
        if not buf:
            buf = t
        elif re.search(r"[A-Za-z0-9]$", buf) and re.match(r"^[A-Za-z0-9]", t):
            buf += " " + t  # 英文词间补空格
        else:
            buf += t
        if len(buf) >= max_len or re.search(r"[。！？!?；;…]$", t):
            paras.append(buf)
            buf = ""
    if buf:
        paras.append(buf)
    return "\n\n".join(paras)


def segments_to_srt(segments: list[dict]) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        blocks.append(
            f"{i}\n{_srt_time(seg['from'])} --> {_srt_time(seg['to'])}\n{seg['text']}\n"
        )
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class BiliSubtitleClient:
    """按 cid 直取字幕。所有网络调用带重试与可选缓存。"""

    def __init__(
        self,
        sessdata: str = "",
        bili_jct: str = "",
        dedeuserid: str = "",
        cache_dir: Optional[str] = None,
        timeout: int = 15,
        retries: int = 4,
        use_cache: bool = True,
    ):
        self.timeout = timeout
        self.retries = retries
        self.use_cache = use_cache
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )
        # 代理客户端换端口后 env 里的 HTTPS_PROXY 会指向死端口，先探一下
        _proxy_ok = True
        try:
            try:
                from .bili_auth import proxy_reachable
            except ImportError:
                from bili_auth import proxy_reachable
            _proxy_ok = proxy_reachable()
        except Exception:  # noqa: BLE001
            _proxy_ok = True
        if not _proxy_ok:
            log.info("环境代理端口不可用，本次全部直连")
        self._proxy_ok = _proxy_ok
        # 直连专用 session（trust_env=False），与主 session 彻底隔离。
        # 早期版本靠临时改 self.s.trust_env 切链路，但多个线程共用一个
        # session 时会互相踩：A 线程刚改成直连，B 线程的请求就跟着变了。
        self._direct: Optional[requests.Session] = None
        self._use_direct = not _proxy_ok
        if sessdata:
            self.s.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        if bili_jct:
            self.s.cookies.set("bili_jct", bili_jct, domain=".bilibili.com")
        if dedeuserid:
            self.s.cookies.set("DedeUserID", dedeuserid, domain=".bilibili.com")
        self.has_login = bool(sessdata)

        self._mixin_key: Optional[str] = None
        self._mixin_at = 0.0
        self._wbi_dead = False
        self._view_cache: dict[str, dict] = {}
        self._summary_cache: dict[str, str] = {}

    def _session(self, direct: Optional[bool] = None) -> requests.Session:
        """取用于发请求的 session。direct=True 时走直连（忽略环境代理）。"""
        if direct is None:
            direct = self._use_direct
        if not direct:
            return self.s
        if self._direct is None:
            sd = requests.Session()
            sd.trust_env = False  # 只此一处，之后永不改动
            sd.headers.update(self.s.headers)
            self._direct = sd
        # cookies 可能在构造后通过 apply_credentials 注入，这里每次同步一次
        self._direct.cookies.update(self.s.cookies)
        return self._direct

    # ---------------- 构造 ----------------
    def apply_credentials(self, creds: dict) -> None:
        """注入一份凭证（扫码登录/一次性会话用）。

        creds 键名用小写：sessdata / bili_jct / dedeuserid / buvid3 / buvid4。
        """
        mapping = {
            "sessdata": "SESSDATA",
            "bili_jct": "bili_jct",
            "dedeuserid": "DedeUserID",
            "buvid3": "buvid3",
            "buvid4": "buvid4",
        }
        for low, real in mapping.items():
            val = (creds or {}).get(low)
            if val:
                self.s.cookies.set(real, val, domain=".bilibili.com")
        self.has_login = bool((creds or {}).get("sessdata"))
        self._view_cache.clear()

    @classmethod
    def from_env(cls, env_file: Optional[str] = None, **kw) -> "BiliSubtitleClient":
        """从 .env 读取 BILIBILI_SESSDATA / BILIBILI_BILI_JCT / BILIBILI_DEDE_USER_ID。"""
        env: dict[str, str] = {}
        if env_file and os.path.exists(env_file):
            try:
                from dotenv import dotenv_values  # type: ignore

                env = {k: v for k, v in dotenv_values(env_file).items() if v}
            except Exception:  # dotenv 缺失时退回手工解析
                with open(env_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip('"').strip("'")
        get = lambda k: env.get(k) or os.environ.get(k, "")
        return cls(
            sessdata=get("BILIBILI_SESSDATA"),
            bili_jct=get("BILIBILI_BILI_JCT"),
            dedeuserid=get("BILIBILI_DEDE_USER_ID"),
            **kw,
        )

    # ---------------- 底层请求 ----------------
    def _mixin(self) -> str:
        if self._mixin_key and time.time() - self._mixin_at < 600:
            return self._mixin_key
        # nav 未登录会返回 -101，但 data.wbi_img 依然存在
        d = self._request(API_NAV, {}, signed=False)
        wbi = (d.get("data") or {}).get("wbi_img") or {}
        img_key = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("获取 WBI 密钥失败（nav 接口无 wbi_img）")
        self._mixin_key = mixin_key_from(img_key, sub_key)
        self._mixin_at = time.time()
        return self._mixin_key

    def _signed_params(self, params: dict) -> dict:
        """返回带 wts / w_rid 的参数字典（值已做非法字符清理）。"""
        p = {k: _ILLEGAL.sub("", str(v)) for k, v in params.items()}
        p["wts"] = str(int(time.time()))
        query = urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((query + self._mixin()).encode()).hexdigest()
        return p

    def _request(self, url: str, params: dict, signed: bool = False) -> dict:
        """返回完整响应体 dict（不抛业务码异常，由调用方判断 code）。

        412 视为硬拦截：不重试，直接抛 RiskyBanned，避免每页白等十几秒。
        代理不可用时（本机代理常抖动）自动切直连再试一轮。
        """
        last: Any = None
        # 第一轮：代理 → 直连；第二轮：网络恢复后再把两条链路各试一遍。
        # 本机代理端口会轮换，瞬时 getaddrinfo/ProxyError 很常见。
        modes = ("env", "direct") if self._proxy_ok else ("direct",)
        for wave in range(2):
            if wave:
                log.info("网络异常，3 秒后整链路重试一轮（上次：%s）", str(last)[:120])
                time.sleep(3)
                tries = 2
            else:
                tries = self.retries
            for mode in modes:
                direct = mode == "direct"
                sess = self._session(direct=direct)
                for attempt in range(tries):
                    try:
                        p = self._signed_params(params) if signed else dict(params)
                        r = sess.get(url, params=p, timeout=self.timeout)
                        if r.status_code == 412:
                            raise RiskyBanned(f"HTTP 412 风控拦截: {url.rsplit('/', 1)[-1]}")
                        if r.status_code in (400, 401, 403, 404, 410):
                            # 4xx 是请求本身的问题，重试只会白等：早期 404 被当可重试，
                            # 一次抓取在 4×2×2 次退避里空转了 70 秒。
                            raise _FatalHttp(f"HTTP {r.status_code}")
                        if r.status_code == 429:
                            raise RuntimeError("HTTP 429 限流")
                        if r.status_code != 200:
                            raise RuntimeError(f"HTTP {r.status_code}")
                        data = r.json()
                        if isinstance(data, dict) and data.get("code") == -412:
                            raise RiskyBanned(f"code -412 风控拦截: {url.rsplit('/', 1)[-1]}")
                        self._use_direct = direct  # 记住哪条链路通，供字幕文件下载复用
                        return data
                    except (RiskyBanned, _FatalHttp):
                        raise
                    except Exception as e:  # noqa: BLE001
                        last = e
                        wait = min(2 ** attempt, 8) + random.uniform(0, 0.6)
                        log.debug("%s 第%d次失败(%s)，%.1fs 后重试",
                                  url.rsplit("/", 1)[-1], attempt + 1, e, wait)
                        time.sleep(wait)
        raise RuntimeError(f"请求失败 {url}: {last}")

    # ---------------- 元信息 ----------------
    def get_view(self, bvid: str = "", aid: int = 0) -> dict:
        key = bvid or f"av{aid}"
        if key in self._view_cache:
            return self._view_cache[key]

        d = self._request(API_VIEW, {"bvid": bvid} if bvid else {"aid": aid})
        if d.get("code") != 0 or not d.get("data"):
            raise RuntimeError(f"view 接口失败: {d.get('code')} {d.get('message')}")
        data = d["data"]
        view = data.get("View") or data  # 部分版本嵌一层 View

        pages = [
            {
                "page": p.get("page", i + 1),
                "part": p.get("part", ""),
                "cid": p.get("cid", 0),
                "duration": p.get("duration", 0),
            }
            for i, p in enumerate(data.get("pages") or [])
        ]
        if not pages and data.get("cid"):
            pages = [{"page": 1, "part": data.get("title", ""), "cid": data["cid"], "duration": data.get("duration", 0)}]

        stat = view.get("stat") or {}
        info = {
            "bvid": view.get("bvid") or bvid,
            "aid": view.get("aid") or aid,
            "title": view.get("title", ""),
            "owner": (view.get("owner") or {}).get("name", ""),
            "owner_mid": (view.get("owner") or {}).get("mid", 0),
            "pubdate": view.get("pubdate") or view.get("ctime") or 0,
            "duration": view.get("duration", 0),
            "desc": (view.get("desc") or "")[:800],
            "stat": {
                "view": stat.get("view", 0),
                "like": stat.get("like", 0),
                "coin": stat.get("coin", 0),
                "favorite": stat.get("favorite", 0),
            },
            "charge_exclusive": any(bool(view.get(f)) for f in _CHARGE_FLAGS),
            "pages": pages,
        }
        self._view_cache[key] = info
        return info

    # ---------------- 登录态自检 ----------------
    def login_check(self) -> tuple[bool, str]:
        """返回 (是否已登录, 用户名或原因)。SESSDATA 过期时返回 False。"""
        try:
            d = self._request(API_NAV, {})
        except RiskyBanned:
            return False, "B站风控：当前出口 IP 被临时限制（HTTP 412），请等 30~60 分钟或换网络后重试"
        except Exception as e:  # noqa: BLE001
            return False, f"nav 请求失败: {e}"
        if d.get("code") == 0:
            return True, ((d.get("data") or {}).get("uname") or "已登录")
        if d.get("code") == -101:
            return False, "SESSDATA 缺失或已过期（B站 AI 字幕需要登录态）"
        return False, f"nav code={d.get('code')} {d.get('message')}"

    # ---------------- 内容交叉校验用的「视频 AI 总结」 ----------------
    def get_summary(self, bvid: str, cid: int, up_mid: int = 0) -> str:
        """取该视频的 AI 总结（WBI 签名，免额外权限）。失败返回空串，不阻断主流程。"""
        key = f"{bvid}_{cid}"
        if key in self._summary_cache:
            return self._summary_cache[key]
        text = ""
        try:
            if not up_mid:
                up_mid = int(self.get_view(bvid).get("owner_mid") or 0)
            d = self._request(API_CONCLUSION, {"bvid": bvid, "cid": cid, "up_mid": up_mid}, signed=True)
            if d.get("code") == 0:
                text = str(((d.get("data") or {}).get("model_result") or {}).get("summary") or "").strip()
            else:
                log.debug("AI 总结 code=%s %s", d.get("code"), d.get("message"))
        except Exception as e:  # noqa: BLE001
            log.debug("AI 总结获取失败（跳过内容校验）: %s", e)
            text = ""
        self._summary_cache[key] = text
        return text

    # ---------------- 字幕轨道 ----------------
    @staticmethod
    def _parse_tracks(resp: dict) -> list[dict]:
        tracks = (((resp.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
        return [t for t in tracks if isinstance(t, dict)]

    def list_tracks(self, aid: int, cid: int, bvid: str = "", raise_on_error: bool = False) -> list[dict]:
        """返回该 cid 的字幕轨道列表（可能为空）。

        顺序：老接口 x/player/v2（无需指纹，最稳）→ WBI 版（少数环境才需要）。
        WBI 版在部分网络会被 -412 拦截，失败即静默跳过，不影响主流程。

        ``raise_on_error=True`` 时把「请求挂了」以异常抛出，用于区分
        「真的没字幕」和「网络/接口异常」——早期版本靠实例变量
        ``last_track_error`` 传递这个信息，而 probe/fetch 是多线程跑的，
        共享变量会被别的线程清空，判定结果不可靠。
        """
        params: dict[str, Any] = {"cid": cid}
        if bvid:
            params["bvid"] = bvid
        else:
            params["aid"] = aid
        first_err: Optional[Exception] = None
        try:
            tracks = self._parse_tracks(self._request(API_PLAYER, params))
            if tracks:
                return tracks
        except RiskyBanned:
            raise
        except Exception as e:  # noqa: BLE001
            first_err = e
            log.debug("player/v2 失败: %s", e)

        if self._wbi_dead:
            if raise_on_error and first_err:
                raise first_err
            return []
        try:
            resp = self._request(API_PLAYER_WBI, {"aid": aid, "cid": cid}, signed=True)
            if resp.get("code") == 0:
                return self._parse_tracks(resp)
            log.debug("player/wbi/v2 code=%s %s", resp.get("code"), resp.get("message"))
        except RiskyBanned as e:
            if not self._wbi_dead:
                log.info("WBI 接口被风控（%s），本会话改用 x/player/v2", e)
                self._wbi_dead = True
        except Exception as e:  # noqa: BLE001
            first_err = first_err or e
            log.debug("player/wbi/v2 失败: %s", e)
        if raise_on_error and first_err:
            raise first_err
        return []

    def _resolve_url(self, track: dict, aid: int, cid: int) -> str:
        url = _norm_url(track.get("subtitle_url", ""))
        if url:
            return url
        lan = str(track.get("lan", ""))
        if not (lan.startswith("ai-") and aid and cid):
            return ""
        # 新版 AI 字幕：player 里 url 为空，需二次请求。这个接口偶发限流/未就绪，
        # 一次失败不代表没有 —— 批量跑 28 讲实测约四成拿不到，失败分布是
        # 「每 2~3 讲挂一个」的限流节奏，退避重试能救回大半。
        for attempt, signed in enumerate((False, True, False)):
            try:
                d = self._request(API_AI_STAT, {"aid": aid, "cid": cid}, signed=signed)
                url = _norm_url(((d.get("data") or {}).get("subtitle_url")) or "")
                if url:
                    log.debug("AI 字幕 URL 经 search/stat 取得: cid=%s 第%d次", cid, attempt + 1)
                    return url
            except Exception as e:  # noqa: BLE001
                log.debug("ai/subtitle/search/stat(%s) 失败: %s", "signed" if signed else "plain", e)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        return ""

    @staticmethod
    def pick_track(tracks: list[dict], prefer_lang: str = "zh") -> Optional[dict]:
        """优先级：指定语言人工 > 指定语言 AI > 其他人工 > 其他 AI。"""
        if not tracks:
            return None

        def score(t: dict) -> tuple:
            lan = str(t.get("lan", "")).lower()
            doc = str(t.get("lan_doc", "")).lower()
            base = lan[3:] if lan.startswith("ai-") else lan  # ai-zh -> zh
            is_ai = lan.startswith("ai-") or "自动" in doc or "ai" in doc
            want = base.startswith(prefer_lang) or (prefer_lang == "zh" and "中文" in doc)
            return (int(want), int(not is_ai))

        return max(tracks, key=score)

    # ---------------- 取字幕内容 ----------------
    def _download_body(self, url: str) -> list[dict]:
        # 字幕文件走 CDN，用刚刚验证过能通的那条链路（代理或直连）
        r = self._session().get(url, timeout=self.timeout, headers={"Referer": "https://www.bilibili.com/"})
        r.raise_for_status()
        raw = r.json()
        body = raw.get("body") if isinstance(raw, dict) else None
        if not isinstance(body, list):
            raise RuntimeError("字幕 JSON 无 body 字段")
        out = []
        for item in body:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            out.append(
                {
                    "from": float(item.get("from", 0) or 0),
                    "to": float(item.get("to", 0) or 0),
                    "text": text,
                }
            )
        return out

    def _cache_path(self, bvid: str, cid: int) -> str:
        assert self.cache_dir
        return os.path.join(self.cache_dir, f"{bvid}_{cid}.json")

    # 字幕覆盖度合理区间：末段结束时间 / 分P时长。
    # B站 偶发返回**别的视频**的字幕对象，两个方向都会出现：
    #   - 偏低：61 分钟视频只给前 5 分钟（P02 案例，8.1%）
    #   - 偏高：11 分钟视频给回 20 分钟的字幕（BV1yNxM6wEiP 案例，168.7%）
    # 两种情况内容都对不上，宁可回落到转写也不要用错文本。
    MIN_COVERAGE = 0.70
    MAX_COVERAGE = 1.20  # 允许字幕略超片尾（滚动字幕、片尾提示）
    # 时长过短的分P不做覆盖度校验（片头/预告本身字幕就短）
    COVERAGE_MIN_DURATION = 90
    # 字幕正文与该视频「AI 总结」的词汇重合度下限。覆盖度只管时长对不对，
    # 管不了「长度恰好相仿的另一集」；这一层才能抓住同长度串台。
    # 实测正确样本 0.35 / 0.38，串台的电影解说字幕 0.04。
    MIN_AFFINITY = 0.12
    # 轨道整体重查次数。B站 对个别 cid 会连续下发坏对象（实测某 cid 5 次里
    # 4 次返回 404、1 次正确），多试几次很便宜（404 现在毫秒级失败）。
    MAX_TRACK_ATTEMPTS = 3

    def fetch(
        self,
        bvid: str,
        cid: int = 0,
        aid: int = 0,
        prefer_lang: str = "zh",
        force: bool = False,
        min_coverage: Optional[float] = None,
        duration: int = 0,
        verify_summary: bool = True,
    ) -> dict:
        """取单分P字幕。返回 available/kind/lang/text/segments/srt。

        不下载任何音视频，1~3 次 API 调用，命中缓存时 0 次。
        """
        view = None
        if not aid:
            view = self.get_view(bvid)
            aid = view["aid"]
            if view.get("charge_exclusive"):
                return {"available": False, "reason": "charge_exclusive", "message": "充电专属，字幕不可取"}
        # cid=0 必须兜底成第一个分P：早期只在「没给 aid」时才 resolve cid，
        # 一旦调用方传了 aid 却漏了 cid，就会带着 cid=0 去查轨道，
        # 拿回什么全看 B站 心情（可能直接是别的分P/别的视频的字幕）。
        if not cid:
            if view is None:
                view = self.get_view(bvid)
            if view.get("pages"):
                cid = view["pages"][0]["cid"]

        # 时长用于校验字幕覆盖度（B站偶发返回时长严重不符的字幕对象）
        if duration <= 0:
            if view is None:
                try:
                    view = self.get_view(bvid)
                except Exception:  # noqa: BLE001
                    view = None
            if view:
                duration = next((p["duration"] for p in view["pages"] if p["cid"] == cid), 0)

        cov_min = self.MIN_COVERAGE if min_coverage is None else min_coverage
        cov_max = self.MAX_COVERAGE if min_coverage is None else float("inf")
        check_cov = duration >= self.COVERAGE_MIN_DURATION and cov_min > 0

        def _coverage(segs: list[dict]) -> float:
            if not segs:
                return 0.0
            return max((s.get("to") or 0) for s in segs) / duration

        def _cov_bad(cov: float) -> str:
            """返回不可用理由；空串表示覆盖度正常。"""
            if not check_cov:
                return ""
            if cov < cov_min:
                return f"覆盖仅 {cov * 100:.0f}%"
            if cov > cov_max:
                return f"覆盖超出 {cov * 100:.0f}%"
            return ""

        # 该视频自己的 AI 总结，用于第二层内容校验（抓「长度相仿的串台」）
        summary = ""
        if verify_summary:
            try:
                # owner_mid 拿不到时交给 get_summary 自己补（内部有 view 缓存）
                summary = self.get_summary(bvid, cid, int((view or {}).get("owner_mid") or 0))
            except Exception as e:  # noqa: BLE001
                log.debug("取 AI 总结失败，跳过内容校验: %s", e)

        cpath = self._cache_path(bvid, cid) if (self.cache_dir and self.use_cache) else ""
        if cpath and not force and os.path.exists(cpath):
            try:
                with open(cpath, encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("available"):
                    cov = _coverage(cached.get("segments") or [])
                    bad = _cov_bad(cov)
                    aff = text_affinity(cached.get("raw_text") or cached.get("text") or "", summary) if summary else 1.0
                    if not bad and aff < self.MIN_AFFINITY:
                        bad = f"内容与该视频不符（亲和度 {aff:.2f}）"
                    if bad:
                        log.info("缓存字幕%s，丢弃重取: %s", bad, cpath)
                        try:
                            os.remove(cpath)
                        except OSError:
                            pass
                    else:
                        cached["cached"] = True
                        return cached
            except Exception:  # noqa: BLE001
                pass

        try:
            tracks = self.list_tracks(aid, cid, bvid, raise_on_error=True)
        except RiskyBanned:
            raise
        except Exception as e:  # noqa: BLE001
            # 请求挂了 ≠ 没字幕，别误导用户去重新扫码
            return {
                "available": False,
                "reason": "track_error",
                "message": f"字幕轨道查询失败（网络/接口异常，非无字幕）：{str(e)[:160]}",
            }
        if not tracks:
            hint = "（未登录，AI 字幕通常需要登录态 SESSDATA）" if not self.has_login else ""
            return {"available": False, "reason": "no_track", "message": f"该分P无字幕轨道{hint}"}

        # 按优先级逐个尝试，第一个能抓到内容的胜出
        def _ordered(ts: list[dict]) -> list[dict]:
            return sorted(
                ts,
                key=lambda t: (
                    int(
                        str(t.get("lan", "")).replace("ai-", "").startswith("zh")
                        or "中文" in str(t.get("lan_doc", ""))
                    ),
                    int(not str(t.get("lan", "")).startswith("ai-")),
                ),
                reverse=True,
            )

        def _try(ordered: list[dict]) -> tuple[Optional[dict], list[str]]:
            errs: list[str] = []
            for track in ordered:
                lan = str(track.get("lan", ""))
                try:
                    # 记录地址来源：player 自带 url，还是 ai/subtitle/search/stat 二次取。
                    # 串台时这个字段能直接指出是哪个接口给错了。
                    raw_url = str(track.get("subtitle_url") or "").strip()
                    url = self._resolve_url(track, aid, cid)
                    url_src = "player" if raw_url else ("stat" if url else "")
                    if not url:
                        errs.append(f"{lan}: 无 URL")
                        continue
                    log.debug("尝试轨道 lan=%s cid=%s 来源=%s url=%s", lan, cid, url_src or "?", url[:120])
                    segs = self._download_body(url)
                    if not segs:
                        errs.append(f"{lan}: body 为空")
                        continue
                    log.debug("  下载成功: %d 段, 末段 %.0fs", len(segs), max(s["to"] for s in segs))
                    cov = _coverage(segs)
                    bad = _cov_bad(cov)
                    if bad:
                        # 覆盖度离谱：内容几乎肯定不是这一分P的，换轨道重试且绝不写缓存
                        end = max(s["to"] for s in segs)
                        errs.append(f"{lan}: {bad}（末段 {end:.0f}s / 时长 {duration}s）")
                        log.info(
                            "字幕%s，弃用该轨道: lan=%s 末段=%.0fs 时长=%ss cid=%s 地址来源=%s",
                            bad, lan, end, duration, cid, url_src or "?",
                        )
                        continue
                    # 第二层：内容是否真的在讲这个视频（管「长度相仿的串台」）
                    body_text = "".join(s["text"] for s in segs)
                    aff = text_affinity(body_text, summary) if summary else 1.0
                    if summary and aff < self.MIN_AFFINITY:
                        errs.append(f"{lan}: 内容与该视频不符（亲和度 {aff:.2f}）")
                        log.info(
                            "字幕内容与该视频不符，弃用该轨道: lan=%s 亲和度=%.2f cid=%s 地址来源=%s",
                            lan, aff, cid, url_src or "?",
                        )
                        continue
                    body_lines = _dedupe_lines(s["text"] for s in segs)
                    if lan.replace("ai-", "").startswith("zh"):
                        try:
                            from .zh_convert import to_simplified
                        except ImportError:
                            try:
                                from zh_convert import to_simplified
                            except ImportError:
                                to_simplified = None  # type: ignore
                        if to_simplified:
                            body_lines = [to_simplified(x) for x in body_lines]
                    result = {
                        "available": True,
                        "kind": "ai" if lan.startswith("ai-") else "cc",
                        "lang": lan,
                        "lang_doc": track.get("lan_doc", lan),
                        "bvid": bvid,
                        "cid": cid,
                        "segments": segs,
                        "srt": segments_to_srt(segs),
                        "text": _paragraphize(body_lines),
                        "raw_text": "\n".join(body_lines),
                        "chars": sum(len(s["text"]) for s in segs),
                        # 便于事后排查串台：地址来源、覆盖度、亲和度一起落盘
                        "url_source": url_src,
                        "coverage": round(cov, 4),
                        "affinity": round(aff, 4),
                    }
                    if cpath:
                        try:
                            with open(cpath, "w", encoding="utf-8") as f:
                                json.dump(result, f, ensure_ascii=False)
                        except Exception as e:  # noqa: BLE001
                            log.debug("缓存写入失败: %s", e)
                    return result, errs
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{lan}: {e}")
                    continue
            return None, errs

        errors: list[str] = []
        for attempt in range(self.MAX_TRACK_ATTEMPTS):
            if attempt:
                # B站 偶发下发「别的视频」的字幕对象，重查一次轨道通常就能拿到正确的
                log.info("字幕全部不可用，重查轨道后再试一次（B站 偶发下发错误字幕对象）")
                time.sleep(1.2)
                try:
                    again = self.list_tracks(aid, cid, bvid, raise_on_error=True)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"重查轨道失败: {e}")
                    break
                if not again:
                    errors.append("重查后仍无轨道")
                    break
                tracks = again
            got, errs = _try(_ordered(tracks))
            errors.extend(errs)
            if got:
                return got

        off_cov = [e for e in errors if "覆盖仅" in e or "覆盖超出" in e]
        mismatch = [e for e in errors if "内容与该视频不符" in e]
        if off_cov and len(off_cov) == len(errors):
            reason = "coverage_bad"
        elif mismatch:
            reason = "content_suspect"
        else:
            reason = "fetch_failed"
        return {
            "available": False,
            "reason": reason,
            "message": "字幕轨道存在但内容不可用：" + "; ".join(errors[:4]),
        }

    def probe(self, bvid: str, max_workers: int = 4, pages: Optional[list[int]] = None) -> dict:
        """并发探测全部分P的字幕可用性（只查轨道，不下载内容）。"""
        view = self.get_view(bvid)
        targets = [p for p in view["pages"] if not pages or p["page"] in pages]

        def one(p: dict) -> dict:
            row = {
                "page": p["page"],
                "cid": p["cid"],
                "part": p["part"],
                "duration": p["duration"],
                "has_sub": False,
                "lan": "",
                "lan_doc": "",
                "kind": "",
            }
            try:
                # raise_on_error：让探测表能区分「真没字幕」和「接口挂了」
                tracks = self.list_tracks(view["aid"], p["cid"], bvid, raise_on_error=True)
                best = self.pick_track(tracks)
                if best:
                    lan = str(best.get("lan", ""))
                    row.update(
                        has_sub=True,
                        lan=lan,
                        lan_doc=best.get("lan_doc", lan),
                        kind="ai" if lan.startswith("ai-") else "cc",
                        tracks=len(tracks),
                    )
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:120]
            return row

        if max_workers > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                rows = list(pool.map(one, targets))
        else:
            rows = [one(p) for p in targets]

        rows.sort(key=lambda r: r["page"])
        return {
            "bvid": bvid,
            "title": view["title"],
            "aid": view["aid"],
            "owner": view["owner"],
            "stat": view["stat"],
            "duration": view["duration"],
            "total": len(rows),
            "has_sub": sum(1 for r in rows if r["has_sub"]),
            "pages": rows,
        }


__all__ = ["BiliSubtitleClient", "segments_to_srt"]
