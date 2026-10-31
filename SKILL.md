---
name: bilibili-extract
description: 提取 B站视频并做成图文笔记。当用户提供 B站链接、BV号、合集或搜索关键词，要求提取/转写/总结视频内容、生成图文笔记、导入知识库（IMA 等）时使用。手机扫码登录拿一次性会话（用完自动注销焚毁）。产物固定为「画面 + 文字」：下载原视频按章节抽帧 + 字幕（官方CC/B站AI字幕，拿不到才降级 faster-whisper 本地转写），抽帧不可跳过、没有只出文本的模式；支持多P批量、字幕预探测与三层防串台校验、点赞 <5 自动跳过。**一期一个 HTML、逐篇投递知识库**（图 base64 内嵌不断链），**不做合并**；含 28 讲合集拆成单篇的完整实战范例。
---

# bilibili-extract · 视频 → 图文笔记

> **这份文件是给 AI agent 读的说明书，不是给人读的 README。**
> 人看的版本在 [README.md](README.md)。
>
> 读这一份就够了，不需要再翻源码。里面写清了三件事：
> 1. **怎么调**（命令、参数、产物落在哪）
> 2. **边界在哪**（什么不能做、什么地方一定会出错、怎么绕）
> 3. **为什么这么设计**（避免你「优化」掉那些刻意保留的笨办法）
>
> 装了 Claude Skills / WorkBuddy 的环境可以直接把本目录当 skill 用；
> 其它环境把它当项目内的 agent 指南读即可。
>
> **本文件是主版本。** 如果同时在个人 skill 目录放了一份，以这里为准，
> 改动后记得同步过去，别让两边分叉。

把 B站视频（单视频 / 多P / 合集）转成文本，供知识卡片化、笔记归档使用。

两条铁律：
1. **能手拿字幕就不下载音频。**
2. **不长期保存 Cookie** —— 扫码拿一次性会话，任务结束就注销焚毁。

## 工具位置

```
C:\Users\32352\Desktop\ai软件\git\bilibili-subtitle-extractor
```

| 文件 | 作用 |
|---|---|
| `extract_bilibili.py` | **主执行脚本**：认证→探测→抓字幕→缺口转写→落盘 |
| `src/bili_auth.py` | **扫码登录**：二维码、一次性会话、注销、焚毁 |
| `src/bili_subtitle.py` | 字幕直取：按 cid 查轨道、AI 字幕二次取址、缓存 |
| `src/bili_notes.py` | **图文笔记管线**：字幕取文 → 自动分章 → 按章抽帧（必经）→ 底稿+联系表 |
| `src/bili_frames.py` | **取画面**：DASH 视频轨下载、PyAV 定点抽帧、dHash 去重、联系表 |
| `src/md_to_html.py` | 笔记 md → 单文件 html（图片 base64 内嵌，浅色排版） |
| `src/youtube_client.py` | **YouTube 取素材**：自研 InnerTube + 分片下载，只依赖 curl_cffi（见「YouTube」节） |
| `tools/split_notes.py` | **分篇 md → 一期一个 HTML**：命名带 P 号、顺手记字节数（详见「交付」节） |
| `tools/cleanup.py` | **清中间产物**：视频/音频/候选帧，默认 dry-run，见「删东西」节 |
| `tools/safe_delete.py` | **删除闸门**：任何删除都先过它，六道保护挡住误删系统目录 |
| `tools/notes_from_local.py` | **本地视频入口**：绕过所有平台 API，mp4 + srt 直接产出同构素材（详见「YouTube」节） |
| `tools/ima_put.py` | **传文件到 IMA**：create_media 只给 COS 凭据，本体靠它 PUT（详见「投递到知识库」节） |
| `tools/shrink_html.py` | 压 HTML：内嵌图重压，塞进 IMA 的 10MB 上限（单篇用不到，见「不做合并」） |
| `tools/merge_notes.py` | ⚠️ **不参与流程**：多篇合一份的旧工具，仅用户明确要单文件时用（见「不做合并」节） |
| `tools/verify_merge.py` | ⚠️ 同上，合并后做四维校验用 |
| `src/bilibili_client.py` | 元信息 / 音频下载；`get_subtitles()` 委托 `bili_subtitle` |
| `src/zh_convert.py` | 繁→简转换（opencc 优先，内置 392 字兜底；不用 zhconv，它是 GPL） |
| `selftest_subtitle.py` | 自检（75 项，离线可跑） |
| `notes/<BV>_P<nn>/` | **主产物**：`transcript.md` / `frames.json` / `frames/` / `contacts/` / `meta.json` |
| `extracted/` | 运行记录：`<BV>.json`（每P状态/来源/字数/帧数）|
| `assets/video/` `assets/frames/` | 视频流与帧缓存（按 `<BV>_<cid>` 命名，重跑不重复下载） |
| `extracted/.subcache/` | 字幕缓存，重跑 0 请求 |

Python：`C:\Program Files\Python312\python.exe`（`-m src.bili_notes` 需在上述仓库根目录下执行）

## 认证：扫码登录，用完即焚

B站 AI 字幕**必须登录**才下发。不要手抄 SESSDATA（长期凭证、明文落盘、容易忘记删），走扫码：

```bash
cd "C:\Users\32352\Desktop\ai软件\git\bilibili-subtitle-extractor"

# ① 生成二维码（图落到临时目录）
python src/bili_auth.py login --no-wait --qr-out "%TEMP%\bili_ephemeral\qr.png"

# ② 把这张 PNG 给用户扫（agent 用 present_files 展示），手机 B站 App 扫码确认

# ③ 等待确认（二维码约 3 分钟内有效；超时/失效后自动生成第二枚再等一轮，共两轮）
python src/bili_auth.py wait --timeout 150
```

> `qr_login` 默认 `rounds=2`：第一枚二维码超时或失效后**自动重新生成一枚**再等一轮，
> 两轮都没扫上才算失败。扫码超时太常见（手机不在手边、确认页加载慢），
> 不该让整个任务直接失败。

成功后凭证写在 `%TEMP%\bili_ephemeral\session.json`（临时目录，不在仓库里），
默认 **30 分钟 TTL**；到期、或任何一次读取发现过期，**当场覆写删除**。

**为什么安全**

- 不碰浏览器、不要密码；二维码由 B站下发，不经第三方
- 拿到的是**独立设备会话**，注销只影响这一份，手机和其它设备照常登录
- 结束调 `passport.bilibili.com/login/exit/v2` 注销服务端会话，再覆写删除本地文件
- 即使用户忘了收尾，TTL 到期也会自动失效并删文件

**收尾**（extract 脚本默认自动执行，也可手动）：

```bash
python extract_bilibili.py --logout     # 注销服务端会话 + 焚毁本地
python src/bili_auth.py status          # 查看当前会话（只显示状态，不打印凭证）
```

**注销失败怎么办**：网络抖动时 `login/exit/v2` 可能超时。此时脚本**不会**假装成功——
会保留 `pending_revoke.json` 待办并告警，下次 `--logout` 自动补注销。
若仍失败，让用户立即在手机 B站 App「设置 → 退出登录」作废全部会话，这是最终兜底。

