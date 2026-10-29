# 🎬 Bilibili Subtitle Extractor — B站视频文本抽取

> **字幕优先，转写兜底** · 把 B站视频变成可检索的纯文本与图文笔记
>
> 手机扫码登录（一次性会话，用完即焚）→ 直取官方 CC / B站 AI 字幕 → 无字幕才下载音频本地转写。
> 拿到文本后可按章节自动抽帧，生成带图笔记；多讲合集可机械合并成一份。

---

## ✨ 它解决什么

- 刷到干货视频想记笔记，却要边看边暂停打字
- 追连载课程（多P/合集），每期手动提取太累
- 有的视频有字幕、有的没有，流程总是断
- 手抄 Cookie 又麻烦又危险，长期凭证一直挂着

现在一条命令解决，且**不留长期凭证**。

---

## 🚀 核心特性

### 认证：扫码 + 一次性会话

| 做法 | 说明 |
|------|------|
| 手机扫码登录 | 二维码由 B站下发，不经第三方，不要密码 |
| 独立设备会话 | 注销只影响这一份，手机和其它设备照常 |
| 用完即焚 | 任务结束调 logout 注销 + 覆写删除本地文件 |
| 自动过期 | 默认 30 分钟 TTL，忘了收尾也会失效 |

### 提取：字幕优先，转写兜底

```
① x/player/v2 按 cid 查字幕轨道        → 官方 CC 字幕，秒级
② AI 轨道子址为空 → ai/subtitle/search/stat 二次取址 → B站 AI 字幕
③ 都没有 → 串行下载音频 + faster-whisper 本地转写（离线）
```

旧版读 `view` 接口的 `subtitle.list` 基本恒空，是「明明有字幕却总去转写」的根因。

### 图文笔记：文本 → 章节 → 关键帧

```
① 用字幕做 TextTiling 分段（纯统计，不调模型）
② 每章在时间轴内取候选帧，PyAV 定点 seek + 解码单帧（0.04–0.09 秒/帧）
③ 64 位 dHash 去重（汉明距离 ≤6 判重），拼成带时间码的联系表
④ 人工/agent 从联系表挑图，写章节化底稿 note.md
```

**不调任何模型 API**：分章用统计、抽帧用本地 PyAV、成文由 agent 读底稿写。零成本、可复现。

### 多讲合并：只机械拼接

17 篇分篇笔记 → 一份完整笔记，**一个字不改**，只重编标题层级、给图片加讲次前缀。
`tools/verify_merge.py` 做四维校验（标题/段落/代码块/图），零丢失才放行。

---

## 📦 安装

```bash
git clone https://github.com/Xiaopeng212321414321413231/bilibili-subtitle-extractor.git
cd bilibili-subtitle-extractor

pip install -r requirements.txt
```

需要 **Python 3.10+**（开发与测试用 3.12.10）。

首次本地转写会自动下载 faster-whisper 模型（约 460 MB），国内建议：

```bash
set HF_ENDPOINT=https://hf-mirror.com     # Windows
export HF_ENDPOINT=https://hf-mirror.com  # macOS / Linux
```

不需要 ffmpeg、不需要 yt-dlp —— 抽帧走 PyAV，音频走 B站 DASH 流。

---

## 🔑 登录（推荐）

```bash
# ① 生成二维码
python src/bili_auth.py login --no-wait --qr-out "%TEMP%\bili_ephemeral\qr.png"
#    用手机 B站 App 扫这张图并确认

# ② 等待确认
python src/bili_auth.py wait --timeout 150

# ③ 用完注销（extract 默认自动执行）
python extract_bilibili.py --logout
```

查看状态：`python src/bili_auth.py status`

> 无人值守场景才用 `.env` 里的长期 Cookie（`--login env`）。它是长期凭证，安全性不如扫码。

---

## 🎯 用法

### 提取文本

```bash
# 先探测：哪些分P有字幕（3 秒，不下载任何东西）
python extract_bilibili.py BV1xxxxxxx --probe --keep-session

# 提取：有字幕抓字幕，无字幕自动转写
python extract_bilibili.py BV1xxxxxxx

# 只取字幕，无字幕就跳过
python extract_bilibili.py BV1xxxxxxx --no-whisper

# 任一分P无字幕就中止（避免误跑几小时转写）
python extract_bilibili.py BV1xxxxxxx --require-subtitle

# 指定分P / 多进程转写 / 出 SRT / 复用会话
python extract_bilibili.py BV1xxxxxxx --pages 1-10,15 --workers 3 --srt --keep-session
```

输出：

```
extracted/
├── BV1xxxxxxx.txt          # 单P正文
├── BV1xxxxxxx_P01.txt      # 多P正文
├── BV1xxxxxxx.json         # 元信息 + 每P结果
└── .subcache/              # 字幕缓存
```

正文首部会写明来源：`官方CC字幕` / `B站AI字幕` / `whisper-small 本地转写`。

### 生成图文笔记

