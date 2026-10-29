# -*- coding: utf-8 -*-
"""繁体 → 简体（离线，零依赖）。

whisper 对中文默认输出繁体，B站 AI 字幕个别视频也有繁体。
这里内置一份常用字映射（取自 subbatch 插件的转换表，374 条），
若环境里装了 opencc（可选，不随本项目分发）则优先用库，覆盖更全。

用法：
    from zh_convert import to_simplified
    text = to_simplified(text)
"""

from __future__ import annotations

# 交错存放：繁简繁简……省空间也便于校对
_PAIRS = (
    "來来個个備备兩两創创動动區区問问嚴严國国執执報报場场壓压實实寫写將将對对帶带幾几強强後后從从應应據据斷断於于時时書书會会東东條条業业極极構构標标"
    "樣样機机檢检欄栏權权歲岁歷历歸归殘残殺杀殼壳毀毁沒没準准為为無无現现環环產产畢毕異异當当痙痉發发盡尽監监盤盘確确種种穩稳紀纪結结絕绝經经總总續续"
    "與与著着處处號号術术裏里裡里規规視视計计討讨訓训設设許许訴诉診诊註注証证評评詞词詢询試试詩诗話话該该詳详認认語语誠诚誤误說说誰谁課课調调談谈請请"
    "論论諸诸諾诺謀谋謁谒謂谓謊谎講讲謝谢證证識识譜谱議议護护讀读變变讓让豐丰豬猪貓猫財财貧贫貨货販贩貪贪貫贯責责貳贰貴贵貶贬貸贷費费貿贸賀贺資资賊贼"
    "賓宾賜赐賞赏賠赔賢贤賣卖賦赋質质賭赌賴赖賺赚賻赙購购賽赛贈赠贊赞贏赢趕赶趙赵趨趋躍跃車车軌轨軍军軟软軸轴較较載载輔辅輕轻輛辆輝辉輥辊輦辇輩辈輪轮"
    "輯辑輳辏輸输輻辐輾辗轄辖轅辕轆辘轉转轎轿轔辚轟轰轡辔轢轹轣辚轤轳辦办迴回這这連连週周進进運运過过達达選选還还鈎钩銀银銅铜錄录錯错鍊链鍋锅鍛锻鍵键"
    "鍾钟鎖锁鎢钨鎮镇鎳镍鏈链鏡镜鐘钟鐳镭鐵铁鑄铸鑑鉴鑒鉴鑰钥鑽钻鑾銮長长門门閃闪閉闭開开閏闰閑闲閒闲間间閣阁閥阀閨闺閩闽閫阃閬阆閭闾閱阅閶阊閹阉閻阎"
    "閼阏閽阍閾阈閿阌闆板闈闱闊阔闋阕闌阑闍阇闐阗闒闼闓闿闔阖闕阙闖闯闗关闘斗闚窥闛闶關关闞阚闟闿闠闳闡阐闢辟闤闳闥闼陽阳際际隨随險险雙双雜杂雞鸡離离"
    "難难雲云電电靈灵靜静響响頁页頂顶項项順顺須须預预頻频題题額额顏颜願愿類类顧顾風风飛飞飯饭飲饮養养餘余館馆饋馈馬马馮冯駐驻駕驾驅驱驗验驚惊骯脏髒脏"
    "體体髮发鬥斗鬧闹鹵卤鹹咸鹽盐麗丽麥麦麵面黃黄黌黉點点齊齐齋斋齎赍齏齑齒齿齕龁齗龂齙龅齚龁齛龁齜龇齝龁齞龁齟龃齠龆齡龄齣出齦龈齧啮齨龁齩咬齪龊齬龉"
    "齮龁齯龁齲龋齳龁齴龁齵龁齶腭齷龌龍龙龐庞龔龚龕龛龜龟龢和"
    # —— 2026-10-29 补充：高频但原表遗漏的字 ——
    # 原 374 字表是「够用就好」的水平，这几个字（学/习/获/数/们/么…）在日常文本里
    # 出现极频繁，缺了会让繁体稿几乎没被转换。完整覆盖请安装 opencc（Apache-2.0）。
    "學学習习們们麼么網网腦脑義义聯联統统組组織织劃划圖图檔档別别簽签獲获數数"
)

_FLAT = "".join(_PAIRS)  # 分块只为排版，这里拼回连续串
T2S: dict[str, str] = {}
for i in range(0, len(_FLAT) - 1, 2):
    t, s = _FLAT[i], _FLAT[i + 1]
    if t != s:
        T2S.setdefault(t, s)

_converter = None
_backend = "builtin"


def _get_converter():
    """优先用 opencc（若用户自行安装），否则用内置表。

    注意：这里刻意不引入 zhconv —— 它是 GPLv2+，本项目以 MIT 分发，
    不能把它列为依赖；内置表已覆盖常用繁体字。
    """
    global _converter, _backend
    if _converter is not None:
        return _converter
    try:
        from opencc import OpenCC  # type: ignore

        _converter = OpenCC("t2s").convert
        _backend = "opencc"
    except Exception:  # noqa: BLE001
        _converter = lambda s: "".join(T2S.get(ch, ch) for ch in s)  # noqa: E731
        _backend = "builtin"
    return _converter


def to_simplified(text: str) -> str:
    """把繁体中文转成简体；非中文内容原样返回。"""
    if not text:
        return text
    if not any(ch in T2S for ch in text) and _backend == "builtin":
        return text  # 没命中任何繁体字，直接返回，省一次遍历
    return _get_converter()(text)


def backend() -> str:
    """当前使用的转换后端：opencc / builtin。"""
    _get_converter()
    return _backend