### agent 执行要点

| 场景 | 做法 |
|---|---|
| 单次提取 | `login --no-wait` → present_files 展示二维码 → `wait` → 再跑 extract |
| 用户说「扫好了」 | 直接 `wait`（沿用上次二维码，未过期时很快出结果） |
| 批量多轮 | 加 `--keep-session` 复用同一会话，全部跑完再 `--logout` |
| 无人值守 / 定时任务 | 只能用 `--login env`（`.env` 长期 Cookie），安全性差，需用户明确同意 |

`--login` 取值：`auto`（会话→.env→扫码，默认）、`session`、`env`、`qr`、`none`。
默认行为：**跑完自动注销 + 焚毁**；`--keep-session` 保留。

## 标准流程

### 1. 先探测（3 秒，不下载任何东西）

```bash
python extract_bilibili.py BV1xxxxxxx --probe --keep-session
```

```
P01 | 官方CC字幕(中文（中国）)      |  34:47 | 一、正大杯究竟是什么比赛？
P02 | B站AI字幕(中文（自动生成）)   |  13:32 | 二、组队分工与时间规划
P05 | 无字幕                        |  12:45 | 五、问卷与抽样设计
```

看到这张表就知道要花多久：有字幕的分P 秒级完成，无字幕的才进转写。

### 2. 提取（唯一模式：图文素材）

```bash
# 默认：字幕 → 分章 → 下载原视频抽帧 → 素材包（抽帧不可跳过）
python extract_bilibili.py BV1xxxxxxx

# 每章多取几帧 / 更高清晰度
python extract_bilibili.py BV1xxxxxxx --frames 8 --quality 80

# 无字幕就跳过（不转写）
python extract_bilibili.py BV1xxxxxxx --no-whisper

# 任一分P无字幕就中止，避免误跑几小时转写
python extract_bilibili.py BV1xxxxxxx --require-subtitle

# 指定分P / 多进程 / 保留会话给下批用
python extract_bilibili.py BV1xxxxxxx --pages 1-10,15 --workers 3 --keep-session

# 批量
python extract_bilibili.py BV1xx BV2yy BV3zz
```

**必须后台运行**（`run_in_background: true`）。有字幕的批次，光是字幕阶段几秒到几十秒；
但**抽帧要下原视频**，一个 1 小时的分P约 1.5–3 分钟（1080P 下载 + 按章取帧）。

### 3. 读结果

产物在 `notes/<BV>_P<nn>/`：

```
transcript.md      章节化底稿（每章时间范围 + 逐句原话，带时间戳）—— 成文只许从这里取事实
frames.json        每章候选帧清单（时间戳/路径/尺寸/哈希）
frames/c01..       按章分目录的 jpg
contacts/sheet_1.jpg   联系表，每格标「章-序号 + 时间码 + 章名」
meta.json          来源、耗时、章节、联系表索引
```

底稿首部会写明文字来源：`官方CC字幕` / `B站AI字幕` / `whisper-small 本地转写`。

来源字段写明 `官方CC字幕` / `B站AI字幕` / `whisper-small 本地转写`；转写内容在卡片里要标注「机器转写，可能有错」。

### 4. 收尾：清中间产物（别跳过）

全部 `note.md` 写完之后跑一次：

```bash
python tools/cleanup.py                # 先看一眼各能回收多少
python tools/cleanup.py --all --yes     # 确认后清（进回收站，可恢复）
```

它只删「对应分P的 note.md 已写完」的原视频、临时音频、`note.md` 没引用的候选帧，
成品和转写缓存一律不动。详见「产物落盘与生命周期」节。

**不清的后果是实打实的**：两个合集（45 讲）不清就是 9.4 GB，其中 7.2 GB 是废料。

交付时报告里带一句「本次产生 X MB，已回收 Y MB」，用户才知道磁盘在被什么吃掉。

## 机制说明（为什么这样快）

| 环节 | 做法 |
|---|---|
| 元信息 | `x/web-interface/view` 拿 aid/cid/pages/stat |
| 字幕轨道 | `x/player/v2?cid=&bvid=`（**老接口优先，最稳**）；空则试 WBI 版 |
| AI 字幕取址 | 新版 AI 轨道 `subtitle_url` 为空，须再请求 `x/player/v2/ai/subtitle/search/stat?aid=&cid=` |
| 轨道优选 | 中文人工 > 中文AI > 其他人工 > 其他AI |
| 字幕内容 | 下载 `subtitle_url` 的 JSON，取 `body[].from/to/content` |
| **覆盖度校验** | 末段 `to` / 分P时长必须落在 **0.70 ~ 1.20**。低于 = 只覆盖开头，高于 = 拿到别的（更长的）视频的字幕。越界则换轨道重试，全部越界再**重查一次轨道**（B站 偶发串台，重查通常就对），仍不行则**不写缓存**并回落转写 |
| **内容交叉校验** | 拿 `x/web-interface/view/conclusion/get`（该视频自己的 AI 总结，WBI 签名免登录）与字幕正文算词汇重合度（英文词 + 中文二元组，短边做分母），**< 0.12 判定串台**。这一层专门抓「长度恰好相仿、覆盖度查不出」的错字幕（实测：正确 0.35/0.38，串台 0.04）。可用 `--no-verify` 关闭 |
| 缓存 | `extracted/.subcache/<BV>_<cid>.json`，同分P第二次 0 请求；读到坏缓存（覆盖不足）当场删除重取 |
| 并发 | 探测/抓取默认 4 线程；转写才用多进程 |

旧版 `info["subtitle"]["list"]` 基本恒空 —— 这是过去「明明有字幕却走了转写」的根因。

## 转写降级（仅当确实无字幕）

- **AI 字幕提取失败自动退避重试（2026-10-31）**：提取失败 → 等 1.5s → 重查
  轨道重试 → 再失败 → 等 3s → 再试 → 仍失败才走 whisper（用户指定的节奏）。
  覆盖度/内容校验不过同样走这个重试，不做区分——重查轨道后内容可能就对了。
- **音频下载**：`x/player/playurl?fnval=16` 取 DASH 音频流（选 `bandwidth` 最高），
  `baseUrl` + `backupUrl` 多源冗余，流式存 `.m4a`（先写 `.part` 再 `os.replace`）。
  **不需要 yt-dlp**（依赖清单里已移除，别再加回来）。早期版本借用 `bilibili-api` 的
  `get_download_url`，但它自带网络栈、无视系统代理，代理环境下必挂，已改成用本模块 session。
- **凭证**：下载音频用「一次性扫码会话」优先，`.env` 长期 Cookie 兜底——扫码登录、`.env` 为空也能转写
- **串行下载音频**：bilivideo.com CDN 对并发下载限流（SSL UNEXPECTED_EOF），必须逐个来
- **并行转写**：`--workers N` 走多进程；若环境不支持 spawn（会 BrokenProcessPool），自动退回串行，不会整批失败
- **输出简体**：whisper 对中文默认吐繁体，已统一转简体（`zhconv`，缺失时用内置表兜底）
- 模型缓存：`models/`（首次运行自动下载约 460MB，已配 hf-mirror）
- 长视频（>10min）不要用百度短语音 API，一律本地 whisper
- 离线验证转写链路（不联网）：`python selftest_subtitle.py --audio 任意音频.m4a`

