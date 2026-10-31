# -*- coding: utf-8 -*-
"""B站扫码登录 + 一次性会话（用完即焚）。

为什么要这个模块
    B站 AI 字幕需要登录态，但手抄 SESSDATA 既不安全（长期凭证、明文落盘、
    忘了删就长期有效）又麻烦。这里改走**手机扫码登录**：
        1. 生成登录二维码（URL 由 B站下发，不经过任何第三方）
        2. 手机 B站 App 扫码确认
        3. 从响应里直接收取本次会话的 Cookie（SESSDATA / bili_jct / DedeUserID）
        4. 任务结束调用 login/exit/v2 注销该会话 + 覆写删除本地文件

    由此得到的是**一次性设备会话**：默认 30 分钟 TTL，用完即注销，
    不会像手抄 Cookie 那样长期有效、也不会影响你手机和其它设备的登录。

CLI
    python bili_auth.py login --qr-out qr.png     # 扫码登录（含等待）
    python bili_auth.py login --no-wait           # 只出二维码，稍后 wait
    python bili_auth.py wait                      # 等上一次扫码结果
    python bili_auth.py status [--json]           # 查看当前会话（不打印凭证）
    python bili_auth.py logout                    # 注销 + 焚毁本地会话
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import tempfile
import time
from typing import Any, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

API_QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
API_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
API_QR_GEN_ALT = "https://api.bilibili.com/x/passport-login/web/qrcode/generate"
API_QR_POLL_ALT = "https://api.bilibili.com/x/passport-login/web/qrcode/poll"
API_LOGOUT = "https://passport.bilibili.com/login/exit/v2"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_SPI = "https://api.bilibili.com/x/frontend/finger/spi"

# 轮询返回码
QR_OK = 0
QR_EXPIRED = 86038
QR_SCANNED = 86090
QR_WAITING = 86101

BLOCK_HINT = (
    "B站风控（HTTP 412）：当前出口 IP 被临时限制，通常等 30~60 分钟或切换网络"
    "（手机热点 / 换代理节点）后恢复。这不是 Cookie 问题。"
)


class NetworkBlocked(RuntimeError):
    """IP 级风控：重试无意义。"""


def proxy_reachable(timeout: float = 1.5) -> bool:
    """环境代理端口是否真的能连上。

    本机代理客户端（Clash 等）重启时端口会换号，env 里的
    HTTPS_PROXY 就成了一个指向死端口的地址 —— requests 会一直
    ProxyError / 502，白等十几秒才轮到直连。这里先用 TCP 探一下。
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        u = raw if "://" in raw else "http://" + raw
        try:
            body = u.split("://", 1)[1]
            body = body.rsplit("@", 1)[-1]  # 去掉 user:pass@
            host, _, port_s = body.partition(":")
            host = host.strip("/")
            if host.startswith("["):  # IPv6
                host = host.split("]")[0].lstrip("[")
            port = int(port_s.split("/")[0]) if port_s else 8080
        except (ValueError, IndexError):
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False
    return True  # 没配代理，按「可用」处理


# 内部小写键 → 真实 cookie 名
_COOKIE_NAMES = {
    "sessdata": "SESSDATA",
    "bili_jct": "bili_jct",
    "dedeuserid": "DedeUserID",
    "buvid3": "buvid3",
    "buvid4": "buvid4",
}

DEFAULT_TTL = 1800  # 30 分钟

SESSION_DIR = os.path.join(tempfile.gettempdir(), "bili_ephemeral")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")
PENDING_FILE = os.path.join(SESSION_DIR, "pending.json")
PENDING_REVOKE_FILE = os.path.join(SESSION_DIR, "pending_revoke.json")


# --------------------------------------------------------------------------- #
# 会话存储（临时目录 + 用完焚毁）
# --------------------------------------------------------------------------- #
def _ensure_dir() -> None:
    os.makedirs(SESSION_DIR, exist_ok=True)


def _harden(path: str) -> None:
    """尽量收紧文件权限（Windows 上 chmod 只影响只读位，其余靠临时目录隔离）。"""
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass


