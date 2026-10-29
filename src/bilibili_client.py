"""B站 API 封装模块。

字幕、视频元信息、分P 与音频地址全部走 `bili_subtitle.py` 的 BiliSubtitleClient
（纯 requests + WBI 签名，不依赖任何第三方 B站 SDK）；
音频下载复用同一个会话，保证代理/Cookie/重试行为一致。
"""

import logging
import os
import tempfile
import time
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


logger = logging.getLogger(__name__)

# 默认下载目录（临时目录下的 bilibili_audio）
_AUDIO_DIR = os.path.join(tempfile.gettempdir(), "bilibili_audio")

API_PLAYURL = "https://api.bilibili.com/x/player/playurl"
API_PLAYURL_WBI = "https://api.bilibili.com/x/player/wbi/playurl"


def download_audio(bvid: str, cid: int = 0) -> str:
    """下载指定分P的音频（仅 DASH 音频流，不下载视频），返回本地 .m4a 路径。

    为什么不用 bilibili-api 的 get_download_url：
        那个库自带网络栈，不走本机 HTTP 代理，代理环境下会 getaddrinfo failed；
        这里统一用 requests + 我们自己的会话（带 Cookie、代理、重试、直连兜底），
        与字幕请求同一条链路，行为一致也更好排障。

    Args:
        bvid: BV 号。
        cid: 分P的 CID，0 表示第一个分P。

    Returns:
        音频文件本地路径。

    Raises:
        ValueError: BV 无效 / 无音频流。
        RuntimeError: 所有备用地址都下载失败。
    """
    os.makedirs(_AUDIO_DIR, exist_ok=True)
    client = _sub_client()

    view = client.get_view(bvid)
    if not cid:
        pages = view.get("pages") or []
        if not pages:
            raise ValueError("该视频没有分P信息")
        cid = pages[0]["cid"]

    filepath = os.path.join(_AUDIO_DIR, f"{bvid}_{cid}.m4a")
    if os.path.exists(filepath) and os.path.getsize(filepath) > 64 * 1024:
        logger.info("音频已存在，跳过下载: %s", filepath)
        return filepath

    params = {"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1}
    data = client._request(API_PLAYURL, params)
    if data.get("code") != 0:
        # 部分视频只认签名接口
        logger.debug("playurl 返回 %s，改用 WBI 签名接口", data.get("code"))
        data = client._request(API_PLAYURL_WBI, params, signed=True)
    if data.get("code") != 0:
        raise ValueError(f"获取音频地址失败: {data.get('code')} {data.get('message')}")

    audios = ((data.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audios:
        raise ValueError("该视频没有可下载的音频流（可能是充电专属或纯视频流）")
    best = max(audios, key=lambda a: (a.get("bandwidth") or 0, a.get("id") or 0))

    urls = [u for u in ([best.get("baseUrl")] + list(best.get("backupUrl") or [])) if u]
    if not urls:
        raise ValueError("音频 URL 为空")

    logger.info("下载音频: %s（%s kbps，%d 个备用源）",
                os.path.basename(filepath), (best.get("bandwidth") or 0) // 1000, len(urls) - 1)

    last: Optional[Exception] = None
    for i, url in enumerate(urls):
        tmp = filepath + ".part"
        try:
            with client.s.get(url, stream=True, timeout=120,
                              headers={"Referer": "https://www.bilibili.com/"}) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            os.replace(tmp, filepath)
            logger.info("音频下载完成: %s（%.1f MB）", filepath, os.path.getsize(filepath) / 1048576)
            return filepath
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("音频源 %d/%d 失败: %s", i + 1, len(urls), str(e)[:100])
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass
    raise RuntimeError(f"所有音频源均下载失败: {last}")


def _sub_client():
    """惰性构造字幕客户端（复用连接、WBI 缓存与磁盘缓存）。

    凭证优先级：一次性扫码会话（临时文件）→ .env 长期 Cookie。
    """
    global _SUB_CLIENT
    if _SUB_CLIENT is None:
        try:
            from .bili_auth import load_session
            from .bili_subtitle import BiliSubtitleClient
        except ImportError:  # 以脚本方式运行时无父包
            from bili_auth import load_session
            from bili_subtitle import BiliSubtitleClient

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _SUB_CLIENT = BiliSubtitleClient.from_env(
            os.path.join(root, ".env"),
            cache_dir=os.path.join(root, "extracted", ".subcache"),
        )
        sess = load_session()
        if sess:
            _SUB_CLIENT.apply_credentials(sess)
            logger.info("使用一次性扫码会话（剩余 %d 分钟）", max(0, int(sess["expires"] - time.time()) // 60))
    return _SUB_CLIENT


_SUB_CLIENT = None


def get_subtitles(bvid: str, video_info: Optional[dict] = None, cid: int = 0) -> dict:
    """获取 B站视频字幕文本（官方 CC → B站 AI 字幕，均无则返回不可用）。

    实现见 `bili_subtitle.py`：按 cid 查 player 接口，AI 字幕 URL 为空时
    再走 ai/subtitle/search/stat 二次取地址。不下载任何音视频文件。

    Args:
        bvid: B站视频 BV 号。
        video_info: 可选的 view 接口结果，用于省掉一次 get_view()。
        cid: 指定分P，0 表示第一个分P。

    Returns:
        available=True 时含 text / source / lang / segments / srt；
        available=False 时含 error。
    """
    client = _sub_client()
    try:
        aid = 0
        if video_info:
            aid = int(video_info.get("aid") or 0)
            if not cid:
                pages = video_info.get("pages") or []
                cid = int(pages[0]["cid"]) if pages else int(video_info.get("cid") or 0)
        if not aid:
            view = client.get_view(bvid)
            aid = view["aid"]
            if not cid:
                cid = view["pages"][0]["cid"]
            if view.get("charge_exclusive"):
                return {"available": False, "error": "充电专属视频，无法获取字幕"}

        res = client.fetch(bvid, cid=cid, aid=aid)
    except Exception as e:  # noqa: BLE001
        logger.warning("字幕获取异常：%s", e)
        return {"available": False, "error": f"获取字幕失败：{e}"}

    if res.get("available"):
        source = "B站 AI 字幕" if res.get("kind") == "ai" else "官方 CC 字幕"
        logger.info("字幕获取成功（%s，%d 字）", source, res.get("chars", 0))
        return {
            "available": True,
            "text": res["text"],
            "source": source,
            "lang": res.get("lang_doc") or res.get("lang", ""),
            "segments": res.get("segments", []),
            "srt": res.get("srt", ""),
        }

    logger.info("无可用字幕：%s", res.get("message", ""))
    return {"available": False, "error": res.get("message", "该视频暂无可用字幕")}