## 过滤规则（用户硬性要求）

- 读 `meta.stat.like`，**点赞 < 5 直接跳过**（`--ignore-like-filter` 可强制）
- 通过的在卡片里标注 `UP主 + 点赞数`
- 充电专属视频字幕与音频都不可得，直接跳过

## 转知识卡片

- 读正文 → 按主题归纳（忠于原意，不添料）
- 无字幕课程合集：先出「课程地图」卡片交付大纲，再按章节优先级转写
- 卡片归档到对应学科文件夹，每张卡片一个 md + frontmatter
- 来源标注：`B站视频 <BV号>（UP主：xxx，赞：xx，来源：AI字幕/机器转写）`

## 图文笔记（画面 + 文字，缺一不出）

> **做合集先看文末「实战范例 A」**：那里有 28 讲拆成单篇的完整命令序列、单篇模板、真实故障处理和
> 成本账，照着执行即可，不要临场设计流程。

**硬规则（2026-10-29 用户明确要求，不再是可选项）：产物必须是「画面 + 文字」。**

以前的做法是「先拿字幕，有字幕就出一份 txt 收工」，被反馈为不对 —— 文字再准也没有画面，
既不能验证字幕是否说错，也做不出图文笔记。现在：

- **抽帧是必经步骤**，没有任何开关能跳过（`--no-frames` 已删除）。
- 抽帧失败 → 该分P**报错**，不许退化成「只有文字」的半成品。
- 纯文本输出模式（`.txt` / `.srt`）已删除，只保留图文素材包。

执行顺序上仍是「先字幕分章 → 再按章抽帧」，因为**抽帧时刻依赖章节边界**（均匀切分会把
一页 PPT 切成两张废图）。但语义变了：字幕快不是跳过视频的理由，只是让抽帧更准的输入。

```bash
# 默认：下载原视频 → 抽帧 → 取字幕 → 分章 → 素材包（不调任何模型 API）
python extract_bilibili.py BV1hbM9zrEwD --pages 1-17 --frames 5 --quality 80

# 等价写法（参数一一对应）
python -m src.bili_notes BV1hbM9zrEwD --page 2
python -m src.bili_notes <url> --chapters notes/xxx_chapters.json --frames 8   # 用人工章节
python -m src.bili_notes BV1hbM9zrEwD --page 2 --no-whisper                    # 无字幕直接报错

# 只要画面（本地视频或直接给 BV）
python -m src.bili_frames download BV1hbM9zrEwD --page 2 --quality 64
python -m src.bili_frames grab assets/video/xxx.mp4 --times "00:42,06:16,13:53"
python -m src.bili_frames grab <BV号> --page 2 --even 8 --frames 6
```

| 环节 | 做法 |
|---|---|
| 文本 | `BiliSubtitleClient.fetch()`：带覆盖度 + 内容亲和度双重校验、命中缓存 0 请求；不可用且未禁用才走 whisper |
| 分章 | **纯统计不调模型**：相邻话语块的词汇衔接（中文二元组+英文词余弦）掉到局部极小处切，块首是「接下来/总结…」等话语标记时加分，再把边界吸附到片段级起句点；超过 12 分钟的章自动二次切 |
| 画面源 | `playurl fnval=16` 取 **DASH 视频轨**（不含音轨，体积最小）；`--quality 64`=720P / `32`=480P，未登录通常只给 480P |
| 抽帧 | PyAV 直接 `seek+解码单帧`（**0.04–0.09 秒/帧**），不预压缩整片、不依赖 ffmpeg CLI/cv2/skimage |
| 去重 | 64 位 dHash，汉明距离 ≤6 判重（同一页 PPT 含光标移动也能判出来） |
| 分批落盘 | 每章抽完立刻写 `frames.json`，长视频中断可续跑；视频与帧都按 `<BV>_<cid>` 缓存 |
| 联系表 | `contacts/sheet_N.jpg`，每格标 `章-序号 + 时间码 + 章名`，一张图看清哪些是有效画面 |

产物结构（`notes/<BV>_P<NN>/`）：

```
transcript.md   章节化底稿：每章时间范围 + 逐句原话（带时间戳）——成文只许从这里取事实
frames.json     每章候选帧清单（时间戳/路径/尺寸/哈希）
contacts/       联系表
frames/c01..   按章分目录的 jpg
meta.json       来源、耗时、章节、联系表索引
```

**选帧必看（实测踩过的坑）**：

- **「一章一图」不成立**：讲者长时间停在同一页 PPT，抽出来是同一张图。有效配图数量由
  「画面实际变化次数」决定，不是章节数（P02 11 章只挑得出 8 张不重复的有效图）。
- **半成品帧**：代码类视频常拍到「正在打字」的中间态（如 `return` 尚未写完）。自动去重和
  打分都识别不了，必须人工/agent 放大核对后再定图。
- 480P 对 PPT 大字号够读，小字代码建议登录后取 720P。
- 帧里通常带**烧进视频的字幕**（下载的是原始流，不是播放器叠加），这是正常的。

**不调模型**：分章用统计、选图靠人看图、成文由 agent 读底稿写。零成本、可复现，
不依赖任何厂商的 key 或配额。

### 每一讲就是最终交付物，单篇必须自洽

**产物就是「一期一个 HTML」，没有大合订本这种形态。** 所以每一讲都要当成一篇能独立阅读的
文章来写，它自己就是交到读者手里的那一份：

```
一句话总结 → 核心结论 → 带图小节（每图带时间码说明）→ 关键数据与术语 → 附：这几张图是怎么来的
```

判据很简单：**读者只打开这一篇，能不能看懂这一集在讲什么、关键结论是什么、图在说明什么。**
不能就是没写完，别指望后面还有什么环节来补。

单篇的自洽标准就一条：读者只打开这一篇，能看懂这一集在讲什么、结论是什么、图在说明什么。
别指望后面还有什么环节来加工它 —— 这一篇交出去就是终稿，没有"等下合并再补"这回事。

### 批量做合集时必跑：合集体检

一次跑 10+ 个分P，一定会遇到「某一P的字幕不是这一P的」。B站 的 AI 总结（`conclusion/get`）
只覆盖部分分P，拿不到时 `fetch()` 的第二层内容校验会**整个跳过**，只剩覆盖度把关——
如果串台来的字幕长度正好接近，「覆盖度」根本拦不住。

```bash
python -m src.bili_notes BV1hbM9zrEwD --audit          # 只看体检，不重跑任何分P
python -m src.bili_notes BV1hbM9zrEwD --pages 1-17     # 批量末尾自动体检
```

体检用「同一合集其他分P的语言分布」当参照：同一老师讲同一门课，词表高度重叠
（实测亲和度 0.57–0.84），串台字幕会断崖式掉到 0.3 以下。阈值 0.45。