```bash
# 一键素材包：字幕 → 自动分章 → 分批抽帧 → 底稿 + 联系表
python -m src.bili_notes BV1hbM9zrEwD --page 2

# 合集批量（10+ 分P 时末尾会自动跑合集体检）
python -m src.bili_notes BV1hbM9zrEwD --pages 1-17 --frames 5 --quality 80 --cols 5

# 只体检：查哪些分P的字幕可能串台
python -m src.bili_notes BV1hbM9zrEwD --audit

# 只要画面
python -m src.bili_frames grab assets/video/xxx.mp4 --times "00:42,06:16,13:53"
```

产物在 `notes/<BV>_P<NN>/`：`transcript.md`（章节化底稿）、`frames.json`、`contacts/`（联系表）、`frames/`。

### 合并成一份 + 出 HTML

```bash
python tools/merge_notes.py --bv BV1hbM9zrEwD          # 机械合并，零改写
python tools/verify_merge.py --bv BV1hbM9zrEwD         # 四维校验，退出码 0 才算过
python -m src.md_to_html notes/BV1hbM9zrEwD_ALL/note.md notes/BV1hbM9zrEwD_ALL/note.html
```

---

## 🧩 项目结构

```
bilibili-subtitle-extractor/
├── extract_bilibili.py       # 主脚本：认证→探测→抓字幕→转写→落盘
├── selftest_subtitle.py      # 自检（75 项，纯离线）
├── src/
│   ├── bili_auth.py          # 扫码登录 / 一次性会话 / 注销焚毁
│   ├── bili_subtitle.py      # 字幕直取 / WBI 签名 / 三层校验 / 缓存
│   ├── bilibili_client.py    # 元信息 / 音频下载
│   ├── bili_notes.py         # 图文笔记管线：分章 → 抽帧 → 底稿
│   ├── bili_frames.py        # DASH 视频轨下载 / PyAV 抽帧 / dHash 去重
│   ├── md_to_html.py         # md → 单文件 HTML（图片 base64 内嵌）
│   ├── zh_convert.py         # 繁→简转换
│   └── download_model.py     # 模型预下载
├── tools/
│   ├── merge_notes.py        # 多篇笔记 → 一份（机械合并）
│   └── verify_merge.py       # 合并保真校验
├── requirements.txt
└── .env.example
```

运行时会生成 `extracted/`、`notes/`、`assets/`、`models/`，这些已在 `.gitignore` 中忽略。

---

## 📋 依赖

| 依赖 | 用途 | 许可证 |
|------|------|--------|
| requests | HTTP（B站 API 全部自实现） | Apache-2.0 |
| python-dotenv | `.env` 读取 | BSD-3 |
| faster-whisper | 本地语音转写 | MIT |
| av (PyAV) | 视频轨解码 / 定点抽帧 | BSD-3 |
| qrcode + Pillow | 登录二维码渲染 | BSD / MIT-CMU |
| opencc-python-reimplemented | 繁→简（whisper 中文默认吐繁体） | Apache-2.0 |

**全部依赖都是 MIT 兼容的宽松许可证**，因此本项目可以放心用 MIT 分发。

> B站 API（view / playurl / player / WBI 签名）由 `src/bili_subtitle.py` 自己实现，
> 不依赖 `bilibili-api-python`（那个库是 GPL-3.0）。音频下载走 DASH 音频流、
> 抽帧走 PyAV，**不需要 yt-dlp 或 ffmpeg**。

---

## 🧪 自检

```bash
python selftest_subtitle.py               # 75 项离线逻辑（签名/轨道/合段/缓存/会话/焚毁/繁简）
python selftest_subtitle.py --audio a.m4a # 实测转写链路（不联网）
python selftest_subtitle.py --live BV1xx  # 额外做一次真实探测
```

全部离线，约 5 秒。CI 每次 push 都会跑。

---

## 📄 许可证

[MIT](LICENSE) © 2026 Xiaopeng212321414321413231

本项目刻意**不引入任何 GPL 依赖**，因此可以自由用于商业/闭源项目：

- B站 API 由 `src/bili_subtitle.py` 自行实现（含 WBI 签名），不用 GPL 的 `bilibili-api-python`
- 繁简转换用 Apache-2.0 的 `opencc-python-reimplemented`，不用 GPL 的 `zhconv`；
  代码里另内置了 392 字对照表，没装 opencc 也能跑

如果你在自己项目里引用本项目，保留 LICENSE 文件即可。

---

## ⚠️ 免责与使用边界

- 仅供**个人学习与研究**使用，请遵守 [B站用户协议](https://www.bilibili.com/protocal/licence.shtml) 与 `robots.txt`
- 请控制请求频率，不要用于批量抓取、镜像站点或再分发他人作品
- 提取出的字幕与截图版权归原 UP 主所有，**不要公开传播**
- 本项目不含任何解密、绕过付费/会员限制的功能；仅读取登录状态下自身可访问的内容

使用本项目产生的任何后果由使用者自行承担。