def _shred(path: str) -> None:
    """覆写后删除，避免从磁盘残留恢复。"""
    try:
        if os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, "r+b") as f:
                f.write(b"\x00" * max(size, 1))
                f.flush()
                os.fsync(f.fileno())
            os.remove(path)
    except Exception:  # noqa: BLE001
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


def load_session(check_expiry: bool = True) -> Optional[dict]:
    """读取本地会话；过期/损坏则当场焚毁并返回 None。"""
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        _shred(SESSION_FILE)
        return None
    if not data.get("sessdata"):
        _shred(SESSION_FILE)
        return None
    if check_expiry and data.get("expires", 0) < time.time():
        _shred(SESSION_FILE)
        return None
    return data


def save_session(creds: dict, ttl: int = DEFAULT_TTL, source: str = "qr") -> dict:
    _ensure_dir()
    data = dict(creds)
    data.setdefault("created", int(time.time()))
    data["expires"] = int(time.time()) + int(ttl)
    data["source"] = source
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    _harden(tmp)
    os.replace(tmp, SESSION_FILE)
    _harden(SESSION_FILE)
    return data


def wipe_session() -> bool:
    """删除本地会话与二维码（返回是否确实删掉了东西）。

    注意：不动 pending_revoke.json —— 那是注销失败的待办，必须留着续做。
    """
    existed = os.path.exists(SESSION_FILE)
    _shred(SESSION_FILE)
    _shred(PENDING_FILE)
    _shred(os.path.join(SESSION_DIR, "qr.png"))
    try:
        leftovers = [n for n in os.listdir(SESSION_DIR) if n != os.path.basename(PENDING_REVOKE_FILE)]
        if os.path.isdir(SESSION_DIR) and not leftovers:
            os.rmdir(SESSION_DIR)
    except Exception:  # noqa: BLE001
        pass
    return existed