实战记录（BV1hbM9zrEwD，17P）：P14 拿到的是另一个 20 分钟视频的字幕（末段恰好 1255s，
覆盖度 100% 通过），体检亲和度 **0.270**，其余 16P 最低 0.567 —— 精准揪出。

**串台的处理顺序**：① `--pages N --force` 重取（可能是接口偶发）→ ② 仍未登录/取不到就
删掉（挪走留证）该 cid 的缓存 json，靠 whisper 兜底转写。删除前先 `mv` 到
`extracted/.subcache/.bad/<cid>.crosstalk.json`，不要 `rm`。

#### 覆盖度拦不住的第四类串台（2026-10-30 实测，BV1pYe76VEFm）

30 组答辩里 5 个分P 拿到了**别的视频**的字幕：P05/P13 是同一个旅游 vlog（两组正文逐字相同）、
P24 是英雄联盟解说、P25 是贵阳美食 vlog、P27 是「学习强国」测评。这 5 个的覆盖率是
**86%～94%**，全部落在 0.70–1.20 区间内，`--audit` 也查不出来——因为 `conclusion/get`
对这些分P 拿不到该视频自己的 AI 总结，第二层内容校验被整段跳过。

**只能靠人眼扫**，特征非常明显：**正文里一个研究词汇都没有**（抽样/样本/问卷/信度/模型/
显著），通篇 vlog 或解说口吻。所以批量跑完必做一件事：

```bash
# 每个分P的底稿里，统计研究词汇命中数；0 命中 = 高度可疑，必须人工看
for d in notes/<BV>_P*/; do
  n=$(grep -cE "样本|问卷|抽样|信度|效度|显著|回归|模型" "$d/transcript.md");
  [ "$n" -lt 2 ] && echo "可疑: $d ($n)";
done
```

#### 转写结果会覆盖串台字幕（已修，2026-10-30）

`build_note()`（`src/bili_notes.py::_run`）**自己会再 fetch 一次字幕**，不认命令行层面
已经转好的文本。所以「把缓存挪走 + `--no-subtitle` 重跑」之后，`notes/xxx/transcript.md`
里看到的仍是串台字幕——看上去像没修好，其实是底稿阶段又被字幕盖回去了。

现在 `extract_bilibili.py` 会把 `force_whisper=True` 透传给 `build_note`（判据是
`item["source"]` 以 `whisper` 开头），底稿阶段直接跳过字幕。

配套加了**转写磁盘缓存**：`<音频>.whisper-<模型>.json`（与音频同目录，默认在系统临时目录
`bilibili_audio/`）。`_w_run`（批量并行转写）和 `transcribe_local`（单P兜底）共用这份缓存，
所以「批量转写 → 生成图文素材」不再解码两遍；重跑同一分P 是秒级完成。日志出现
`转写缓存命中：N 段 / N 字（免解码）` 即为命中。

#### 后台跑一定要用工具的托管后台

**不要用 `nohup ... &`**。实测：命令返回后进程会在几十秒内被杀，日志停在
`下载音频: xxx.m4a` 或 `Processing audio with duration ...` 一行不动，看起来像卡死，
其实是进程没了（`Get-CimInstance Win32_Process` 里查不到 python 子进程）。
必须用工具自带的 `run_in_background: true`；`| tail -N` 会让输出全部缓冲到进程结束，
想看进度就别接 tail，用 `>` 重定向到日志文件再 `tail`。

### 交付格式：HTML 是唯一最终产物，且不加装饰

**`note.html` 是交付物，`note.md` 只是写作过程中的源文件。**

**每期视频只生成一个 HTML —— 不多、不少（硬规则，2026-10-31）。**

- 一个分P = 一个 `note.md`（源）+ 一个 `note.html`（交付）。就这两个文件。
- 不生成：额外的 txt、大纲 md、草稿 md、带彩色样式的第二个 html 版本。
- 交付时**只交 note.html**，note.md 留在本地当源文件，不投递。
- 用户要的是「这一讲的笔记」时，交付物列表里就是一行一个 html，别把中间文件
  一并丢给用户。

HTML 之所以是唯一格式：**它是目前唯一能「文字 + 图片 + 排版」塞进一个文件里的轻量格式**。
md 的图是相对路径，一挪位置或在知识库里打开就断成一排空白；HTML 把图 base64 内嵌，
发给谁都不坏，体积才 0.5~2 MB。

#### 刻意不加装饰（默认如此，除非用户要）

- **不加彩色强调**。`md_to_html.py` 的 CSS 只有中性灰阶，2026-10-31 把最后一处彩色
  （引言块左边框的墨绿 #2f6f5e）也去掉了。
- **不加 emoji、不加图标、不加渐变卡片、不加彩色标签**。
- 重点靠**字号、粗细、留白**立，不靠颜色 —— 一页里三处标红等于什么都没强调。

两个实打实的理由：

1. **装饰吃 token 和体积**。见过不少工具生成的笔记，图没几张、样式代码几十 KB。
2. **颜色替读者做判断**。读者该自己决定哪里重要，而不是被高亮牵着走。

用户明确要求时才加（「做成深色主题」「重点用颜色标出来」），并且只改那一份，
不要把临时样式写回 `md_to_html.py`。

### 交付：一期一个 HTML，逐篇投递

**不合并。** 每讲生成一个独立 HTML，图片 base64 内嵌，单独传进知识库。
读者在知识库里看到的是「P01/P02/…」一排条目，点哪一集看哪一集。

```bash
# 一条命令搞定整个合集：每讲一个 HTML，命名 Pnn-<标题>.html
python tools/split_notes.py --bv BV12omoB4ExF

# 单篇也行（分篇 md 已经写完的时候）
python -m src.md_to_html notes/<BV>_P01/note.md notes/<BV>_SPLIT/P01-<标题>.html
```

`split_notes.py` 会读每个分P的 `meta.json` 取标题（去掉「第N章-XX-」前缀）、
生成 `notes/<BV>_SPLIT/` 下的独立页面，并写出 `_sizes.json`（文件名 → 字节数），
后面 create_media 直接用，不用逐个再算。有分P缺 `note.md` 会列出来并以退出码 1 提醒。

**为什么发 HTML 不发 md**：md 里的图是相对路径，进了知识库就全断（读者看到一片空白方框）。
HTML 把图 base64 内嵌，图文一体，浏览器里直接看。

**体积**：单篇通常 0.5–1.9 MB（10 张图上下），远低于 IMA 的 10 MB 上限，**不要压图**，
保持抓帧原清晰度 —— 代码截图压糊了就没价值。单篇永远不会碰到上限，压图这条路可以忘掉。

### 投递到 IMA 知识库

IMA 的连接器只提供增/查，**没有删除接口**，传错了只能让用户在界面上手删。
所以文件名和目录一次定对，别指望改。

```bash
# 三步：create_media（MCP）-> 传 COS（脚本）-> add_knowledge（MCP）
```

**① `mcp__ima-mcp__create_media`** —— 只登记元数据，返回 `media_id` + 一份临时 COS 凭据：

```
knowledge_base_id: "<知识库 id>"
content_type: "text/html"      # 必须和扩展名一致，别用 octet-stream 兜底
file_ext: "html"
file_name: "P01-xxx.html"
file_size: <精确字节数>          # 服务端据此校验上限，写错会被拒
```

**② 传本体** —— 凭据交给 `tools/ima_put.py`，凭据走 stdin 不留 shell history：

```bash
cat <<'JSON' | "/c/Program Files/Python312/python.exe" tools/ima_put.py
{"file": "notes/<BV>_SPLIT/P01-xxx.html",
 "key": "<cos_key>",
 "cred": {"secret_id": "...", "secret_key": "...", "token": "...",
          "bucket_name": "ima-share-kb-1258344701", "region": "ap-shanghai"}}
JSON
```

看到 `OK <文件名> ... ETag=...` 才算成功。

**③ `mcp__ima-mcp__add_knowledge`** —— 入库：

```
knowledge_base_id: "<知识库 id>"
folder_id: "<文件夹 id>"          # 学科目录靠它，不是靠 knowledge_base_id
media_id: "<①的 media_id>"
duplicate_name_strategy: "DUPLICATE_NAME_STRATEGY_REPLACE"
```

#### 两个必须知道的 IMA 事实

- **用户说的「知识库」往往是知识库下的「文件夹」。** `get_knowledge_base_list` 只会列出
  2~3 个真知识库（如「小彭新的个人知识库」），而「高等数学」「人工智能」「蓝桥杯py」
  这些学科目录都是它下面的 `folder`。先用 `get_knowledge_list(knowledge_base_id, limit)`
  看根目录下的 folder 列表，拿到 `folder_id` 再传 —— 传错层级比传错文件还难救。
- **html 类型上限 10 MB（`media_type 20`）**，超了直接报
  `文件大小 xxx 超出 media_type 20 的上限 10485760`。单篇碰不到，合并版必撞。

#### 大批量投递用并行子代理

28 篇串行做，光 create_media 返回的凭据 JSON 就能吃掉几万 token 上下文。
把「四步流程」写成一份 SOP 文件，每个子代理认领 4~6 篇、读 SOP 执行，
在 prompt 里列清单即可，别把流程重复写进每个 prompt。

### YouTube（2026-10-31 起自研实现，不依赖 yt-dlp）

```bash
python -m src.youtube_client "https://www.youtube.com/watch?v=XXX"
```

只做探测（不写文件）：标题、时长、观看数、字幕轨、原生章节、选流结果。
写代码时 `from src.youtube_client import extract, pick_streams, get_subtitles, download`，
接口与 `bilibili_client` 对齐，字幕 segment 同样是 `{from,to,text}`，
后面 `auto_chapters` / 写笔记 / 出 HTML / 传知识库**一个字都不用改**。

**只需要一个依赖：`curl_cffi`（1MB）。** 曾经用 yt-dlp（19.8MB），把它换掉是因为
摸清了 InnerTube player API —— 抄清楚了就不需要那个大包。**装之前连续 10 次全败**
（`SSL: UNEXPECTED_EOF_WHILE_READING`），装完立刻通：**这不是网络问题，是 TLS 指纹
（JA3）被识别**。所以别用裸 `requests` 打 YouTube，必须 curl_cffi 的 impersonate。

#### 自己实现的关键：visionos 客户端 + 完整上下文

整个流程只有三步：GET `/watch` 页拿 `INNERTUBE_API_KEY` 和 `VISITOR_DATA` →
POST `/youtubei/v1/player` → 直链和字幕 URL 直接下。

**成败全在客户端上下文完不完整。** 少一个字段就从 `OK` 掉到 `UNPLAYABLE` 或
`LOGIN_REQUIRED`，而且不告诉你缺谁。实测要凑齐这些：

```python
{"clientName": "VISIONOS", "clientVersion": "1.02",   # 写 1.1 会 404
 "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
 "osName": "visionOS", "osVersion": "26.5.23O471",
 "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) ... Safari/605.1.15",
 "hl": "en", "timeZone": "UTC", "utcOffsetMinutes": 0}   # ← 这两个最容易漏
```

header 里还必须有 `Origin: https://www.youtube.com`、`X-YouTube-Client-Name: 101`、
`X-YouTube-Client-Version: 1.02`、`X-Goog-Visitor-Id`（从 watch 页取）。

这个 client 的好处：**不要求 po_token，返回的直链不带 `n` 参数** —— 省掉了签名解算
和限速反混淆两大块，这也是能不用 yt-dlp 的根本原因。

#### 下载必须分片并发

单连接被限速到约 **150 KB/s**，381MB 的 1080p 要下 45 分钟；切成 8 段并发立刻到
**2.6 MB/s**，整片 2.5 分钟。这不是优化，是「能不能用」的差别。`download()` 已内置
（`threads=8`，小于 4MB 的文件自动走单连接，`.part` + Range 断点续传）。

#### 选流：同分辨率优先小体积

1080p 同一个视频，`av01` 是 381MB、`avc1` 是 965MB，差 2.5 倍。抽帧只取几帧，
编码格式无所谓（PyAV 内置 dav1d 能解 AV1），所以同分辨率下按 `filesize` 从小到大选。

**字幕的坑：中文轨必然 429。** YouTube 的中文自动字幕是**机器翻译**（由英文轨实时翻），
请求它等于让对方现翻一遍，一请求就 `HTTP 429 Too Many Requests`；英文原轨（`en`）
却能正常拿到。所以：

- 英文视频：直接取 `en` 的 `json3`，1.28MB / 3000+ 条，秒下；
- 中文视频：`zh-Hans` 大概率 429，要么等一段时间重试，要么走 whisper 转写视频音轨。
- 顺序上**先试人工字幕（`subtitles`），再试自动字幕（`automatic_captions`）**，
  自动字幕的专有名词错得明显更多，`source` 里必须写明。

**体积比想象小。** 84 分钟的视频，1080p（av01 编码）只有 **381MB**，720p60 才 192MB。
不要因为"以为要下几个 G"就去降清晰度 —— 抽帧要的就是清晰度。

**完整可跑的序列**（就是 2026-10-31 跑通的那套）：

```bash
# 1) 取元信息（含字幕轨、章节、直链）
python -m src.youtube_client "https://www.youtube.com/watch?v=XXX"

# 2) 代码里取字幕 + 下视频流（都走同一个 info，不用再敲命令）
#    info = extract(URL) -> sub = get_subtitles(info)
#    download(pick_streams(info)["video"], "assets/video/<slug>.mp4")

# 3) 字幕存成 srt，和视频一起喂统一入口
python tools/notes_from_local.py --video assets/video/<slug>.mp4 \
       --subtitle notes/<slug>/subtitle_en.srt --chapters notes/<slug>/chapters.json \
       --name <slug> --frames 5 --cols 5
```

**英文长视频必须手写章节。** `auto_chapters` 是给中文设计的：它取章首那句当标题，
而英文口语开头全是 `So` / `Um` / `Okay` / `Do you want to`，切出来完全不能看
（实测 20 章里有 6 章标题是废的）。做法是按时间采样读内容、自己归纳，
写成 `chapters.json` 用 `--chapters` 喂进去 —— 这一步没法省。