def remaining_ttl() -> int:
    s = load_session()
    return max(0, int(s["expires"] - time.time())) if s else 0


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
def _session(cookie: Optional[dict] = None, passport: bool = False) -> requests.Session:
    """passport=True 用于账密/二维码接口：Referer 必须是 passport 域名。

    实测：给 passport 接口带 www 的 Origin/Referer 会被返回 404 HTML。
    """
    s = requests.Session()
    ref = "https://passport.bilibili.com/login" if passport else "https://www.bilibili.com/"
    headers = {
        "User-Agent": UA,
        "Referer": ref,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if not passport:
        headers["Origin"] = "https://www.bilibili.com"
    s.headers.update(headers)
    # 环境代理端口已死（代理客户端换号了）时直接走直连，别在死代理上白等
    if not proxy_reachable():
        s.trust_env = False
    # 只认这 5 个凭证键；会话文件里还混有 expires/created 等整数字段，不能当 cookie
    for k in ("sessdata", "bili_jct", "dedeuserid", "buvid3", "buvid4"):
        v = (cookie or {}).get(k)
        if isinstance(v, str) and v:
            s.cookies.set(_COOKIE_NAMES[k], v, domain=".bilibili.com")
    return s


def _json(s: requests.Session, url: str, params: Optional[dict] = None,
          alt: str = "", timeout: int = 15) -> dict:
    """GET 并解析 JSON；主域名 404/非 JSON 时自动换备用域名。"""
    last: Any = None
    urls = [url] + ([alt] if alt else [])
    for u in urls:
        for attempt in range(2):  # 代理抖动时同一个 URL 再试一次
            try:
                r = s.get(u, params=params, timeout=timeout)
                if r.status_code == 412:
                    raise NetworkBlocked(BLOCK_HINT)
                ctype = r.headers.get("content-type", "")
                if r.status_code != 200 or "json" not in ctype:
                    last = f"{r.status_code} {ctype.split(';')[0]}"
                    break
                payload = r.json()
                if isinstance(payload, dict) and payload.get("code") == -412:
                    raise NetworkBlocked(BLOCK_HINT)
                return payload
            except NetworkBlocked:
                raise
            except Exception as e:  # noqa: BLE001
                last = str(e)[:120]
                if attempt == 0:
                    time.sleep(1.5)
    raise RuntimeError(f"接口不可用（{last}）：{url}")


def fetch_buvid(s: requests.Session) -> dict:
    """领一对设备指纹 cookie，显著降低 -352/-412 风控概率。"""
    try:
        d = s.get(API_SPI, timeout=10).json()
        data = d.get("data") or {}
        return {"buvid3": data.get("b_3", ""), "buvid4": data.get("b_4", "")}
    except Exception:  # noqa: BLE001
        return {}


def whoami_state(creds: dict, timeout: int = 10) -> tuple[str, str]:
    """三态校验：ok / invalid / unknown。

    必须区分「真的失效」和「网络抽风」——早期版本把 Read timed out
    当成失效处理，直接把一份好好的会话焚毁了，代价过高。
    """
    last = ""
    for attempt in range(2):
        try:
            s = _session(creds)
            d = s.get(API_NAV, timeout=timeout).json()
        except Exception as e:  # noqa: BLE001
            last = f"nav 请求失败: {e}"
            if attempt == 0:
                continue
            return "unknown", last
        code = d.get("code")
        if code == 0:
            return "ok", ((d.get("data") or {}).get("uname") or "已登录")
        if code == -101:
            return "invalid", "未登录 / SESSDATA 已失效"
        return "invalid", f"nav code={code} {d.get('message')}"
    return "unknown", last


def whoami(creds: dict) -> tuple[bool, str]:
    """校验会话是否仍然有效（unknown 视为不可用）。"""
    state, msg = whoami_state(creds)
    return state == "ok", msg


def revoke(creds: dict, retries: int = 3, timeout: int = 20) -> tuple[bool, str]:
    """注销本次会话（只影响这一份 SESSDATA，不动手机和其它设备）。

    带重试：B站/代理抖动时注销会超时，而「本地删了但服务端没注销」
    等于凭证还活着，必须尽力重试。
    """
    if not creds.get("sessdata") or not creds.get("bili_jct"):
        return False, "缺少 SESSDATA 或 bili_jct，无法注销"
    last = ""
    for attempt in range(retries):
        try:
            s = _session(creds, passport=True)
            r = s.post(API_LOGOUT, data={"biliCSRF": creds["bili_jct"]}, timeout=timeout)
            d = r.json()
            if d.get("code") == 0:
                return True, "已注销该会话"
            last = f"code={d.get('code')} {d.get('message')}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:80]}"
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    return False, f"注销失败（已重试 {retries} 次）：{last}"


def _save_pending_revoke(creds: dict) -> None:
    """注销失败时留个尾巴，下次 logout 继续尝试，避免凭证长期有效。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(PENDING_REVOKE_FILE)), exist_ok=True)
        with open(PENDING_REVOKE_FILE, "w", encoding="utf-8") as f:
            json.dump({"creds": creds, "at": int(time.time())}, f, ensure_ascii=False)
        _harden(PENDING_REVOKE_FILE)
    except Exception as e:  # noqa: BLE001
        print(f"[auth] ⚠️ 待注销记录写入失败：{e}", flush=True)


def retry_pending_revoke() -> Optional[tuple[bool, str]]:
    """续做上次失败的注销；成功则删掉待办记录。"""
    if not os.path.exists(PENDING_REVOKE_FILE):
        return None
    try:
        with open(PENDING_REVOKE_FILE, encoding="utf-8") as f:
            creds = (json.load(f) or {}).get("creds") or {}
    except Exception:  # noqa: BLE001
        _shred(PENDING_REVOKE_FILE)
        return None
    ok, msg = revoke(creds)
    if ok:
        _shred(PENDING_REVOKE_FILE)
    return ok, msg


# --------------------------------------------------------------------------- #
# 扫码登录
# --------------------------------------------------------------------------- #
def render_qr(url: str, out_path: str) -> str:
    """把登录 URL 渲染成 PNG，返回路径。失败则抛异常由调用方降级。"""
    import qrcode  # 延迟导入，缺库时给出可读提示

    img = qrcode.make(url)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    return out_path


def _safe_nav(s: requests.Session):
    """尽力探一次 nav（用于落 cookie），失败也不影响已拿到的 Set-Cookie。"""
    try:
        return s.get(API_NAV, timeout=10)
    except Exception:  # noqa: BLE001
        return None


def dump_cookies(creds: dict) -> str:
    """诊断用：只输出 cookie 名与长度，绝不输出值。"""
    parts = []
    for k in ("sessdata", "bili_jct", "dedeuserid", "buvid3", "buvid4"):
        v = (creds or {}).get(k) or ""
        parts.append(f"{k}={'有' if v else '空'}({len(v)})")
    return " ".join(parts)


def _extract_cookies(s: requests.Session, resp=None) -> dict:
    """从会话 cookie 罐里取本次登录的三个关键值。"""
    jar = {c.name: c.value for c in s.cookies}
    # 有些环境 Set-Cookie 只在响应头里，再兜一层
    raw_headers = resp.headers.get("Set-Cookie", "") if resp is not None else ""
    for raw in raw_headers.split(","):
        if "=" in raw:
            k, _, v = raw.strip().partition("=")
            jar.setdefault(k.strip(), v.split(";")[0])
    return {
        "sessdata": jar.get("SESSDATA", ""),
        "bili_jct": jar.get("bili_jct", ""),
        "dedeuserid": jar.get("DedeUserID", ""),
        "buvid3": jar.get("buvid3", ""),
        "buvid4": jar.get("buvid4", ""),
    }


def _qr_login_once(
    qr_out: str = "",
    timeout: int = 180,
    ttl: int = DEFAULT_TTL,
    interval: float = 1.5,
    on_qr=None,
    save: bool = True,
) -> dict:
    """单轮扫码登录：生成一枚二维码并等待确认。

    Args:
        qr_out: 二维码 PNG 输出路径，空则不落图。
        timeout: 等待确认的最长秒数。
        ttl: 会话有效期（秒），过期自动焚毁。
        on_qr: 二维码就绪回调，参数 (url, png_path, qrcode_key)。
        save: 成功后是否写入本地一次性会话文件。

    Returns:
        {"ok": True, "creds": {...}, "expires": ...} 或 {"ok": False, "error": ...}
    """
    s = _session(passport=True)
    buvid = fetch_buvid(s)
    for k, v in buvid.items():
        if v:
            s.cookies.set(k, v, domain=".bilibili.com")

    try:
        gen = _json(s, API_QR_GEN, alt=API_QR_GEN_ALT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"二维码生成请求失败: {e}"}
    if gen.get("code") != 0:
        return {"ok": False, "error": f"二维码生成失败: {gen.get('code')} {gen.get('message')}"}

    url = (gen.get("data") or {}).get("url", "")
    key = (gen.get("data") or {}).get("qrcode_key", "")
    if not url or not key:
        return {"ok": False, "error": "二维码生成返回缺少 url/qrcode_key"}

    png = ""
    if qr_out:
        try:
            png = render_qr(url, qr_out)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"二维码图片渲染失败: {e}", "url": url}

    _ensure_dir()
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump({"qrcode_key": key, "url": url, "created": int(time.time())}, f)
    _harden(PENDING_FILE)

    if on_qr:
        on_qr(url, png, key)

    deadline = time.time() + timeout
    last_state = ""
    while time.time() < deadline:
        try:
            d = _json(s, API_QR_POLL, {"qrcode_key": key}, alt=API_QR_POLL_ALT)
        except Exception:  # noqa: BLE001
            time.sleep(interval)
            continue
        data = d.get("data") or {}
        code = data.get("code")
        if code == QR_OK:
            creds = _extract_cookies(s, _safe_nav(s))
            if not creds.get("sessdata"):
                return {"ok": False, "error": "扫码成功但未取到 SESSDATA"}
            creds.update({k: v for k, v in buvid.items() if v and not creds.get(k)})
            result: dict[str, Any] = {"ok": True, "creds": creds, "source": "qr"}
            if save:
                saved = save_session(creds, ttl=ttl, source="qr")
                result["expires"] = saved["expires"]
            _shred(PENDING_FILE)
            return result
        if code == QR_EXPIRED:
            return {"ok": False, "error": "二维码已失效，请重新生成"}
        state = {QR_WAITING: "waiting", QR_SCANNED: "scanned"}.get(code, str(code))
        if state != last_state:
            last_state = state
            print(f"  [{state}] {data.get('message') or ''}", flush=True)
        time.sleep(interval)

    return {"ok": False, "error": f"等待扫码超时（{timeout}s）"}


def qr_login(
    qr_out: str = "",
    timeout: int = 180,
    ttl: int = DEFAULT_TTL,
    interval: float = 1.5,
    on_qr=None,
    save: bool = True,
    rounds: int = 2,
) -> dict:
    """完整扫码登录流程：默认自动给两轮。

    第一轮二维码超时或失效后，**自动重新生成一枚新二维码再等一轮**——
    扫码超时太常见了（手机不在手边、网慢加载不出确认页），不该让整个
    任务直接失败。两轮都没扫上才算失败。

    Args:
        rounds: 二维码总轮数（默认 2）。
        其余参数同 `_qr_login_once`。

    Returns:
        {"ok": True, "creds": {...}, "expires": ...} 或 {"ok": False, "error": ...}
    """
    last: dict = {"ok": False, "error": "未开始"}
    for rnd in range(1, max(1, rounds) + 1):
        if rnd > 1:
            print(f"  [auth] 第 {rnd}/{rounds} 轮：重新生成二维码，请再扫一次", flush=True)
        res = _qr_login_once(qr_out=qr_out, timeout=timeout, ttl=ttl,
                             interval=interval, on_qr=on_qr, save=save)
        if res.get("ok"):
            return res
        last = res
    return last


def wait_pending(timeout: int = 120, ttl: int = DEFAULT_TTL, interval: float = 1.5) -> dict:
    """继续等待上一次 `login --no-wait` 生成的二维码。

    关键：必须沿用生成二维码时那套 buvid 设备指纹，否则 B站 会把
    登录态绑定到旧指纹上，拿回来的 SESSDATA 校验为「未登录」。
    """
    if not os.path.exists(PENDING_FILE):
        return {"ok": False, "error": "没有待确认的二维码，请先 login --no-wait"}
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            pending = json.load(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读取待确认二维码失败: {e}"}
    key = pending.get("qrcode_key", "")
    if not key:
        return {"ok": False, "error": "待确认二维码已损坏"}

    buvid = pending.get("buvid") or {}
    s = _session(passport=True)
    if not buvid:
        buvid = fetch_buvid(s)  # 老版本 pending 没存指纹，只能现领
    for k, v in buvid.items():
        if v:
            s.cookies.set(k, v, domain=".bilibili.com")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = _json(s, API_QR_POLL, {"qrcode_key": key}, alt=API_QR_POLL_ALT)
        except NetworkBlocked as e:
            return {"ok": False, "error": str(e)}
        except Exception:  # noqa: BLE001
            time.sleep(interval)
            continue
        data = d.get("data") or {}
        if data.get("code") == QR_OK:
            creds = _extract_cookies(s, _safe_nav(s))
            if not creds.get("sessdata"):
                return {"ok": False, "error": "扫码成功但未取到 SESSDATA"}
            for k, v in buvid.items():
                if v and not creds.get(k):
                    creds[k] = v
            saved = save_session(creds, ttl=ttl, source="qr")
            _shred(PENDING_FILE)
            return {"ok": True, "creds": creds, "expires": saved["expires"]}
        if data.get("code") == QR_EXPIRED:
            return {"ok": False, "error": "二维码已失效"}
        time.sleep(interval)
    return {"ok": False, "error": f"等待超时（{timeout}s）"}


# --------------------------------------------------------------------------- #
# 给提取器用的统一入口
# --------------------------------------------------------------------------- #
def resolve_credentials(
    env_file: str = "",
    login: str = "auto",
    qr_out: str = "",
    ttl: int = DEFAULT_TTL,
    qr_timeout: int = 180,
    verbose: bool = True,
) -> dict:
    """按策略拿到一份可用凭证。

    login:
        auto    本地一次性会话 → .env（若有效）→ 扫码
        session 只用本地一次性会话，没有就失败
        env     只用 .env 里的长期 Cookie（不推荐）
        qr      强制重新扫码

    Returns:
        {"creds": {...}, "source": "qr|env", "ephemeral": bool, "ttl": int}
    """
    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    if login in ("auto", "session"):
        sess = load_session()
        if sess:
            state, who = whoami_state(sess)
            if state == "ok":
                say(f"[auth] 复用一次性会话（剩余 {remaining_ttl() // 60} 分钟）：{who}")
                return {"creds": sess, "source": "qr", "ephemeral": True,
                        "ttl": remaining_ttl(), "uname": who}
            if state == "unknown":
                # 网络抽风而不是真的失效：保留会话，乐观使用（后续请求失败会自行回落）
                say(f"[auth] 会话校验未完成（网络问题，保留不焚毁）：{who}")
                return {"creds": sess, "source": "qr", "ephemeral": True,
                        "ttl": remaining_ttl(), "uname": "未校验"}
            say("[auth] 本地会话已失效，已焚毁")
            wipe_session()

    if login in ("auto", "env") and env_file:
        env = _read_env(env_file)
        creds = {
            "sessdata": env.get("BILIBILI_SESSDATA", ""),
            "bili_jct": env.get("BILIBILI_BILI_JCT", ""),
            "dedeuserid": env.get("BILIBILI_DEDE_USER_ID", ""),
        }
        if creds["sessdata"]:
            state, who = whoami_state(creds)
            if state == "ok":
                say(f"[auth] 使用 .env 长期 Cookie：{who}（提示：这是长期凭证，建议改用扫码）")
                return {"creds": creds, "source": "env", "ephemeral": False, "ttl": 0, "uname": who}
            if state == "unknown":
                say(f"[auth] .env Cookie 校验未完成（网络问题，继续尝试）：{who}")
                return {"creds": creds, "source": "env", "ephemeral": False, "ttl": 0, "uname": "未校验"}
            say(f"[auth] .env Cookie 已失效（{who}），需要重新登录")

    if login == "env":
        return {"creds": {}, "source": "none", "ephemeral": False, "ttl": 0,
                "error": ".env 中没有可用的 BILIBILI_SESSDATA"}

    if login == "session":
        return {"creds": {}, "source": "none", "ephemeral": False, "ttl": 0,
                "error": "没有可用的一次性会话，请先扫码登录"}

    # 扫码（auto 的最后一档 / 显式 qr）
    say("[auth] 请用手机 B站 App 扫码登录（二维码 3 分钟内有效）")
    res = qr_login(qr_out=qr_out, timeout=qr_timeout, ttl=ttl,
                   on_qr=lambda u, p, k: say(f"[auth] 二维码已生成：{p or u}"))
    if not res.get("ok"):
        return {"creds": {}, "source": "none", "ephemeral": False, "ttl": 0,
                "error": res.get("error", "扫码登录失败")}
    ok, who = whoami(res["creds"])
    say(f"[auth] 登录成功：{who}（会话 {ttl // 60} 分钟后自动失效）")
    return {"creds": res["creds"], "source": "qr", "ephemeral": True,
            "ttl": ttl, "uname": who}


def destroy(creds: Optional[dict] = None, do_revoke: bool = True) -> None:
    """用完即焚：注销服务端会话 + 覆写删除本地文件。

    若注销因网络失败，会留下待办记录并明确告警，避免「文件删了但凭证还活着」。
    """
    creds = creds or load_session()
    if do_revoke and creds:
        ok, msg = revoke(creds)
        print(f"[auth] {msg}", flush=True)
        if not ok:
            _save_pending_revoke(creds)
            print("[auth] ⚠️ 服务端会话可能仍然有效！已记录待办，联网后运行 "
                  "`python src/bili_auth.py logout` 补注销；", flush=True)
            print("[auth]    或直接在手机 B站 App「设置 → 退出登录」立即作废全部会话。", flush=True)
    wipe_session()
    print("[auth] 本地会话已焚毁", flush=True)


def _read_env(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        from dotenv import dotenv_values  # type: ignore

        return {k: v for k, v in dotenv_values(path).items() if v}
    except Exception:  # noqa: BLE001
        out = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
        return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="B站扫码登录（一次性会话，用完即焚）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="扫码登录")
    p_login.add_argument("--qr-out", default=os.path.join(SESSION_DIR, "qr.png"), help="二维码 PNG 路径")
    p_login.add_argument("--ttl", type=int, default=DEFAULT_TTL, help=f"会话有效期秒数（默认 {DEFAULT_TTL}）")
    p_login.add_argument("--timeout", type=int, default=180, help="等待扫码秒数")
    p_login.add_argument("--no-wait", action="store_true", help="只生成二维码，稍后用 wait")

    p_wait = sub.add_parser("wait", help="等待上一次扫码结果")
    p_wait.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    p_wait.add_argument("--timeout", type=int, default=120)

    p_status = sub.add_parser("status", help="查看会话状态")
    p_status.add_argument("--json", action="store_true")

    p_out = sub.add_parser("logout", help="注销并焚毁本地会话")
    p_out.add_argument("--keep-local", action="store_true", help="只删本地，不调用注销接口")

    args = ap.parse_args(argv)

    if args.cmd == "login":
        if args.no_wait:
            s = _session(passport=True)
            buvid = fetch_buvid(s)
            try:
                gen = _json(s, API_QR_GEN, alt=API_QR_GEN_ALT)
            except NetworkBlocked as e:
                print(f"[auth] 失败: {e}")
                return 2
            except Exception as e:  # noqa: BLE001
                print(f"[auth] 失败: {e}")
                return 1
            if gen.get("code") != 0:
                print(f"[auth] 生成失败: {gen.get('message')}")
                return 1
            url = gen["data"]["url"]
            png = ""
            if args.qr_out:
                try:
                    png = render_qr(url, args.qr_out)
                except Exception as e:  # noqa: BLE001
                    print(f"[auth] 二维码渲染失败: {e}")
            _ensure_dir()
            with open(PENDING_FILE, "w", encoding="utf-8") as f:
                json.dump({"qrcode_key": gen["data"]["qrcode_key"], "url": url,
                           "created": int(time.time()), "buvid": buvid}, f)
            _harden(PENDING_FILE)
            print(f"[auth] 二维码已生成: {png or url}")
            print("[auth] 手机扫码确认后运行: python bili_auth.py wait")
            return 0

        res = qr_login(qr_out=args.qr_out, timeout=args.timeout, ttl=args.ttl)
        if not res.get("ok"):
            print(f"[auth] 失败: {res.get('error')}")
            return 1
        ok, who = whoami(res["creds"])
        print(f"[auth] 登录{'成功' if ok else '校验异常'}: {who}")
        print(f"[auth] 本地会话: {SESSION_FILE}（{args.ttl // 60} 分钟后过期）")
        print("[auth] 提醒：任务结束后务必运行 python bili_auth.py logout")
        return 0

    if args.cmd == "wait":
        res = wait_pending(timeout=args.timeout, ttl=args.ttl)
        if not res.get("ok"):
            print(f"[auth] 失败: {res.get('error')}")
            return 1
        ok, who = whoami(res["creds"])
        print(f"[auth] 登录{'成功' if ok else '校验异常'}: {who}")
        if not ok:
            print(f"[auth] 凭证诊断: {dump_cookies(res['creds'])}")
            print("[auth] 若 sessdata 有值但仍未登录：可能是扫码时未点「确认登录」，或设备指纹不匹配。重新 login 再试。")
        return 0 if ok else 3

    if args.cmd == "status":
        sess = load_session()
        if not sess:
            info = {"logged_in": False, "reason": "本地无有效会话"}
        else:
            ok, who = whoami(sess)
            info = {
                "logged_in": ok,
                "uname": who,
                "source": sess.get("source", "qr"),
                "ttl_seconds": remaining_ttl(),
                "has_sessdata": bool(sess.get("sessdata")),
                "has_bili_jct": bool(sess.get("bili_jct")),
                "file": SESSION_FILE,
            }
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            for k, v in info.items():
                print(f"  {k:<14}: {v}")
        return 0 if info.get("logged_in") else 2

    if args.cmd == "logout":
        pend = retry_pending_revoke()
        if pend is not None:
            print(f"[auth] 补注销上次遗留会话: {pend[1]}")
        sess = load_session(check_expiry=False)
        if not sess:
            print("[auth] 本地没有会话，无需注销")
            wipe_session()
            return 0
        if args.keep_local:
            wipe_session()
            print("[auth] 已删除本地会话（未调用注销接口，服务端会话仍有效）")
            return 0
        destroy(sess)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