**还是不通怎么办**：走**本地文件模式**，一条命令，零新依赖：

```bash
python tools/notes_from_local.py --video in.mp4 --subtitle in.srt --name <slug>
# 没有字幕文件就省掉 --subtitle，自动走本地 whisper
```

产出与 `src.bili_notes` **完全同构**（transcript.md / frames/ / contacts/ /
frames.json / meta.json），后面的写笔记、`md_to_html`、`split_notes`、传知识库
一个字都不用改 —— 这条链路**不碰任何平台 API**，绕开反爬、下载条款和网络波动。

别为了「让它通」去堆代理池/改 DNS —— 那是另一个工程，不属于这个 skill 的职责。

**章节优先用原生**：UP 主自己划的章节比我们按字幕统计硬切准得多，
`chapters()` 有就一定要用，没有才回落 `auto_chapters`（或手写）。

### 产物落盘与生命周期（2026-10-31 新增，这是必读）

**这个 skill 是磁盘杀手。** 实测跑完两个合集（28 讲 + 17 讲）堆了 **9.4 GB**，
其中 7.2 GB 是纯中间产物，一次没清过。用户为此专门问过「会不会搞崩电脑」。

#### 每个东西能活多久

| 路径 | 谁生成 | 性质 | 何时可删 |
|---|---|---|---|
| `assets/video/<BV>_<cid>.mp4` | 抽帧前下载 | **临时** | 该分P的 `note.md` 写完后 |
| `assets/frames/` | 抽帧中间图 | **临时** | 同上 |
| `notes/<BV>_P<nn>/frames/` | 候选帧 | 半成品 | 只留 `note.md` 引用的，其余可删 |
| `notes/<BV>_P<nn>/contacts/` | 联系表 | 半成品 | 选完帧就能删 |
| `notes/<BV>_P<nn>/_tmpview/` | 放大看图时截的 | **临时** | 随时 |
| `notes/<BV>_P<nn>/note.md` | agent 写 | **成品** | 永不 |
| `notes/<BV>_P<nn>/transcript.md` | 脚本生成 | **底稿** | 永不（重写笔记要用） |
| `*.whisper-<model>.json` | 转写缓存 | **缓存** | 永不（删了要重转几十分钟） |
| `notes/**/*.html` | 交付物 | **成品** | 永不 |
| `models/` | whisper 模型 | 依赖 | 永不 |
| `temp/bilibili_audio/*.m4a` | 转写用音频 | **临时** | 转写完即可 |
| `audio_b/` `audio60/` `audio_tmp/` | **legacy 旧脚本** | **纯废料** | 随时，见下 |

#### 三条铁律

1. **临时产物只能落在 `assets/` 或系统 temp 目录**。不许在项目根新建 `audio_xxx/`
   这种目录 —— `audio_b`(2.7GB)、`audio60`(337MB)、`audio_tmp` 就是 v1 脚本这么干
   留下的，在 `.gitignore` 里躺了几个月没人发现，最后被当成本次统计的盲区漏掉 3 GB。
2. **能落 temp 就别落项目内**。临时音频、中间转换文件一律 `tempfile.gettempdir()`。
3. **跑完就清，别攒**。攒到 9 GB 再清，中间任何一次统计都可能漏。

#### 每次任务收尾必做

```bash
python tools/cleanup.py        # 只预览：视频/音频/候选帧/临时图各多少
python tools/cleanup.py --all --yes   # 确认后清（进回收站）
```

**不看一眼不算交付完成。** 报告里带一句「本次产生 X MB，可回收 Y MB」，
用户才知道磁盘在被什么吃掉。

#### 统计别只盯几个熟目录

这次漏掉 3 GB 的直接原因：只统计了 `assets/video`、`notes/`、`temp/bilibili_audio`，
没想到项目根下还躺着三个 `audio_*`。要统计就全量扫：

```python
# 项目根下所有目录，按体积排序 —— 别猜，扫一遍
for d in sorted(os.listdir(".")):
    if os.path.isdir(d) and d not in (".git",):
        print(d, size_of(d))
```

### 删东西：必须过闸门，不要自己写 os.remove / rm

**这个项目会堆大量中间产物**：视频、音频、抽帧图。跑完两个合集实测 6.4GB。

但清理归清理，**删除自己写不得**。真实教训：2026-10-11 我用 `os.remove` 删了
一个「看起来像测试产物」的文件，实际是用户的聊天记录，22 条对话不可恢复。

规则：

```bash
python tools/safe_delete.py <路径>          # 先看：待删什么、多大、有没有被拒
python tools/safe_delete.py <路径> --yes    # 再删（进回收站）
```

六道闸（系统树、关键路径、目录层级、通配符、dry-run、大批量二次确认）见脚本内注释。
自己写删除逻辑时，这几条必须照做：

| 必须 | 原因 |
|---|---|
| **走回收站**（send2trash）不用 `rm` / `os.remove` | 后悔了能捞回来 |
| **默认 dry-run** | 先看清单再动手 |
| **判断依据是「谁生成的」**，不是文件名 | `_tmp`、`test` 这种名字也可能是用户数据 |
| **不确定就 `shutil.move` 到 `.bak`** | 挪走比删掉安全 |
| **报告 + 确认** | 列清单、说清删什么留什么，等用户点头 |

批量清理中间产物用现成的：

```bash
python tools/cleanup.py              # 预览：视频/音频/候选帧各能回收多少
python tools/cleanup.py --all --yes  # 执行
```

它只清 `assets/video`、临时音频、`notes/*/frames/` 里**没被 note.md 引用**的候选帧，
永不碰 `notes/` 的成品、`models/`、`extracted/`。视频只删「对应分P的 note.md 已写完」的。

#### 三个踩过的坑

- **别用 shell 调系统命令行删除**：实测返回码全是 1、stderr 空，1432 个文件只报成功
  40 个，实际删掉的远不止 —— 返回码不可信比删不掉更糟（你以为没删，其实删了）。
- **`send2trash` 自己也会误报**：批量删 118 个，它报「110 个失败，系统找不到指定的
  文件」，但一查三个目录全空了 —— 其实都删掉了。所以**别信返回值，信事实**：

  ```python
  try:
      send2trash(p)
  except Exception:
      if not os.path.exists(p):   # 报错但其实删了
          ok += 1
      else:
          fail += 1
  ```

  这条和上一条是同一个教训的两面：**删除的唯一可靠判据是「文件还在不在」**，
  返回码、异常、日志都只能当参考。
- **音频按扩展名过滤会漏**：临时目录里有 37 个音频被命名成 `.mp4`（早期版本存的），
  还有 42MB 的 `.part` 碎片，只匹配 `.m4a/.wav` 会漏掉 294MB。
  临时目录里除 `.json` 缓存外，一律算可删。
- **候选帧是隐藏的空间黑洞**：抽帧是「每章 5 张留着挑」，一篇文章只用 8~10 张。
  实测 77 篇抽了 1769 张（181MB），实际引用 552 张（41MB），挑剩的 1217 张白占 140MB。
  HTML 里是 base64 副本，删掉不影响任何已交付产物。

### 不做合并（2026-10-30 起，这是硬规定）
**合集不合并成一份文件。** 一期一个 HTML，逐篇投递，这就是最终交付形态。
不要主动提议合并，也不要在交付时附带"要不要我合成一份"。

理由不是偏好，是两次都撞了：

| 时间 | 做了什么 | 结果 |
|---|---|---|
| 2026-10-29 | 17 讲合成一份，我理解成「按知识点重排 + 精简」 | 只剩分篇的 21%，图 134→23，用户一眼看出「怎么少了这么多」，返工两轮 |
| 2026-10-30 | 28 讲合成 20.3 万字单文件 | 用户看完说「还是拆成一期一期吧」，全部重做 |

而且合并版有硬伤：20+ MB 超过 IMA 的 10 MB 上限，必须压图（820px/q48），
清晰度反而不如单篇原图；图片路径要重写（不改就断链）、标题层级要重编，
每一步都在制造新故障点。换来的只是「少点几次」——而知识库本身按条目检索，
单篇反而更好用。

> `tools/merge_notes.py` / `tools/verify_merge.py` 仍留在仓库，但**不在流程里**。
> 仅当用户明确说「我要一个单文件、要离线分发」时才用；用的时候必须纯机械、零改写，
> 并且跑 `verify_merge.py` 做四维校验（标题/段落/代码块/图，全 0 丢失才算过）。
> 合并后如果超过 10 MB，`tools/shrink_html.py` 会逐档降质压进去。

### 转写模型必须走离线加载

本地已有模型时，`huggingface_hub` 仍会走代理去校验 etag，代理抽风就 502，
把「本地明明有模型」变成「转写失败」。`transcribe_local` 已按目录存在与否传
`local_files_only=True`；手工调用时也可 `HF_HUB_OFFLINE=1` 加保险。

## 实战范例 A：28 讲合集 → 28 个单篇（默认做法）

**任务原话**：「把整个合集转写，整理好我就导入到 IMA 知识库」+ 后续「拆成一期一期」。

**交付物**：`notes/BV12omoB4ExF_SPLIT/` 下 28 个 HTML，合计 29.31 MB / 20.3 万字 / 250 张图，
逐个传进 IMA「人工智能」文件夹。**全程 0 元**。

### 关键命令

| # | 命令 | 验收点 |
|---|---|---|
| 1 | `python -m src.bili_notes <BV> --pages 1-28 --frames 5 --quality 64 --cols 5` | 28 个 `notes/<BV>_PNN/` |
| 2 | `python -m src.bili_notes <BV> --audit` | 每P亲和度 ≥ 0.45（串台会掉到 0.3） |
| 3 | ~~逐篇写 `note.md`~~ | 每篇图引用数 == 附表行数、无缺图 |
| 4 | 循环 `python -m src.md_to_html` 生成 28 个单篇 HTML | 每篇 0.5–1.5 MB，退出码 0 |
| 5 | 逐篇 create_media → `ima_put.py` → add_knowledge | folder_id 正确、无重名 |

### 这一轮踩到的三个坑（都已修进工具）

- **串台字幕**：28P 里 P15 拿到英雄联盟解说、P27 拿到笔记本维修，字幕「可用」所以走不到
  转写兜底。`--audit` 亲和度 0.297 / 0.427（其余 0.57–0.73）精准揪出。
  用新加的 **`--force-whisper`** 跳过字幕直接重转。
- **不要主动合并**：第一版做成了 20.3 万字单文件，用户看完说「还是拆成一期一期吧」，
  全部重做。合并版还有硬伤——超 10 MB 必须压图，清晰度反而不如单篇。
  现在这是一条硬规定，见「不做合并」节。
- **IMA 的「知识库」是文件夹**：用户说的「蓝桥杯」「人工智能」都在同一个知识库下，
  靠 `folder_id` 区分。见「投递到 IMA」节。

## 单篇模板（照抄，别自创结构）

**先提炼重点，再动笔成文（硬规则，2026-10-31）。**

`transcript.md` 是**素材**，不是正文。动笔前先把每一讲的核心要点提炼出来：
这一讲在讲什么问题、给出了什么结论/方法、有哪些必须保留的数据、代码、命令。
**以提炼出的要点为骨架组织正文，禁止把字幕整段搬进正文。**

- 目标篇幅：正文约为字幕字数的 1/3 ~ 1/2（字幕 5000 字 → 正文 1500~2500 字）。
  讲座类、代码实操类可以偏上限；纯闲聊水分多的偏下限。
- **留三类内容**：结论与方法、可复用的命令/代码/参数、具体数字。
- **丢三类内容**：重复啰嗦的过渡句、口头语与寒暄、与主线无关的支线话题。
- 提炼过程在脑子里/草稿里完成，**不要落成单独的大纲文件**——每期视频的
  产物只有 note.md 和 note.html 两个文件，多出来的都是垃圾。

每篇结构固定成五块，agent 只往里填内容，不要发明新结构：

````markdown
# <BV>｜<这一讲讲什么 + 用了什么招>

![<这帧是什么>](frames/c01/000019.80.jpg)

*<时间码> <这一帧在讲什么，具体到能替代这张图>*

> 一句话总结：**<压成一句，必须点出"难在哪 + 怎么破">**

**视频**：[<BV> · P<N>](链接) ｜ 时长 xx 分钟 ｜ 文本来自 B站 AI 字幕（N 条 / N 字），图来自视频关键帧

---

## 核心结论

- **<加粗的结论句>**：<一句解释，能独立读懂>
- （6–8 条）

## 01 <第一个小节的问句或名词短语>

<正文：推导过程，保留讲者原话里的例子和数字>

![...](frames/c02/000148.00.jpg)

*<时间码> <这帧在讲什么>*

## 02 ...

## 关键数据与术语

- **<术语>**：<解释，含具体数值/范围/命名>

---

## 附：这 N 张图是怎么来的

| 图 | 时间码 | 抽帧依据 |
|---|---|---|
| c01/000019.80.jpg | 00:22 | <为什么选它：内容完整 / 手写没被擦 / 代码已敲完> |
````

**四条硬要求（缺一条这篇就得重写）**：

1. **每张图下面必须有 `*时间码 + 这帧在讲什么*`** —— 只贴图不写说明，图等于装饰品。
2. **核心结论每条都要能在正文找到出处**，不许引入视频没说过的内容（哪怕是"正确"的知识）。
3. **讲者口误 / 讲错 / 屏幕与口述不一致的地方原样保留，标成「坑」**，不许偷偷改正。
   实测例子：P03 视频里 `q[l:r+1] = tmp` 只口述、屏幕第 22 行是空行；P04 讲者两处写反
   （`mid` 写到循环外、性质判断写反）。这些在分篇里都标成坑，是笔记最有价值的部分。
4. **字幕来源要标清**（AI 字幕条数/字数，或 whisper）。whisper 转的篇要注明，
   因为专有名词识别率不一样。

### 本轮真实故障（6 个，下次一定还会遇到）

| 现象 | 处理 |
|---|---|
| 连下 12 个分P后报「所有视频源均下载失败」 | CDN 限速，**单跑该分P即可**，别整批重来（P13 重跑 53 秒成功，51 帧） |
| 某P亲和度 0.270、其余 0.57–0.84 | 串台。`--audit` 揪出 → `mv` 该 cid 缓存到 `.subcache/.bad/` 留证 → `--page N --force` 走 whisper |
| `--force` 后进程被杀 / 无输出 | 前台 timeout 掐掉了 whisper（要好几分钟）。**改后台跑**，别设 60–90 秒的短超时 |
| `md_to_html ... -o out.html` 生成一个名叫 `-o` 的文件 | 它只吃两个位置参数：`<in.md> <out.html>`，没有 `-o` |
| HTML 打开一片坏图，但生成时没报错 | 旧版 `_img_tag` 在**图文件不存在时会静默输出外链 `<img>`**。已改为汇总告警并退出码 1；看到「有 N 张图找不到源文件」就是 figs 目录没复制全 |
| `verify_merge.py` 报「内嵌 82 != 引用 134」，重跑又好了 | 读到**过期的 note.html**。已改为：HTML 比 md 旧时给提示并跳过内嵌检查。重新生成 HTML 即可 |

### 成本与产出比（17P，10 小时 14 分视频）

| 项 | 数值 |
|---|---|
| 字幕 | 151,790 字（16P 走 AI 字幕，1P whisper 兜底） |
| 候选帧 | 595 张 → 去重 + 人工核对后 **126 张**入笔记 |
| 视频缓存 | 1.3 GB（1080P DASH 视频轨，`assets/video/`） |
| 代码 | 17 篇分篇，78 个代码块 |
| 钱 | **0 元**（无 API 调用） |

> 判断"这一讲值不值得写"：字幕 < 2000 字且候选帧 < 3 张的，多半是纯口播或过场，
> 合成一段短记录即可，别硬凑一篇。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 所有视频「探测不到字幕」 | 会话过期或未登录，跑 `--check` 确认，重新扫码 |
| 扫码一直超时 | 二维码约 3 分钟有效；重新 `login --no-wait` 生成 |
| `HTTP 412` / `code -412` | WBI 接口被风控；脚本自动降级老接口并本会话禁用 WBI，不影响取字幕 |
| 有字幕按钮但 track 为 0 | 未登录，AI 字幕不下发 |
| 日志出现「字幕轨道查询失败（网络问题，不是没字幕）」 | `x/player/v2` 偶发 404。这是网络/B站接口问题，**不要急着重新扫码**，重跑一次通常就好（实测 12:13 连续 404、12:15 同一地址 200） |
| 转写很慢 | 先 `--probe` 确认是否真没字幕；有字幕的分P不会进转写 |
| 字幕内容明显不是这个视频（讲别的主题/带歌词） | **串台**。`--audit` 体检确认 → `--pages N --force-whisper` 跳过字幕直接重转（首选，一条命令）；老办法是挪走该 cid 缓存让 whisper 兜底 |
| 「所有视频源均下载失败」 | 大合集连下十几个分P后 CDN 会限速超时；单独重跑该分P即可（实测 P13 重跑一次成功） |
| 转写报 `502 Bad Gateway` 但本地有模型 | 走了 HF 代理校验 etag。用 `HF_HUB_OFFLINE=1`，或确认 `transcribe_local` 的 `local_files_only` 生效 |
| 字幕内容跟视频对不上 / 只覆盖前几分钟 / 覆盖到片尾之后 | B站 返回了**别的视频**的字幕对象（实测：713s 的视频给回 1203s 的LOL解说字幕 = 168%；另有长度相仿的电影解说字幕）。已内建「覆盖度区间 + AI总结交叉校验」双重检查，日志出现「字幕覆盖仅/覆盖超出/内容与该视频不符，弃用该轨道」属正常自愈，无需干预 |
| 日志反复出现「内容与该视频不符」后回落转写 | 该 cid 的字幕在 B站 侧就是坏的（实测某 cid 5 次里 4 次 404、1 次正确）。已自动重试 3 轮，仍失败走转写是正确的，不要以为是本地 bug |
| AI 字幕把英文术语听错 | 中文 AI 字幕对专有名词识别差（DeepSeek→"deep sick"、DeepInfer→"d info"）。技术视频引用术语需人工校对；也可 `--no-subtitle` 走 whisper 对比 |
| 中途报「登录态无效」 | 会话 TTL 到期，重扫一次；长任务用 `--keep-session` 并加长 `--session-ttl`。注意：**只有 B站 明确回 -101 才会焚毁会话**，网络超时只告警不烧会话 |
| `getaddrinfo failed` / `ProxyError` / `502` | 本机代理链路抖动。脚本会 TCP 预检 `HTTPS_PROXY` 死端口并自动切直连，3 秒后整链路再试一轮；仍失败说明代理客户端挂了，重启代理或换节点 |
| `.env` Cookie 报 `-101` | 长期 Cookie 已过期。扫码登录，或重新从浏览器取一份写进 `.env` |
| 用户说「合成一份」，做出来却比单篇「少了很多」 | 合并只许机械拼接，不许概括精简。先看「不做合并」节——默认不该合并；确需合并时跑 `tools/merge_notes.py` + `verify_merge.py` 四维校验 |

## 自检

```bash
python selftest_subtitle.py              # 75 项离线测试（签名/优先级/合段/缓存/覆盖度/会话/焚毁/繁简）
python selftest_subtitle.py --audio a.m4a  # 额外实测转写链路（不联网，需本地音频）
python selftest_subtitle.py --live BV1xx # 额外做一次真实探测
python src/bili_auth.py status --json    # 会话状态（不含凭证内容）
python tools/cleanup.py                  # 磁盘占用体检（只看不删）
```

## 已移除的旧入口（2026-10-27）

`src/gui.py`（Tkinter 界面）、`src/main.py`（CLI/webhook 入口）、`src/utils.py`、`src/config.py`、
`src/zhipu_processor.py`（智谱/百度/OCR 处理层）、`test_baidu.py` 已删除：做成 skill 后由 agent
直接编排，界面与 webhook 层纯属冗余。需要时从 git 恢复：

```bash
git checkout HEAD -- src/gui.py src/main.py src/utils.py src/config.py src/zhipu_processor.py
```

## 遗留脚本

`check_subs60.py` / `check_subs_any.py` / `transcribe60.py` / `batch_extract.py` 等是 v1 分步脚本，
功能已被 `extract_bilibili.py --probe` 与 `--pages` 覆盖，仅作历史参考，不再维护。

> ⚠️ **别跑这些脚本。** 它们会把音频和转换中间文件堆在**项目根**下的
> `audio_b/` `audio60/` `audio_tmp/`，且不在任何清理流程里。
> 实测这三个目录攒了 **3.1 GB**（`audio_b` 一个就 2.7 GB），在 `.gitignore` 里
> 躺了几个月没人发现，直到有一次统计磁盘才暴露。
> 新代码一律落 `assets/` 或系统 temp 目录，见「产物落盘与生命周期」节。
