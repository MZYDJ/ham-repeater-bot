#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点名主播 —— net_control.py
纯ASR + 本地状态机 + LLM信息提取（可选） + 独立TTS 的业余无线电中继台点名活动主持。

接收侧：复用 direct_announce.PersistentAnnouncer 常驻连接与下行泵
（on_downlink 回调）→ Opus 解码 → 话音检测(VAD) 切段 → WAV 落盘 → ASR 转写。
信息提取：字母解释法词表确定性解码（主） + 呼号格式校验 + 已抄收去重
+ 置信度判定 → 低置信度走"请重复一遍"（限次）→ LLM（可选）补提取信号/备注。
播报侧：Edge-TTS 合成（固定话术缓存复用）+ 复用链路抢麦/发包（ensure/release）。

两种模式：
- 开放点名（默认 roster_mode=false）：CQ 开场 → 收听窗口内逐个抄收 → 汇总 → 结束
- 固定名单点名（roster_mode=true）：逐个呼叫名单成员 → 超时跳过 → 汇总 → 结束

与定时播报共享同一条常驻链路：busy 锁保证同一时刻只有一个发射者；
announce.py 在点名进行中跳过整点播报（net_active()）。

依赖：libopus0（解码，镜像已有）、edge_tts（TTS，镜像已有）、urllib（标准库）。
无新增 pip 依赖。ASR 走 qwen3-asr-flash（阿里云百炼 OpenAI 兼容接口，System
Message 传实体词表提升呼号识别）；LLM 可选走 GLM-4.5-Flash（智谱免费模型）。

用法：
    python3 net_control.py --decode "Bravo Hotel Three X-ray X-ray 信号五九"   # 离线解码自测
    python3 net_control.py --opus-roundtrip                                     # 编解码往返自测
"""
import argparse
import array
import base64
import contextlib
import csv
import datetime
import http.client
import json
import logging
import queue
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import direct_announce

logger = logging.getLogger("net_control")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")


class _NormalCloseFilter(logging.Filter):
    """websocket-client 库把 WebSocket 正常关闭（code 1000，服务器 Bye/本端主动
    close 后收到）也打 ERROR——实测 21:16:20 两条"Connection closed normally
    (code 1000)"吓人但属预期行为。仅将这类正常关闭降为 DEBUG，真错误保留。"""
    def filter(self, record):
        msg = record.getMessage()
        if "Connection closed normally" in msg or "code 1000" in msg:
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


for _lib in ("websocket", "websockets", "websocket-client"):
    _lg = logging.getLogger(_lib)
    if not any(isinstance(f, _NormalCloseFilter) for f in _lg.filters):
        _lg.addFilter(_NormalCloseFilter())

# ====================== 配置访问（复用 direct_announce 的 CFG） ======================
def nc_cfg(*path, default=None):
    return direct_announce.cfg_get("net_control", *path, default=default)

# ====================== 字母解释法词表（确定性解码） ======================
# ITU 字母解释法标准词 + 数字读法（含常见变体）
PHONETIC_ITU = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I", "juliett": "J",
    "juliet": "J", "kilo": "K", "lima": "L", "mike": "M", "november": "N",
    "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S",
    "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W", "whisky": "W",
    "xray": "X", "yankee": "Y", "zulu": "Z",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "niner": "9", "tree": "3", "fower": "4", "fife": "5",
    # ASR 对解释法单词的常见听写变体（中英混识实测）：Foxtrot→Florida/follow/fox
    "florida": "F", "fox": "F", "follow": "F",
}
# 常见中文音译变体（本地台网习惯可经配置 net_control.extra_vocab 扩展；此处覆盖主流写法）
PHONETIC_ZH = {
    "阿尔法": "A", "布雷沃": "B", "布拉沃": "B", "博拉沃": "B", "查理": "C",
    "德尔塔": "D", "艾可": "E", "埃可": "E", "艾科": "E", "福克斯特": "F",
    "福克斯": "F", "高尔夫": "G", "霍特尔": "H", "霍特": "H", "印地安": "I",
    "印迪亚": "I", "因迪亚": "I", "朱丽叶": "J", "朱丽特": "J", "基洛": "K",
    "基罗": "K", "利马": "L", "迈克": "M", "麦克": "M", "十一月": "N",
    "奥斯卡": "O", "帕帕": "P", "魁北克": "Q", "罗密欧": "R", "西拉": "S",
    "塞拉": "S", "探戈": "T", "尤尼弗姆": "U", "尤尼福姆": "U", "维克多": "V",
    "威士忌": "W", "艾克斯": "X", "艾克斯瑞": "X", "爱克斯": "X", "杨基": "Y",
    "扬基": "Y", "祖鲁": "Z",
    # ASR 中文音译变体实测（21:14 日志）："佛罗里达之路"=Foxtrot Zulu、
    # "弗雷"=Foxtrot（"反而我弗雷打住了"）、"高"不作为单字映射（防"高功率"污染）
    "佛罗里达": "F", "之路": "Z", "弗雷": "F",
}
CN_DIGITS = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
             "洞": "0", "幺": "1"}
CN_DROP = {"十": "", "百": "", "千": ""}         # 数字串中的位词直接省略（五十九→59）
# 中国内地业余电台呼号一般结构：B + 分区字母 + 数字 + 1~3 位字母后缀（后缀纯字母，
# 天然与信号报告数字区分，避免"BH3XX59"粘连误判）。港澳台/其他地区可经配置覆盖。
DEFAULT_CALL_RE = re.compile(r"^B[A-Z]\d[A-Z]{1,3}$")
# 快路径：在原文上直接找呼号子串（"BH3XXX"/"BG9ABC" 直读场景，不依赖词表装配）。
# 后缀纯字母，尾随数字/标点必然不属于呼号，故不加尾边界（避免挡住 "BH3XX59"）。
FAST_CALL_RE = re.compile(r"(?<![A-Z0-9])B[A-Z]\d[A-Z]{1,3}")
SIG_RE = re.compile(
    r"信号\s*([0-9零一二两三四五六七八九洞幺十百千]{1,4})"
    r"(?:\s*(?:和|及|、|/|到|至|\s)\s*([0-9零一二两三四五六七八九洞幺十百千]{1,4}))?")


def _tokenize(text):
    """按空白切块后取词：英文词（含连字符/撇号）、中文连续串、数字串。"""
    toks = []
    for chunk in re.split(r"\s+", text):
        if not chunk:
            continue
        toks += re.findall(r"[A-Za-z][A-Za-z\-']*|[\u4e00-\u9fff]+|\d+", chunk)
    return toks


def _map_token(tok, call_re):
    """单个 token → (映射串, 未映射字符数)。呼号直读（如 BH3XXX）直接透传。"""
    t = tok.strip().replace("-", "").replace(" ", "").lower()
    if t in PHONETIC_ITU:
        return PHONETIC_ITU[t], 0
    if re.fullmatch(r"[a-z]+", t) and len(t) == 1:
        return t.upper(), 0
    if t.isdigit():
        return t, 0
    up = t.upper()
    if call_re.fullmatch(up):                    # 直接读出完整呼号（BH3XXX）
        return up, 0
    if re.fullmatch(r"[\u4e00-\u9fff]+", t):
        out, unk = "", 0
        i = 0
        while i < len(t):
            hit = None
            for k in sorted(PHONETIC_ZH, key=len, reverse=True):
                if t.startswith(k, i):
                    out += PHONETIC_ZH[k]
                    i += len(k)
                    hit = True
                    break
            if hit:
                continue
            ch = t[i]
            if ch in CN_DIGITS:
                out += CN_DIGITS[ch]
            elif ch in CN_DROP:
                pass
            else:
                unk += 1
            i += 1
        return (out if out else None), unk
    # 长英文串的词表贪心分词：ASR 偶尔无空格连写解释法
    # （"bravoindianinegolfcharliewhiskey"），整串按未知会丢呼号。
    # 至少命中 1 个词表词才按解释法串处理（避免普通英文句被逐字污染）。
    # 短英文串（≤4 字母）无条件逐字符透传：覆盖 "BJ九EFU"/"BI九DGI" 这类
    # 被中文数字切开、词表匹配不到的解释法后缀（EFU/DGI），不切会丢呼号。
    if re.fullmatch(r"[a-z]+", t) and len(t) >= 2:
        out, matched = "", 0
        i = 0
        while i < len(t):
            hit = False
            for k in sorted(PHONETIC_ITU, key=len, reverse=True):
                if t.startswith(k, i):
                    out += PHONETIC_ITU[k]
                    i += len(k)
                    matched += 1
                    hit = True
                    break
            if hit:
                continue
            out += t[i].upper()          # 未知字母逐字符透传（保留拼写，便于呼号子串搜索）
            i += 1
        if matched >= 1 or len(t) <= 4:
            return out, 0
    return None, 1


def extract_signal(text):
    """从转录文本提取信号报告（"信号五九/59" → "59"，"59 59" → "5959"）。"""
    m = SIG_RE.search(text)
    if not m:
        return None
    def conv(s):
        return "".join(CN_DIGITS.get(ch, CN_DROP.get(ch, ch)) for ch in s)
    a = conv(m.group(1))
    b = conv(m.group(2)) if m.group(2) else None
    if not a:
        return None
    return a if b is None else a + b


def decode_callsign(text, regex=""):
    """从 ASR 转录文本确定性解码呼号与信号报告（开放集，不依赖预知名单）。
    返回 {"callsign", "signal", "score", "reasons"}。
    双路径：①快路径——原文直读呼号子串（BH3XXX/BG9ABC 不经词表）；
    ②慢路径——字母解释法词表映射装配，取最左最短合法呼号。
    置信度 = 格式合法(60) + 文本干净度(40)。"""
    call_re = re.compile(regex) if regex else DEFAULT_CALL_RE
    if not text:
        return {"callsign": None, "signal": None, "score": 0, "reasons": ["空文本"]}
    # 快路径：原文直读（覆盖被数字/标点切开的直读呼号，如 "BG9ABC"）
    fm = FAST_CALL_RE.search(text.upper())
    fast_call = fm.group(0) if fm else None
    # 慢路径：解释法词表装配
    seq, unknowns = [], 0
    for tok in _tokenize(text):
        m, unk = _map_token(tok, call_re)
        if m:
            seq.append(m)
        unknowns += unk
    full = "".join(seq)
    best = None
    for i in range(len(full)):                     # 最左
        for j in range(len(full), i, -1):          # 最长（后缀纯字母，无数字粘连）
            if call_re.fullmatch(full[i:j]):
                best = full[i:j]
                break
        if best is not None:
            break
    candidate = fast_call or best                  # 快路径更干净，优先
    reasons = []
    if candidate is None:
        reasons.append("未匹配呼号格式")
    if unknowns:
        reasons.append(f"未映射字 {unknowns} 个")
    n_chars = max(1, len(full) + unknowns)
    clean_ratio = 1.0 - unknowns / n_chars
    score = 0
    if candidate is not None:
        score = 60 + int(round(40 * clean_ratio))
        if score > 100:
            score = 100
    return {"callsign": candidate, "signal": extract_signal(text),
            "score": score, "reasons": reasons}


def is_duplicate(call, checked_calls):
    """已抄收去重（大小写归一）。"""
    return call.upper() in checked_calls


DIGIT_SPOKEN = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
                "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}
LETTER_SPOKEN = {"A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta",
                 "E": "Echo", "F": "Foxtrot", "G": "Golf", "H": "Hotel",
                 "I": "India", "J": "Juliett", "K": "Kilo", "L": "Lima",
                 "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa",
                 "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
                 "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray",
                 "Y": "Yankee", "Z": "Zulu"}


def callsign_phonetic(call):
    """呼号 → 字母解释法英文单词（TTS 友好：读单词比读字母串稳定）。
    "BH3XX" → "Bravo Hotel Three X-ray X-ray"；数字用 ITU 读法。"""
    out = []
    for ch in str(call or "").upper():
        if ch.isdigit():
            out.append(DIGIT_SPOKEN.get(ch, ch))
        elif ch.isalpha():
            out.append(LETTER_SPOKEN.get(ch, ch))
    return " ".join(out)


FIELD_LABEL = {"qth": "QTH", "device": "设备", "antenna": "天线",
               "power": "功率", "signal": "信号"}


def callsign_edit_distance(a, b):
    """两呼号 Levenshtein 编辑距离（大小写不敏感）。"""
    a, b = (a or "").upper(), (b or "").upper()
    la, lb = len(a), len(b)
    if abs(la - lb) > 4:
        return abs(la - lb)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def callsign_similar(a, b, max_dist=1):
    """呼号相似度：编辑距离 ≤ max_dist 视为"同一友台识别修正"候选。
    取 max_dist=1（ASR 把呼号听错几乎都是单个字符，如 BG9BLZ↔BG9BFZ）；
    距离 ≥2（如 BG9AA vs BG9BB、BI9BZY vs BG9BFZ）→ 判定为另一友台。
    保守倾向"排队而非替换"：把修正误判成插队只会多等一轮（不丢人），
    把插队误判成修正会顶掉当前友台（丢人）——中继转发场景所有友台
    同 session 无法靠 session 区分，此判据是最后防线。"""
    a, b = (a or "").upper(), (b or "").upper()
    if not a or not b:
        return False
    return callsign_edit_distance(a, b) <= max_dist
# 点名应答应记录的核心结构化字段（抄收后若缺失则主动追问，每字段最多问一次）
REQUIRED_FIELDS = ("signal", "qth", "device", "antenna", "power")


def extract_report_fields(text):
    """从补充信息段提取结构化字段（点名确认/记录用）：
    返回有序 [(内部key, 值)]，仅含实际提取到的字段。
    - QTH/设备/天线/功率/信号报告 五类核心信息（key=qth/device/antenna/power/signal）
    - 提取不到任何字段 → 空列表（调用方不重复复诵，改走确认/纠正/静默分支）"""
    fields = []
    # QTH：ASR 常展开为 "Q T H"，覆盖 QTH/Q T H/位置/地址/所在地
    m = re.search(r"(?:QTH|Q\s*T\s*H|位置|地址|所在地)(?:[是在位于]|的|是)?"
                  r"\s*([^，。；,;.!！?？\s]{2,24})", text, re.I)
    if m and not re.search(r"[A-Za-z]\d[A-Za-z]{1,3}", m.group(1)):
        fields.append(("qth", m.group(1).strip()))
    # 设备：覆盖"设备是手机/电台为K6/用的是手机/使用手机/用手机"等句式。
    # 先匹配"设备/机器/电台/手台/车台"关键词（"我使用的设备是手机"→手机），
    # 再兜底"用/使用"句式（"用的是手机"→手机）；排除疑问词防误取。
    # ASR 常插入填充词（"设备情况森海科斯G T幺二"→"情况"是噪声，型号含中文
    # 数字+空格，"G T幺二"应并回设备名）→ 分隔符含"情况/的话"等，匹配集含
    # 空格，事后压缩空格并把型号中文数字转阿拉伯（幺二→12）。
    m = re.search(r"(?:设备|机器|电台|手台|车台)(?:是|为|的|的是|用的|情况|的话)?"
                  r"\s*([\u4e00-\u9fffA-Za-z0-9 \-]{2,20})", text, re.I)
    if not m:
        m = re.search(r"(?:用的是|使用的是|用|使用)(?:的)?(?:是)?"
                      r"\s*([\u4e00-\u9fffA-Za-z0-9\-]{2,16})", text, re.I)
    if m and not re.search(r"什么|哪个|怎样|怎么|多少|干嘛|干吗", m.group(1)):
        dev = m.group(1).strip()
        dev = re.sub(r"\s+", "", dev)      # "G T幺二" → "GT幺二"
        dev = re.sub(r"[零幺一二三四五六七八九洞两]",
                     lambda c: CN_DIGITS.get(c.group(0), c.group(0)), dev)
        fields.append(("device", dev))
    # 天线：两种常见语序——"天线原机天线"（天线在前）与"原机天线/八木天线"（天线在后）。
    # 优先"天线在前"（避免把"天线原机天线"误切为 xxx天线），再试"天线在后"。
    m = re.search(r"天线(?:是|为|的|用的)?\s*([\u4e00-\u9fffA-Za-z0-9\-]{2,12})",
                  text, re.I)
    if m:
        fields.append(("antenna", m.group(1).strip()))
    else:
        m = re.search(r"([\u4e00-\u9fffA-Za-z0-9\-]{2,6})天线", text)
        if m:
            fields.append(("antenna", m.group(1) + "天线"))
    # 功率：阿拉伯数字 + 中文数字（"5瓦/五瓦"），不带单位读法（瓦）；
    # 或档位词（"高功率/中功率/低功率/大功率"——实测 20:49 "高功率发射"）。
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:瓦|W)", text, re.I) or \
        re.search(r"([零一二两三四五六七八九洞幺])\s*瓦", text)
    if m:
        p = m.group(1)
        p = CN_DIGITS.get(p, p)          # 中文数字转阿拉伯
        fields.append(("power", f"{p} 瓦"))
    else:
        m = re.search(r"(高|中|低|大|小)功率", text)
        if m:
            fields.append(("power", m.group(1) + "功率"))
    # 信号报告（"信号五九"→59）
    sig = extract_signal(text)
    if sig:
        fields.append(("signal", sig))
    return fields


def clean_report_text(text):
    """补充信息段轻量清洗（TTS 复诵友好）：
    - 去首尾标点/空白、压缩连续空白
    - ASR 常把 QTH 展开成 "Q T" / "Q T H" → 归一为 QTH
    - 功率 "5W" → "5 瓦"（TTS 读 W 不稳定）；"5瓦" → "5 瓦"
    - 过滤纯标点/语气词的噪音文本（如 "。"、"嗯"、0.5s 环境声段）"""
    t = re.sub(r"^[\s。，,.！!？?；;：:、~～…]+|[\s。，,.！!？?；;：:、~～…]+$", "", text or "")
    t = re.sub(r"\s+", " ", t)
    # 中文场景不能用 \b（\b 依赖 \w 边界，中文相邻时失效；\w 也匹配中文，
    # 故用"排除后跟 H"防 QTH 二次替换）
    t = re.sub(r"Q\s*T\s*H", "QTH", t, flags=re.I)
    t = re.sub(r"Q\s*T(?![Hh])", "QTH", t, flags=re.I)
    t = re.sub(r"(\d+)\s*W", r"\1 瓦", t, flags=re.I)
    t = re.sub(r"(\d+)瓦", r"\1 瓦", t)
    t = t.strip()
    if not t:
        return ""
    # 纯语气词/无实义噪音（全为语气词且长度≤4）→ 视为空
    if len(t) <= 4 and re.fullmatch(r"[嗯啊哦呃哈哼唉]+", t):
        return ""
    return t


# ====================== WAV 落盘 ======================
def write_wav(path, pcm16, rate=16000):
    with open(path, "wb") as f:
        f.write(struct.pack("<4sI4s4sIHHIIHH", b"RIFF", 36 + len(pcm16), b"WAVE",
                            b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(struct.pack("<4sI", b"data", len(pcm16)))
        f.write(pcm16)


def pcm_to_wav_bytes(pcm16, rate=16000):
    """16k 16bit 单声道裸 PCM → 标准 WAV 文件字节（供 ASR Data URL 使用）。

    注意：ASR 服务端按 Data URL 声明的 mediatype（audio/wav）解析音频，
    若直接发送无 WAV 头的裸 PCM 会报 400
    （InternalError.Algo.InvalidParameter: ... does not support this input）。"""
    import io
    buf = io.BytesIO()
    buf.write(struct.pack("<4sI4s4sIHHIIHH", b"RIFF", 36 + len(pcm16), b"WAVE",
                          b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(struct.pack("<4sI", b"data", len(pcm16)))
    buf.write(pcm16)
    return buf.getvalue()


# ====================== 话音检测（VAD）与切段 ======================
class VoiceCapture:
    """话音检测与语音段切分：喂入 48kHz 单声道 PCM（下行 Opus 解码后），
    按 20ms 帧算能量，检测"起讲→讲话→静音收尾"，输出 16kHz 单声道完整段。
    带自适应噪声底；on_segment(pcm16, dur_s, wav_path, session) 在喂入线程中回调，
    session 为段来源讲话人标识（点名状态机据此把补充信息段归入当前友台）。"""
    FRAME_S = 960 * 2                                # 20ms @48k int16 字节数

    def __init__(self, threshold=None, silence_end_ms=None, min_segment_ms=None,
                 max_segment_ms=None, out_rate=16000, save_dir=None, on_segment=None,
                 ptt_release_delay_ms=None):
        self.threshold = threshold if threshold is not None else nc_cfg("vad_threshold", default=800)
        self.silence_end_ms = silence_end_ms if silence_end_ms is not None else nc_cfg("silence_end_ms", default=2000)
        self.min_segment_ms = min_segment_ms if min_segment_ms is not None else nc_cfg("min_segment_ms", default=400)
        self.max_segment_ms = max_segment_ms if max_segment_ms is not None else nc_cfg("max_segment_ms", default=15000)
        # PTT 抬起后延迟收尾（毫秒）：对方松 PTT 后不立即切段，再等这段窗口内的
        # 断续语音（中继台转发偶发停顿），避免"一句话没说完就断成两段"
        self.ptt_release_delay_ms = (ptt_release_delay_ms if ptt_release_delay_ms is not None
                                     else nc_cfg("ptt_release_delay_ms", default=1000))
        self.out_rate = out_rate
        self.save_dir = Path(save_dir) if save_dir else None
        self.on_segment = on_segment
        self._lock = threading.Lock()   # feed（keeper 线程）与 pump/_someone_speaking
                                        # （主线程/播报线程）并发访问同一状态，必须互斥
        self._speaking = False
        self._buf48 = bytearray()
        self._silence_s = 0.0
        self._noise = 0.0
        self._seq = 0
        self._seg_session = None        # 当前段来源 session（讲话人标识，点名上下文关联用）
        self._defer_frames = 0          # PTT 抬起后的延迟收尾剩余帧数（20ms/帧）
        self.last_feed_at = None        # 最近一次收到音频帧的时刻（采集卡死检测用）

    def _rms(self, frame):
        s = array.array('h')
        s.frombytes(frame)
        if not s:
            return 0.0
        return (sum(x * x for x in s) / len(s)) ** 0.5

    def _active_thr(self):
        return max(float(self.threshold), self._noise * 3.0, 300.0)

    def feed(self, pcm48, session=None):
        """喂入 48kHz 单声道 PCM。session 为该批语音的远端来源标识：
        讲话人切换（session 变化）时先收尾当前段再开新段，保证每个切出的
        语音段归属单一 session（点名状态机据此把"补充信息段"归入当前友台）。
        每批都刷新 last_feed_at——对方讲完、链路静默后不再有帧喂入，
        上层据此判定"已讲完"，避免 _speaking 无限卡 True。"""
        if not pcm48:
            return
        with self._lock:
            self.last_feed_at = time.time()
            if (self._speaking and session is not None
                    and self._seg_session is not None and session != self._seg_session):
                self._finalize()                       # 换人：先收尾上一位的段
            for off in range(0, len(pcm48) - self.FRAME_S + 1, self.FRAME_S):
                frame = pcm48[off:off + self.FRAME_S]
                rms = self._rms(frame)
                active = rms > self._active_thr()
                if not self._speaking:
                    if rms < self._noise:
                        self._noise = rms
                    else:
                        self._noise += (rms - self._noise) * 0.002
                    if active:
                        self._speaking = True
                        self._silence_s = 0.0
                        self._buf48 = bytearray(frame)
                        self._seg_session = session
                else:
                    self._buf48 += frame
                    if active:
                        self._silence_s = 0.0
                        self._defer_frames = 0          # 又有声音：取消 PTT 延迟收尾
                    elif self._defer_frames > 0:
                        self._defer_frames -= 1         # 延迟窗口内：不计入静音（防断续）
                    else:
                        self._silence_s += 0.02
                    dur = len(self._buf48) / (48000 * 2)
                    if (self._silence_s * 1000 >= self.silence_end_ms
                            or dur >= self.max_segment_ms / 1000):
                        self._finalize()

    def _finalize(self):
        # 调用方必须已持有 self._lock（feed/pump/force_finalize 内调用）
        pcm48 = bytes(self._buf48)
        session = self._seg_session
        self._speaking = False
        self._buf48 = bytearray()
        self._silence_s = 0.0
        self._seg_session = None
        self._defer_frames = 0
        dur = len(pcm48) / (48000 * 2)
        if dur < self.min_segment_ms / 1000:
            return
        pcm16 = direct_announce.resample_linear(pcm48, 48000, self.out_rate)
        wav_path = ""
        if self.save_dir:
            self._seq += 1
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            wav_path = str(self.save_dir / f"seg_{ts}_{self._seq:03d}.wav")
            try:
                self.save_dir.mkdir(parents=True, exist_ok=True)
                write_wav(wav_path, pcm16)
            except Exception as e:
                logger.warning(f"应答录音落盘失败: {e}")
        if self.on_segment:
            try:
                self.on_segment(pcm16, dur, wav_path, session)
            except Exception as e:
                logger.warning(f"应答段回调异常: {e}")

    def pump(self):
        """时间驱动收尾（不依赖 feed）：链路静默（无新帧）时静音累计无法推进，
        _speaking 段会挂到下一个讲话人 PTT 才被 force_finalize 顶出——实测
        "收到应答段"日志滞后数秒、靠后面的人顶出来（21:54/21:55 日志）。
        由主循环/等待循环周期性调用，按实际流逝时间推进 PTT 延迟窗口与静音
        累计；feed 恢复有帧时（active）会清零静音，互不冲突。"""
        with self._lock:
            if not self._speaking:
                return
            if self.last_feed_at is None:
                return
            idle = time.time() - self.last_feed_at
            if idle <= 0:
                return
            # PTT 抬起延迟窗口（defer_frames 仅被 feed 逐帧递减，断帧时按时间折算）
            if self._defer_frames > 0:
                defer_s = self._defer_frames * 0.02
                if idle >= defer_s:
                    self._defer_frames = 0
                    self._silence_s += idle - defer_s
                else:
                    self._defer_frames -= max(1, int(idle / 0.02))
                    return                     # 延迟窗口内：不推进静音
            else:
                self._silence_s += idle
            if self._silence_s * 1000 >= self.silence_end_ms:
                self._finalize()

    def force_finalize(self, defer_ms=0):
        """外部（如 UserTalking 结束信令）要求结束当前语音段。
        - defer_ms=0（默认）：立即切段（讲话人切换用，不吞尾字）
        - defer_ms>0（PTT 抬起）：延迟 defer_ms 毫秒再切，期间若又检测到声音
          （断续/中继台转发停顿）则取消收尾；避免"一句话没说完就断"。"""
        with self._lock:
            if not self._speaking:
                return
            if defer_ms > 0:
                self._defer_frames = max(self._defer_frames, defer_ms // 20)
                return
            self._finalize()


# ====================== ASR 客户端（qwen3-asr-flash，OpenAI 兼容） ======================
# system 词表"回显"特征：服务端把 system 词表当输入文本转写时，返回内容
# 必然包含这些引导语（实测 2026-09-18 21:55:22 完整回显）。
VOCAB_MARKERS = ("以下为背景实体词表", "业余无线电点名应答转写", "请优先正确识别")


class AsrClient:
    """非流式转写：OpenAI 兼容 /chat/completions，System Message 传实体词表
    （点名名单、解释法词表、已抄收呼号）提升呼号识别。纯标准库，无新增依赖。
    连接复用：持有一个 http.client.HTTPSConnection 长连接（keep-alive），
    点名期间多次 ASR 共用同一条 TCP+TLS，省去每次请求的握手开销
    （此前每次 transcribe 都新建连接——用户实测"每次 ASR 都要重新发送一次 TCP"）。

    官方文档（2026-09-17 更新）确认 system 消息受支持且必须放 messages 第一位，
    仅千问3-ASR-Flash 支持，用于提供上下文/实体词表。但实测带 system 返回 400
    （InternalError.Algo.InvalidParameter: ... does not support this input）。
    自动降级链：带 system → 去 system → 再去 asr_options，保证点名不中断；
    --asr-probe 可逐变体探测 system 的正确格式（content 字符串/数组/纯词表）。"""

    def __init__(self, api_key="", model="qwen3-asr-flash",
                 base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                 enable_itn=True, language="", timeout=30):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.enable_itn = enable_itn
        self.language = language
        self.timeout = timeout
        self._conn = None                       # 复用的 HTTPS 长连接（懒创建）
        self._conn_host = None
        self._conn_port = None
        self._lock = threading.Lock()           # 长连接非线程安全，串行化所有请求

    # ---------- 请求体构造（sys_style 供 system 词表格式探测） ----------
    def _build_body(self, wav_bytes, context_words=None, with_asr_opts=True,
                    sys_style="list"):
        """构造请求体。sys_style：
        - "list" ：system.content 为 [{"type":"text","text":...}]（生产默认；
                   实测 2026-09-18 只有此格式被服务端接受，其余均 400）
        - "str"  ：system.content 为纯字符串（带指令性引导语）→ 实测 400
        - "bare" ：system.content 仅为词表本身字符串 → 实测 400
        - "none" ：不带 system（降级兜底）"""
        uri = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii")
        messages = []
        if context_words and sys_style != "none":
            vocab = "、".join(context_words)
            if sys_style == "list":
                sys_content = [{"type": "text", "text":
                                "业余无线电点名应答转写。以下为背景实体词表，"
                                "请优先正确识别：" + vocab}]
            elif sys_style == "bare":
                sys_content = vocab
            else:
                sys_content = ("业余无线电点名应答转写。以下为背景实体词表，"
                               "请优先正确识别：" + vocab)
            messages.append({"role": "system", "content": sys_content})
        messages.append({"role": "user",
                         "content": [{"type": "input_audio",
                                      "input_audio": {"data": uri}}]})
        body = {"model": self.model, "messages": messages, "stream": False}
        if with_asr_opts:
            asr_opts = {"enable_itn": bool(self.enable_itn)}
            if self.language:
                asr_opts["language"] = self.language
            body["asr_options"] = asr_opts
        return body

    # ---------- 长连接请求 ----------
    def _request(self, body, retry_conn=True):
        """POST JSON 到 /chat/completions，复用长连接。返回 (status, body_bytes)。
        响应体必须读完（http.client 才能继续复用连接）；连接被服务端关闭
        （空闲超时/断链）时重建一次重试。"""
        from urllib.parse import urlparse
        u = urlparse(self.base_url)
        path = u.path.rstrip("/") + "/chat/completions"
        if self._conn is None or (u.hostname, u.port or 443) != (self._conn_host, self._conn_port):
            self._conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, timeout=self.timeout)
            self._conn_host, self._conn_port = u.hostname, u.port or 443
        conn = self._conn
        req_body = json.dumps(body).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.api_key,
                   "Content-Type": "application/json",
                   "Connection": "keep-alive"}
        try:
            conn.request("POST", path, body=req_body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()                  # 读完 body 才能复用连接
            return resp.status, data
        except (http.client.HTTPException, OSError) as e:
            if retry_conn:
                # 长连接被服务端关闭（空闲超时等）→ 重建后重试一次（幂等 POST，安全）
                logger.info(f"ASR 长连接失效，重建重试（{type(e).__name__}）")
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                return self._request(body, retry_conn=False)
            raise

    def transcribe(self, wav_bytes, context_words=None, sys_style=None):
        # 并发保护：发射/识别线程化后存在多路径调用风险，长连接必须串行
        with self._lock:
            return self._transcribe_locked(wav_bytes, context_words, sys_style)

    def _transcribe_locked(self, wav_bytes, context_words=None, sys_style=None):
        # 自动降级重试：system 词表实测须用数组格式（list），字符串格式报 400
        # （does not support this input）。链：list → 去 system → 再去 asr_options。
        # sys_style 显式传入时（--asr-probe）不降级、按指定格式试。
        # 生产默认 asr.sys_style="none"（实测 list 格式偶发被服务端"词表回显"——
        # 服务端把 system 词表当输入文本转写返回，见 21:55:22 日志）。
        for attempt in (1, 2, 3):
            style = sys_style if sys_style is not None else (
                nc_cfg("asr", "sys_style", default="none") if attempt == 1 else "none")
            body = self._build_body(
                wav_bytes,
                context_words if style != "none" else None,
                with_asr_opts=(attempt <= 2),
                sys_style=style)
            status, data = self._request(body)
            detail = data.decode("utf-8", "replace")
            if status == 200:
                out = json.loads(detail)
                content = (out.get("choices") or [{}])[0].get(
                    "message", {}).get("content", "")
                # 官方响应 content 标注为 array（示例为字符串），两种都兼容
                if isinstance(content, list):
                    content = "".join(
                        str(x.get("text", "")) if isinstance(x, dict) else str(x)
                        for x in content)
                content = content.strip()
                # 词表回显校验：返回内容含词表引导语特征 = 服务端把 system
                # 词表当输入转写（21:55:22 实测），判定无效 → 去掉 system 重试
                if style != "none" and content and any(
                        m in content for m in VOCAB_MARKERS):
                    logger.warning("ASR 返回词表回显（system 被服务端当输入转写），"
                                   "去掉词表重试")
                    continue
                return content
            logger.error(f"ASR HTTP {status}: {detail[:600]}")
            if (status == 400 and attempt < 3 and sys_style is None
                    and ("does not support this input" in detail
                         or "InvalidParameter" in detail)):
                if attempt == 1:
                    logger.warning("ASR 400：尝试去掉 system 词表消息重试"
                                   "（OpenAI 兼容可能仅支持单组 user 消息）")
                else:
                    logger.warning("ASR 400：尝试去掉 asr_options 重试")
                continue
            raise RuntimeError(f"ASR HTTP {status}: {detail[:200]}")
        else:
            raise RuntimeError("ASR 请求连续失败")


# ====================== LLM 客户端（可选，GLM-4.5-Flash） ======================
class LlmClient:
    """信息提取兜底：主路径是确定性解码，LLM 仅当 net_control.llm.enabled=true
    时用于低置信度修复/备注/名字提取。默认关闭，零成本。"""

    def __init__(self, api_key="", model="glm-4.5-flash",
                 base_url="https://open.bigmodel.cn/api/paas/v4", timeout=30):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._sys = ("你是业余无线电点名应答的信息提取器。从转录文本中提取："
                     "呼号（如BH3XXX，注意字母解释法词已展开成字母）、信号报告（如59）、"
                     "是否抄收（true/false）、备注。只输出JSON："
                     '{"callsign":"","signal":"","copied":true,"note":""}，'
                     "无法确定则字段留空。")

    def extract(self, text):
        body = {"model": self.model, "messages": [
            {"role": "system", "content": self._sys},
            {"role": "user", "content": text}], "temperature": 0}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        content = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}


# ====================== TTS（独立于 announce.py，避免循环依赖） ======================
def _cosyvoice_audio_format(fmt, sample_rate):
    """dashscope tts_v2 的 AudioFormat 枚举：采样率已合并在 format 里（如 MP3_24000HZ_MONO_256KBPS）。
    按 config 的 cosyvoice_format + cosyvoice_sample_rate 映射；未知组合回退默认。"""
    from dashscope.audio.tts_v2.speech_synthesizer import AudioFormat
    fmt = (fmt or "mp3").lower()
    sr = int(sample_rate or 24000)
    if fmt == "mp3":
        return getattr(AudioFormat, f"MP3_{sr}HZ_MONO_256KBPS",
                       AudioFormat.MP3_24000HZ_MONO_256KBPS)
    if fmt == "wav":
        return getattr(AudioFormat, f"WAV_{sr}HZ_MONO_16BIT",
                       AudioFormat.WAV_24000HZ_MONO_16BIT)
    if fmt == "pcm":
        return getattr(AudioFormat, f"PCM_{sr}HZ_MONO_16BIT",
                       AudioFormat.PCM_24000HZ_MONO_16BIT)
    if fmt == "opus":
        return getattr(AudioFormat, f"OGG_OPUS_{int(sr/1000)}KHZ_MONO_32KBPS",
                       AudioFormat.OGG_OPUS_24KHZ_MONO_32KBPS)
    return AudioFormat.MP3_24000HZ_MONO_256KBPS


def _cosyvoice_synth_sdk(text, cache_path, timeout=28):
    """阿里云百炼 CosyVoice 官方 SDK 合成（dashscope.audio.tts_v2.SpeechSynthesizer，
    WebSocket 流式，与官方示例一致；首包延迟低，支持 cosyvoice-v3.5-plus/flash 复刻音色）。
    写入 mp3 文件。复用 asr.api_key；模型/音色见 tts.cosyvoice_*。
    SDK 版本签名差异（1.18 老签名 sample_rate/rate/pitch ↔ 1.2x+ 新签名
    format 枚举含采样率 + speech_rate/pitch_rate）用 TypeError 探测自动适配。"""
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer
    api_key = (direct_announce.cfg_get("net_control", "asr", "api_key", default="")
               or direct_announce.cfg_get("asr", "api_key", default=""))
    model = direct_announce.cfg_get("tts", "cosyvoice_model",
                                    default="cosyvoice-v3.5-flash")
    voice = direct_announce.cfg_get("tts", "cosyvoice_voice", default="")
    if not api_key or not voice:
        missing = [f for f, v in (("asr.api_key", api_key),
                                  ("tts.cosyvoice_voice", voice)) if not v]
        raise RuntimeError("CosyVoice 未配置：" + "、".join(missing)
                           + "（config.json 中该字段为空或缺失）")
    dashscope.api_key = api_key
    fmt = direct_announce.cfg_get("tts", "cosyvoice_format", default="mp3")
    sr = int(direct_announce.cfg_get("tts", "cosyvoice_sample_rate", default=24000))
    vol = int(direct_announce.cfg_get("tts", "cosyvoice_volume", default=50))
    rate = float(direct_announce.cfg_get("tts", "cosyvoice_rate", default=1.0))
    pitch = float(direct_announce.cfg_get("tts", "cosyvoice_pitch", default=1.0))
    try:                                  # 新签名（1.2x+）：AudioFormat 枚举 + speech_rate/pitch_rate
        synthesizer = SpeechSynthesizer(
            model=model, voice=voice,
            format=_cosyvoice_audio_format(fmt, sr),
            volume=vol, speech_rate=rate, pitch_rate=pitch)
    except TypeError:                     # 旧签名（1.18）：sample_rate/rate/pitch
        synthesizer = SpeechSynthesizer(
            model=model, voice=voice,
            format=fmt, sample_rate=sr, volume=vol,
            rate=rate, pitch=pitch, timeout=timeout)
    try:
        audio = synthesizer.call(text, timeout_millis=timeout * 1000)
    except TypeError:                     # 旧版 call 无 timeout_millis
        audio = synthesizer.call(text)
    if not audio:
        raise RuntimeError("CosyVoice SDK 返回空音频")
    cache_path.write_bytes(audio)


def _cosyvoice_synth(text, cache_path, timeout=28):
    """阿里云百炼 CosyVoice HTTP 接口合成（兜底，SDK 不可用时使用），写入 mp3 文件。
    复用 asr.api_key（同一百炼账号、独立免费额度）；模型/音色见 tts.cosyvoice_*。
    - cosyvoice-v3.5-flash/v3.5-plus 仅华北2（北京）地域可用，且无系统音色，
      需先在百炼控制台"声音设计/声音复刻"创建音色，把音色 ID 填入 tts.cosyvoice_voice
    - 响应兼容 output.audio(base64) 与 output.audio_url 两种返回"""
    import base64
    import json
    import urllib.request
    api_key = (direct_announce.cfg_get("net_control", "asr", "api_key", default="") or direct_announce.cfg_get("asr", "api_key", default=""))
    model = direct_announce.cfg_get("tts", "cosyvoice_model",
                                    default="cosyvoice-v3.5-flash")
    voice = direct_announce.cfg_get("tts", "cosyvoice_voice", default="")
    base = direct_announce.cfg_get("tts", "cosyvoice_base", default=(
        "https://dashscope.aliyuncs.com/api/v1/services/"
        "aigc/multimodal-generation/generation"))
    if not api_key or not voice:
        missing = [f for f, v in (("asr.api_key", api_key),
                                  ("tts.cosyvoice_voice", voice)) if not v]
        raise RuntimeError("CosyVoice 未配置：" + "、".join(missing)
                           + "（config.json 中该字段为空或缺失）")
    payload = {
        "model": model,
        "input": {"text": text, "voice": voice},
        "parameters": {
            "format": direct_announce.cfg_get("tts", "cosyvoice_format", default="mp3"),
            "sample_rate": int(direct_announce.cfg_get(
                "tts", "cosyvoice_sample_rate", default=24000)),
            "volume": int(direct_announce.cfg_get(
                "tts", "cosyvoice_volume", default=50)),
            "rate": float(direct_announce.cfg_get(
                "tts", "cosyvoice_rate", default=1.0)),
            "pitch": float(direct_announce.cfg_get(
                "tts", "cosyvoice_pitch", default=1.0)),
        },
    }
    req = urllib.request.Request(
        base, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    output = data.get("output", {}) or {}
    audio = output.get("audio")
    if audio:
        cache_path.write_bytes(base64.b64decode(audio))
        return
    audio_url = output.get("audio_url")
    if audio_url:
        with urllib.request.urlopen(audio_url, timeout=timeout) as r:
            cache_path.write_bytes(r.read())
        return
    raise RuntimeError(f"CosyVoice 无音频返回: {data.get('message') or data}")


def synth_text(text, voice=None, cache_dir=None, timeout_inner=28, timeout_join=30,
               max_retries=2, retry_delay=5.0):
    """点名 TTS 合成（懒加载），返回 mp3 路径；失败返回空串。
    tts.engine=cosyvoice（默认）→ 阿里云百炼 CosyVoice：优先官方 SDK（tts_v2 WebSocket，
    与官方示例一致），SDK 未安装时自动回退 HTTP 接口；复用 asr.api_key。
    tts.engine=edge → Edge-TTS 兜底。与 announce.py 同款"缓存 + dry-run 校验"，键为全文 md5。"""
    import hashlib
    engine = direct_announce.cfg_get("tts", "engine", default="cosyvoice")
    if voice is None:
        voice = direct_announce.cfg_get("tts", "voice", default="zh-CN-XiaoxiaoNeural")
    if cache_dir is None:
        cache_dir = nc_cfg("tts_cache_dir", default=direct_announce.cfg_get(
            "paths", "cache_dir", default="/app/tts_cache"))
    cache_path = Path(cache_dir) / f"nc_{hashlib.md5(text.encode('utf-8')).hexdigest()}.mp3"
    if cache_path.exists() and direct_announce.dry_validate_mp3(cache_path):
        return str(cache_path)
    for attempt in range(1, max_retries + 1):
        err = []
        def _syn():
            try:
                if engine == "cosyvoice":
                    if direct_announce.cfg_get("tts", "cosyvoice_sdk", default=True):
                        try:
                            _cosyvoice_synth_sdk(text, cache_path, timeout=timeout_inner)
                            return
                        except ImportError:
                            logger.warning("dashscope SDK 未安装，点名 TTS 回退 HTTP 接口")
                    _cosyvoice_synth(text, cache_path, timeout=timeout_inner)
                else:
                    import asyncio
                    import edge_tts
                    async def _run():
                        await edge_tts.Communicate(text, voice).save(str(cache_path))
                    asyncio.run(asyncio.wait_for(_run(), timeout=timeout_inner))
            except Exception as e:
                err.append(f"{type(e).__name__}: {e}")
        t = threading.Thread(target=_syn, daemon=True)
        t.start()
        t.join(timeout=timeout_join)
        if not t.is_alive() and cache_path.exists() and direct_announce.dry_validate_mp3(cache_path):
            return str(cache_path)
        if err:
            logger.warning(f"点名 TTS 合成失败 (第{attempt}/{max_retries}次): {err[0]}")
        if cache_path.exists():
            try:
                cache_path.unlink()
            except Exception:
                pass
        if attempt < max_retries:
            time.sleep(retry_delay)
    return ""


class _StdoutToLogger:
    """把 direct_announce 的 print 诊断重定向进点名日志（与 announce.py 同款）。"""
    def __init__(self, log):
        self._log = log
    def write(self, s):
        s = s.rstrip()
        if s:
            self._log.info("[点名直连] " + s)
    def flush(self):
        pass


# ====================== 点名会话（状态机） ======================
class NetControlSession:
    """点名主播会话（开放点名 / 固定名单两种模式）。
    - 接收：link.on_downlink（keeper 线程）→ VoiceCapture → 段队列 → 会话线程
    - 播报：TTS → build_audio → link.ensure_session()(busy) → take_mic/play → release()
    - 与定时播报共享同一条常驻链路：busy 锁保证同一时刻只有一个发射者
    - 低置信度：播报"请重复一遍呼号"（限次）→ 仍失败记未抄收，点名不中断
    """
    def __init__(self, link, tts_func=None, on_done=None):
        self.link = link                        # direct_announce.PersistentAnnouncer
        self.tts_func = tts_func                # 外部注入（announce.get_tts_file）；None 用内置
        self.on_done = on_done                  # on_done(summary_dict)
        self._stop = threading.Event()
        self._thread = None
        self._seg_queue = queue.Queue(maxsize=64)
        self._checked_in = []  # [(call, signal, wav, raw, info)]  info=后续补充信息（QTH/设备等）
        self._checked_calls = set()
        self._dups = 0
        self._failed = 0
        self._retry_pending = False
        self._retry_left = int(nc_cfg("max_retry", default=1))
        # 拼读合并缓存：session → {"text", "ts"}。友台被请重复呼号后常用
        # 逐字母解释法拼读（"Bravo / Golf / Nine / ..."），VAD 按字母间停顿
        # 切成 0.5~1.5s 短段逐段 ASR（实测 21:15 场景），单段拼不出呼号 →
        # 同 session 短段文本累积，停顿后统一解码。
        self._spell_buf = {}
        # "当前正在点名"的友台上下文：抄收呼号后保留，后续不带呼号的补充段
        # （QTH/设备/天线/功率等）按来源 session 归入该友台，不再当"未抄收"。
        self._current_call = None           # 当前台上友台呼号（大写）
        self._current_session = None        # 抄收该呼号的语音段来源 session
        self._current_entry = None          # 指向 _checked_in 中该友台的条目（引用）
        self._current_active = False        # 当前友台流程进行中（抄收→追问→确认窗口）：
                                            # 期间收到新呼号视为插队→静默记录+排队，不打断
        self._waiting = []                  # 等候排队的插队者（{"call","signal","session"}）
        self._pending_at = None             # 最近一次补充信息段时刻：友台一句话被 VAD
                                            # 切成多段时，合并累积、停稳后统一确认一次
        self._last_confirmed_info = {}      # 各友台最近一次合并确认播报过的 entry[4]（防重复）
        # 结构化字段永久化：呼号 → {qth, device, antenna, power, signal}（CSV 导出用）
        self._fields = {}
        self._checkin_times = {}            # 呼号 → 抄收时刻 ISO 串（CSV 导出用）
        self._csv_path = None               # 实时点名记录 CSV 路径（点名开始即建，持续更新）
        self._last_tx_end = None            # 最近一次发射结束时刻（中继台回波过滤用）
        self._speech_seq = 0                # 播报序号：新话术生成即 +1；等待发射的旧话术
                                            # 检测到序号前进就放弃（只播最新，接话不滞后）
        self._last_activity = time.time()       # 最近一次应答活动时间（"到点后安静N秒"判定用）
        self._talking_gate = bool(nc_cfg("use_talking_gate", default=True))
        self._talking_sessions = set()          # 服务器已广播"开始讲话"的远端 session
        self._talking_active = {}               # session → 最后语音活动时刻（陈旧超时用）
        self._ever_talk = False                 # 本会话是否收到过任何开始讲话信令
        self._gate_started = time.time()
        self._preempted = threading.Event()     # 播报发射中检测到他人讲话（抢占让位）
        self._asr = None
        self._llm = None
        self._llm_calls = 0                 # 每轮点名 LLM 兜底调用计数（防超时拖死）
        self._llm_fails = 0                 # 连续失败计数（熔断：≥2 本轮停用）
        self._play_lock = threading.Lock()  # 发射串行化：嵌套播报排队，不并发抢麦
        self._decoder = None
        self._capture = None
        self._asked_fields = {}      # call → set(已追问过的缺失字段)：结构化追问每字段最多一次
        self._net_ctx = {}          # 话术模板上下文（_run 启动时填充）
        self._started_at = None
        self.summary = {}
        self.done = threading.Event()

    # ---------- 生命周期 ----------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="net-control")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------- 模板上下文（TTS 动态占位符） ----------
    # 话术模板支持 {date}/{weekday}/{time}/{net_name}/{repeater_call}/{ctrl_call}/
    # {ctrl_phonetic}/{main_qth}/{main_device}/{main_antenna}/{main_power}/
    # {frequency}/{offset}/{tone}（会话级，net_control.net 段配置，date/time 实时刷新）
    # 与 {call}/{call_phonetic}/{report}/{n}/{calls}（应答级）。
    def _fmt(self, text, **extra):
        ctx = dict(self._net_ctx)
        now = datetime.datetime.now()
        ctx["date"] = f"{now.year}年{now.month}月{now.day}日"
        ctx["time"] = f"{now.hour}点{now.minute:02d}分"
        ctx.update(extra)
        try:
            return text.format(**ctx)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"话术模板占位符错误: {e} → 原样播报")
            return text

    # ---------- 会话主流程 ----------
    def _run(self):
        self._started_at = datetime.datetime.now()
        wd = "一二三四五六日"[self._started_at.weekday()]
        self._net_ctx = {
            "weekday": f"周{wd}",
            "net_name": nc_cfg("net", "net_name", default="业余无线电中继台应急通讯演练台网点名"),
            "repeater_call": nc_cfg("net", "repeater_call", default="BR9AB"),
            "ctrl_call": nc_cfg("net", "ctrl_call", default="BI9BZW"),
            "ctrl_phonetic": nc_cfg("net", "ctrl_phonetic",
                                    default="Bravo India Nine Bravo Zulu Whiskey"),
            "main_qth": nc_cfg("net", "main_qth", default=""),
            "main_device": nc_cfg("net", "main_device", default=""),
            "main_antenna": nc_cfg("net", "main_antenna", default=""),
            "main_power": nc_cfg("net", "main_power", default=""),
            "frequency": nc_cfg("net", "frequency", default=""),
            "offset": nc_cfg("net", "offset", default=""),
            "tone": nc_cfg("net", "tone", default=""),
        }
        logger.info("点名主播会话开始")
        try:
            self._ensure_capture()
            self._ensure_csv()          # 实时记录：点名一开始即建 CSV，中断也不丢
            # 点名接收侧复用常驻链路（_on_downlink 挂在 PersistentAnnouncer 上）：
            # 开场白前先等常驻就绪（最多 8s），避免 ensure_session 不健康走临时
            # 短链 → suspend 断开常驻 → 开场白后重连空窗漏掉友台首答
            # （实测 21:12:14 开场白 → 21:12:16 重连，2s 空窗）。
            if self.link is not None:
                for _ in range(8):
                    if self._stop.is_set():
                        break
                    s = self.link.ensure_session()
                    if s is not None:
                        self.link.release()
                        break
                    time.sleep(1.0)
            self._speak(self._fmt(nc_cfg("opening_text", default=
                "CQ CQ CQ，这里是{repeater_call}业余无线电中继台，现在是每周{weekday}晚"
                "{net_name}，我是今晚主控{ctrl_call}，今天是{date}，现在是北京时间{time}，"
                "我的QTH位于{main_qth}，所用设备{main_device}，{main_antenna}，"
                "{main_power}功率发射，现在开始台网点名，请抄收到信号的友台依次上台报告"
                "你的呼号、QTH、使用设备、天线、功率以及抄收到主控台的信号报告，"
                "这里是{ctrl_phonetic} {ctrl_call}，Over")))
            if nc_cfg("roster_mode", default=False):
                self._run_roster()
            else:
                self._run_open()
            self._speak_summary()
        except Exception as e:
            logger.error(f"点名会话异常: {e}", exc_info=True)
        finally:
            self._teardown()

    def _run_open(self):
        """开放点名窗口：
        - 最短收听 listen_after_open_seconds（默认 60s）
        - 总时长 max_net_seconds（默认 1800s=30 分钟）：到点后不再强制立即结束，
          若仍有人在点名（正上麦应答），等其说完——连续 quiet_end_seconds（默认 10s）
          无任何应答活动才触发收尾
        - grace_seconds（默认 300s）为到点后的硬性宽限，防长时间持续讲话无限延长
        - 应答处理是同步的（ASR+TTS 播报期间不检查结束条件），天然"等人说完" """
        start = time.time()
        self._last_activity = start
        min_window = float(nc_cfg("listen_after_open_seconds", default=60))
        max_net = float(nc_cfg("max_net_seconds", default=1800))
        quiet_end = float(nc_cfg("quiet_end_seconds", default=10))
        grace = float(nc_cfg("grace_seconds", default=300))
        max_count = int(nc_cfg("max_checked_in", default=200))
        idle_gap = float(nc_cfg("idle_call_seconds", default=300))   # 空闲多久重新呼叫
        last_idle_call = start
        deadline = start + max(max_net, min_window)
        hard = deadline + grace
        logger.info(f"点名开放收听：最短 {min_window:.0f}s，总时长 {max_net:.0f}s，"
                    f"到点后连续 {quiet_end:.0f}s 无应答即收尾"
                    f"（硬上限 {hard - start:.0f}s；空闲 {idle_gap:.0f}s 重新呼叫）")
        while not self._stop.is_set():
            now = time.time()
            if len(self._checked_in) >= max_count:
                break
            if now >= hard:
                logger.warning(f"到达硬上限（宽限 {grace:.0f}s 已耗尽），强制收尾")
                break
            if now >= deadline and now - self._last_activity >= quiet_end:
                logger.info(f"已到点名总时长，且连续 {quiet_end:.0f}s 无应答，收尾")
                break
            # 空闲重新呼叫：长时间无应答活动 → 重播点名开始时的呼叫，邀请友台上台
            # （播报后的中继回波/应答会刷新 _last_activity，自然不会连续重播）
            if now - self._last_activity >= idle_gap \
                    and now - last_idle_call >= idle_gap:
                logger.info(f"点名已空闲 {now - self._last_activity:.0f}s，"
                            f"重新呼叫（{idle_gap:.0f}s 无应答）")
                self._speak(self._fmt(nc_cfg("idle_call_text", default=(
                    "CQ CQ CQ，这里是{repeater_call}业余无线电中继台，现在是{net_name}，"
                    "我是主控{ctrl_call}，点名继续开放，请抄收到信号的友台依次上台报告"
                    "呼号、QTH、使用设备、天线、功率，这里是{ctrl_phonetic} {ctrl_call}，Over")),
                    ))
                last_idle_call = now
            try:
                pcm16, dur, wav, session = self._seg_queue.get(timeout=1.0)
            except queue.Empty:
                self._capture_pump()       # 时间驱动收尾：链路静默时也切段（不靠下一个人顶）
                self._flush_pending_report()   # 合并确认：友台说完停稳后统一播报
                self._spell_flush_all()        # 拼读合并：拼读停止后统一解码
                continue
            self._capture_pump()
            self._process_segment(pcm16, dur, wav, session)

    def _run_roster(self):
        roster = [str(x).upper() for x in (nc_cfg("roster", default=[]) or [])]
        max_secs = float(nc_cfg("roster_max_seconds", default=1800))
        started = time.time()
        logger.info(f"固定名单点名，共 {len(roster)} 位（总时长上限 {max_secs:.0f}s）")
        for call in roster:
            if self._stop.is_set():
                break
            if time.time() - started >= max_secs:
                logger.warning(f"固定名单到达总时长上限 {max_secs:.0f}s，跳过剩余名单")
                break
            self._speak(self._fmt(nc_cfg("roster_call_text", default="请{call_phonetic}回答。Over"),
                                  call=call, call_phonetic=callsign_phonetic(call)))
            try:
                pcm16, dur, wav, session = self._seg_queue.get(
                    timeout=float(nc_cfg("roster_call_timeout", default=12)))
            except queue.Empty:
                self._capture_pump()       # 时间驱动收尾：链路静默时也切段
                self._flush_pending_report()   # 合并确认：友台说完停稳后统一播报
                self._speak(self._fmt(nc_cfg("no_reply_text",
                                             default="无人应答，继续下一位。")))
                continue
            self._capture_pump()
            self._process_segment(pcm16, dur, wav, session)
        logger.info("固定名单点名结束")

    # ---------- 应答处理 ----------
    def _process_segment(self, pcm16, dur, wav, session=None):
        self._last_activity = time.time()
        logger.info(f"收到应答段 {dur:.1f}s（{Path(wav).name if wav else '未落盘'}）"
                    f"{' session=' + str(session) if session is not None else ''}")
        raw = self._asr_text(pcm16)
        logger.info(f"ASR: {raw}")
        res = decode_callsign(raw, regex=nc_cfg("callsign_regex", default=""))
        call, signal, score = res["callsign"], res["signal"], res["score"]
        # 中继台回波过滤：自己发射后/对方讲完后紧接的短段，先于一切处理丢弃。
        # 仅对"无新呼号/已抄收呼号"的段生效——解析出**新呼号**的段不可能是回波
        # （回波是同一人的尾音/自身播报，不会报出新呼号）。实测中继转发场景
        # 友台 A 讲完（"这里是BG9AA"）B 立即报名（"这里是BG9BB"），模板化开场
        # 文本重合≥70% + 同 session → 若按旧逻辑 B 的报名被当 A 的回波静默吞掉。
        if (call is None or call.upper() in self._checked_calls) \
                and self._is_echo(raw, dur):
            logger.info(f"疑似中继台回波，忽略（{raw!r} dur={dur:.1f}s"
                        f"{' session=' + str(session) if session is not None else ''}）")
            return
        thr = int(nc_cfg("confidence_threshold", default=60))
        # 空/纯语气词段（放麦尾音、环境声、回波残余）→ 静默：
        # 不播"请重复呼号"、不消耗重复请求额度（实测 20:15 放麦后 0.6s 空段
        # 触发"请重复"→ 30s 等待 → 抢麦失败的连锁）
        if not call and not clean_report_text(raw):
            logger.info(f"空/语气词段静默忽略（{raw!r}）")
            return
        # 报名意图（无呼号时全局生效，不依赖当前友台）：点名开始后无人成功
        # 抄收时，报名者的"请求参加测试点名"等话语若无呼号会落到低置信度分支
        # 被额度耗尽静默吞掉（实测 21:14:12"请求参加测试点名测试，是否收到？"）
        # → 引导重报完整呼号，不消耗重复额度。
        if call is None:
            info0 = clean_report_text(raw)
            if info0:
                kw0 = info0.lower()
                if any(k in kw0 for k in ("请求参加", "参加点名", "参加测试",
                                          "点名测试", "请求加入", "想参加",
                                          "参加一下", "报名")):
                    logger.info(f"报名意图但未解出呼号，引导重报: {info0}")
                    if self._current_call:
                        self._speak(self._fmt(nc_cfg("repeat_text", default=
                            "{call_phonetic}，请再报一次您的完整呼号，Over"),
                            call=self._current_call,
                            call_phonetic=callsign_phonetic(self._current_call)))
                    else:
                        self._speak("请再报一次您的完整呼号，Over")
                    return
        # 主控自身呼号：开场白/播报的回声、友台报主控呼号 → 不视为友台
        ctrl = (self._net_ctx or {}).get("ctrl_call", "")
        if call and ctrl and call.upper() == ctrl.upper():
            logger.info(f"主控自身呼号 {call}，忽略（回波/自我识别）")
            return
        if call and is_duplicate(call, self._checked_calls):
            return self._handle_duplicate(call, signal, raw)
        if call and score >= int(nc_cfg("confidence_threshold", default=60)):
            return self._handle_callsign(call, signal, score, raw, wav, session)
        # ---- 无呼号：先判断是否为"当前友台的信息补充段" ----
        # 拼读合并（无呼号/低分 + 短段 + **重复请求流程中**）：友台被请重复后
        # 逐字母解释法拼读（VAD 按字母间停顿切成短段，实测 21:15 场景）→
        # 累积同 session 短段文本统一解码，避免"请重复"刷屏与呼号丢失。
        # 仅在 retry 流程启用：确认/纠正等短段（"正确""对"）不在该流程，
        # 不会被吞；正常段直接解出呼号也走不到这里。
        _in_retry = (self._retry_pending or self._retry_left < int(
            nc_cfg("max_retry", default=1)))
        if (call is None or score < thr) and _in_retry \
                and self._is_spell_piece(raw, dur):
            if self._spell_feed(raw, dur, session, wav):
                return
        else:
            self._spell_buf.pop(session, None)   # 非拼读段/非retry：新话题，清缓存
        # 点名流程中友台报完呼号后，补充 QTH/设备/天线/功率时通常不再重复呼号
        # （17:26:51 实测段"我的QTH在咸阳市…设备即时通…五瓦功率发射"即此场景）。
        # 归入条件：无新呼号（有呼号低分也必须走重试/抄收，不能吞——
        # 实测 20:41 同 session 友台补报"这里是BJ九EFU"被当补充信息忽略）
        # 且 已有当前友台 且 段来源 session 与其一致（session 缺失时保守归入）。
        if (call is None
                and self._current_call is not None
                and (session is None or self._current_session is None
                     or session == self._current_session)):
            info = clean_report_text(raw)
            if not info:
                # 空段/纯语气词（0.5s 环境声、放麦尾音）→ 静默忽略：
                # 不归入不播报，避免"您的信息已记录"空刷屏（实测 19:14 连续两次）
                logger.info(f"{self._current_call} 空补充段忽略（{raw!r}）")
                return
            kw = info.lower()
            correct_kw = ("不对", "不正确", "错了", "纠正", "说错",
                          "重报", "听错", "抄错", "读错")
            confirm_kw = ("正确", "确认", "对的", "没问题", "收到了", "是的",
                          "对对对", "收到收到")
            # "不是"单独判断：排除反问/抱怨句式（"不是已经说过了吗？"），
            # 仅"不是"+具体内容（"不是，我的呼号是…"）才算纠正
            correct_hit = any(k in kw for k in correct_kw) or (
                "不是" in kw and not re.search(r"不是(?:已经|早就|刚|刚才|说|问|记|都|早就)", kw))
            ask_kw = ("是否抄收", "抄收到了吗", "抄收了吗", "是否收到",
                      "听得到吗", "听清了吗", "主控在吗", "在吗",
                      "能否抄收", "能否超收", "是否超收", "能不能抄收",
                      "是否超时", "超时了吗", "超收了吗", "可以了吗", "好了吗")
            if correct_hit:
                # 友台纠正（呼号/信息听错）→ 通常紧接着会重复正确的呼号/信息：
                # **先尝试从本段直接提取**（确定性低分呼号 + LLM 兜底），
                # 提取成功直接修正抄收，只有提取失败才请对方重报
                # （实测 21:10 "呼号不正确"段无正确信息→请重报；若含"我的呼号是…"应直接收）
                new_call, sig2, sc2 = None, None, 0
                if call is None or score < thr:
                    fix = self._llm_fix(raw, res)
                    if fix:
                        new_call, sig2, sc2 = fix["callsign"], fix["signal"], fix["score"]
                    elif call:          # 低分但格式合法
                        new_call, sig2, sc2 = call, signal, score
                elif call:
                    new_call, sig2, sc2 = call, signal, score
                if new_call:
                    cur = (self._current_call or "").upper()
                    if new_call.upper() == cur:
                        # 纠正段确认了当前友台呼号无误 → 收尾确认（不再请重报）
                        logger.info(f"{self._current_call} 纠正后确认呼号无误: {info}")
                        self._speak(self._fmt(nc_cfg("correct_confirm_text", default=
                            "抄收，{call_phonetic}，呼号确认无误，信息已记录，请下一位友台。Over"),
                            call=self._current_call,
                            call_phonetic=callsign_phonetic(self._current_call)))
                        self._end_current(flush=False)   # 友台已确认，不再重复问
                        return
                    if is_duplicate(new_call, self._checked_calls):
                        logger.info(f"重复抄收 {new_call}（纠正提取），跳过")
                        self._dups += 1
                        self._retry_pending = False
                        self._retry_left = int(nc_cfg("max_retry", default=1))
                        self._speak(self._fmt(nc_cfg("dup_text", default=
                            "{call_phonetic} 已经抄收过，请下一位友台。"),
                            call=new_call, call_phonetic=callsign_phonetic(new_call)))
                        self._end_current()   # 收尾窗口：轮到排队中的下一位
                        return
                    # 替换抄收：纠正=之前抄错，用新呼号替换当前友台旧记录
                    for i, e in enumerate(self._checked_in):
                        if e[0].upper() == cur:
                            self._checked_in.pop(i)
                            break
                    self._checked_calls.discard(cur.upper())
                    self._fields.pop(cur.upper(), None)
                    self._asked_fields.pop(cur.upper(), None)   # 纠正替换：追问计数随旧记录清除
                    self._checkin_times.pop(cur.upper(), None)
                    self._current_call = None     # 由 _do_checkin 重建上下文
                    self._current_session = None
                    self._current_entry = None
                    logger.info(f"抄收修正: {cur or '无'} → {new_call}（{info}）")
                    self._do_checkin(new_call, sig2, wav, raw, session, sc2)
                    return
                logger.info(f"{self._current_call} 纠正请求（未提取到正确信息）: {info}")
                self._speak(self._fmt(nc_cfg("correct_text", default=
                    "抱歉，刚才抄收可能有误，请您再重复一遍，Over"),
                    call=self._current_call,
                    call_phonetic=callsign_phonetic(self._current_call)))
                return
            if len(info) <= 12 and any(k in kw for k in confirm_kw) \
                    and not any(k in kw for k in correct_kw):
                # 短确认语（"正确""收到""没问题"）→ 确认收尾，请下一位
                # （排除纠正词：'不正确'含'正确'子串，先命中 correct 分支；
                #   长度放宽到 12 以容纳'呼号正确，没问题'等完整确认）
                logger.info(f"{self._current_call} 确认收到: {info}")
                # 确认后若核心结构化字段仍缺失 → 追问缺失项（每字段最多一次）
                if self._ask_missing(self._current_call):
                    return
                self._speak(self._fmt(nc_cfg("confirm_text", default=
                    "抄收，{call_phonetic}，感谢确认，请下一位友台。Over"),
                    call=self._current_call,
                    call_phonetic=callsign_phonetic(self._current_call)))
                self._end_current(flush=False)   # 友台已确认，不再重复问"是否正确"
                return
            # 结构化字段提取：确认的是结构化内容（QTH/设备/天线/功率/信号），
            # 不再把整句话原样复诵（实测 19:47 "主控是否抄收"被复诵成废话）
            fields = extract_report_fields(info)
            if fields:
                self._apply_report_fields(self._current_call, info, signal)
                return
            # 无结构化字段的文本分类：
            if any(k in kw for k in ask_kw):
                # 友台询问是否抄收/主控在吗 → 确认抄收并引导补报信息
                logger.info(f"{self._current_call} 询问抄收状态: {info}")
                self._speak(self._fmt(nc_cfg("ask_ack_text", default=
                    "抄收，{call_phonetic}，您的呼号已记录，"
                    "请报告您的QTH、使用设备、天线、功率，Over"),
                    call=self._current_call,
                    call_phonetic=callsign_phonetic(self._current_call)))
                return
            if "呼号" in kw:
                # 友台在补报呼号（"我的呼号是BG9"之类未拼完整）→ 请其报完整呼号
                logger.info(f"{self._current_call} 补报呼号: {info}")
                self._speak(self._fmt(nc_cfg("repeat_text", default=
                    "{call_phonetic}，请再报一次您的完整呼号，Over"),
                    call=self._current_call,
                    call_phonetic=callsign_phonetic(self._current_call)))
                return
            # 报名意图（"这里B九B L Z请求参加点名测试"实测 20:15:15：呼号被
            # ASR 漏字母未解出，不能静默吞掉）→ 引导重报完整呼号
            join_kw = ("请求参加", "参加点名", "参加测试", "点名测试",
                       "请求加入", "想参加", "参加一下", "报名")
            if any(k in kw for k in join_kw):
                logger.info(f"{self._current_call} 报名意图但未解出呼号，引导重报: {info}")
                self._speak(self._fmt(nc_cfg("repeat_text", default=
                    "{call_phonetic}，请再报一次您的完整呼号，Over"),
                    call=self._current_call,
                    call_phonetic=callsign_phonetic(self._current_call)))
                return
            # 无实义（"那主播""哦，这里是"等）→ 静默忽略，不归入不播报
            logger.info(f"{self._current_call} 无结构化信息且无关键词，忽略: {info}")
            return
        # LLM 兜底（可选，llm.enabled=true）：确定性解码低置信度/未解出呼号且
        # 文本非空时，先让 LLM 尝试修复呼号与信号——命中直接按抄收处理，
        # 避免"请重复"空耗一轮；失败/未启用则原样走低置信度流程
        if (call is None or score < thr) and clean_report_text(raw) \
                and self._looks_like_report(raw):
            fix = self._llm_fix(raw, res)
            if fix:
                call2, sig2, score2 = fix["callsign"], fix["signal"], fix["score"]
                if is_duplicate(call2, self._checked_calls):
                    logger.info(f"重复抄收 {call2}（LLM 修复），跳过")
                    self._dups += 1
                    self._retry_pending = False
                    self._retry_left = int(nc_cfg("max_retry", default=1))
                    self._speak(self._fmt(nc_cfg("dup_text", default=
                        "{call_phonetic} 已经抄收过，请下一位友台。"),
                        call=call2, call_phonetic=callsign_phonetic(call2)))
                    return
                return self._do_checkin(call2, sig2, wav, raw, session, score2)
        # 低置信度：请求重复（限次）
        if self._retry_pending or self._retry_left <= 0:
            logger.warning(f"未抄收（{'/'.join(res['reasons'])}）文本: {raw}")
            self._failed += 1
            self._retry_pending = False
            if raw and raw.strip():
                logger.info("重复请求额度已用尽，静默等待下一位友台")
            return
        self._retry_pending = True
        self._retry_left -= 1
        target = call or ""
        report_kw = ("QTH", "qth", "Q T", "Q T H", "设备", "天线", "功率", "瓦", "信号")
        if any(k in (raw or "") for k in report_kw):
            # 友台已报位置/设备等详细信息但呼号缺失（且非当前友台）→ 确认抄收并礼貌请其补报呼号
            logger.info(f"置信度 {score}，已识别报告内容但缺呼号，请求补报呼号"
                        f"（剩余额度 {self._retry_left}）")
            self._speak(self._fmt(nc_cfg("repeat_report_text", default=
                "抄收您的位置与设备信息，请再报一次您的呼号，Over")))
        else:
            logger.info(f"置信度 {score}，请求重复呼号（剩余额度 {self._retry_left}）")
            self._speak(self._fmt(nc_cfg("repeat_text", default=
                "{call_phonetic}，请重复一遍您的呼号。"), call=target,
                call_phonetic=callsign_phonetic(target) or "上一位友台"))

    def _missing_fields(self, call):
        """返回该友台尚未记录的核心结构化字段列表（signal/qth/device/antenna/power）。
        "没有天线""没有功率"等已作为值记录 → 视为已填，不再追问。"""
        call = (call or "").upper()
        fd = self._fields.get(call, {}) or {}
        return [k for k in REQUIRED_FIELDS
                if not str(fd.get(k) or "").strip()]

    def _apply_report_fields(self, call, info_text, signal=None):
        """结构化字段落地：记录 entry[4]/信号 → 永久化 _fields → 实时落盘 CSV →
        置 _pending_at 等合并确认（同一句话被 VAD 切碎时只播一次）。
        补充信息分支与"重复抄收但补报缺失字段"（20:51:26 实测"抄你的信号五九"）
        共用。"""
        call = (call or "").upper()
        entry = self._current_entry
        fields = extract_report_fields(info_text)
        if not fields:
            return False
        prev_info = entry[4] or "" if entry else ""
        if entry is not None:
            entry[4] = (prev_info + " " + info_text).strip()
            if signal and not entry[1]:
                entry[1] = signal
        field_str = "、".join(
            f"{FIELD_LABEL.get(k, k)} {v}" for k, v in fields)
        logger.info(f"{call} 补充信息（结构化 {len(fields)} 项）：{field_str}")
        old = self._fields.get(call, {}) or {}
        is_dup = all(str(old.get(k, "")) == v for k, v in fields)
        if is_dup:
            logger.info(f"{call} 重复补充信息忽略（已记录）: {field_str}")
            return False
        fd = dict(fields)
        if entry is not None and entry[1]:
            fd.setdefault("signal", entry[1])
        self._fields.setdefault(call, {}).update(fd)
        self._flush_csv()          # 实时落盘：结构化字段更新即写入
        # 合并确认：只累积字段、停稳 report_merge_gap_seconds 后统一确认一次
        self._pending_at = time.time()
        return True

    def _flush_pending_report(self, force=False):
        """合并确认：友台连续多段补充信息（同一句话被 VAD 切碎）→ 只播一次完整确认。
        距最后一段超过 report_merge_gap_seconds（默认 4s）或 force（收尾前）时触发；
        entry[4] 无新增内容（_last_confirmed_info 相同）则不重复播报。"""
        if self._pending_at is None or not self._current_active:
            self._pending_at = None
            return
        gap = float(nc_cfg("report_merge_gap_seconds", default=4))
        if not force and time.time() - self._pending_at < gap:
            return
        self._pending_at = None
        entry = self._current_entry
        if entry is None:
            return
        info = (entry[4] or "").strip()
        if not info or info == self._last_confirmed_info.get(self._current_call):
            return
        self._last_confirmed_info[self._current_call] = info
        fields = self._fields.get(self._current_call, {}) or {}
        if fields:
            field_str = "、".join(
                f"{FIELD_LABEL.get(k, k)} {v}" for k, v in fields.items())
        else:
            field_str = info
        logger.info(f"{self._current_call} 信息合并确认: {field_str}")
        tmpl = nc_cfg("info_ack_text", default=
            "抄收，{call_phonetic}，您的信息已记录：{fields}。是否正确？Over")
        if "{fields}" in tmpl:
            self._speak(self._fmt(tmpl, call=self._current_call,
                call_phonetic=callsign_phonetic(self._current_call),
                fields=field_str))
        else:                      # 用户自定义旧模板（无 {fields}）→ 兼容整句复诵
            self._speak(self._fmt(tmpl, call=self._current_call,
                call_phonetic=callsign_phonetic(self._current_call),
                info=info))
        # 注意：这里不再立即追问缺失字段（旧逻辑播完"是否正确？Over"后马上
        # 追一句"请补充信号、QTH、功率"，实测 22:06:16→22:06:34 主控连播两句、
        # 友台还没回答"是否正确"就被抢话）——友台先回答"是否正确/继续补充"，
        # 确认后（_process_segment 确认分支）再按缺失字段追问，节奏才正常。

    def _ask_missing(self, call):
        """结构化信息未记全 → 主动追问缺失项（每字段每友台最多问一次，防无限循环）。
        返回 True=已追问（调用方应 return）；False=无未问过的缺失字段。"""
        call = (call or "").upper()
        if not call:
            return False
        asked = self._asked_fields.setdefault(call, set())
        missing = [k for k in self._missing_fields(call) if k not in asked]
        if not missing:
            return False
        for k in missing:
            asked.add(k)
        labels = "、".join(FIELD_LABEL.get(k, k) for k in missing)
        logger.info(f"{call} 结构化信息缺失，追问: {labels}")
        self._speak(self._fmt(nc_cfg("missing_ask_text", default=
            "抄收，{call_phonetic}，信息已记录，请再补充您的{missing}，Over"),
            call=call, call_phonetic=callsign_phonetic(call), missing=labels))
        return True

    def _handle_duplicate(self, call, signal, raw):
        """重复抄收：先看本段是否在补报缺失字段（实测 20:51:26 友台重复报
        "抄你的信号五九"→ 信号59 应补录，而不是直接"已经抄收过"吞掉）。"""
        if self._current_call == call and self._apply_report_fields(call, raw, signal):
            logger.info(f"{call} 重复抄收但补报字段已记录: {raw}")
            return
        logger.info(f"重复抄收 {call}，跳过")
        self._dups += 1
        self._retry_pending = False
        self._retry_left = int(nc_cfg("max_retry", default=1))  # 有效应答，恢复重复请求额度
        self._speak(self._fmt(nc_cfg("dup_text", default="{call_phonetic} 已经抄收过，"
                                                         "请下一位友台。"),
                              call=call, call_phonetic=callsign_phonetic(call)))
        self._end_current()        # 重复抄收=收尾窗口，轮到排队中的下一位

    def _handle_callsign(self, call, signal, score, raw, wav, session):
        """有呼号且高分（≥置信度阈值）的统一处理。_process_segment 主分支；
        拼读合并 flush（碎段拼出完整呼号）也复用同一套分支——重复抄收 /
        识别修正 / 插队排队 / 正式抄收，行为与正常段完全一致。"""
        if self._current_active:
            # 当前友台进行中收到新呼号，两种语义：
            # ① 识别修正（同一友台纠正/ASR 听错）：新呼号与当前友台高度相似
            #    （编辑距离≤1，如 ASR 把 BFZ 听成 BLZ）→ 替换旧记录，不打断。
            #    仅限"刚抄收、尚未报信息"的早期窗口：已记录结构化信息后再
            #    重报（20:15:15 实测"BFZ 在等确认时又说话"）不覆盖已确认记录，
            #    回"呼号已记录"反馈。
            # ② 另一友台插队：呼号不相似（BG9AA vs BG9BB）→ 静默排队等候，
            #    不顶掉当前友台。
            # 判定依据是**呼号相似度而非 session**：中继转发场景所有友台
            # 同 session（用户实测确认），同 session 不代表同一人；反过来
            # 跨设备（不同 session）也可能是一人纠正。保守倾向排队——
            # 修正被误判为插队只多等一轮（不丢人），插队被误判为修正会
            # 顶掉当前友台（丢人）。
            if (self._current_call or "").upper() != call.upper():
                similar = callsign_similar(self._current_call, call)
                if similar:
                    if self._current_entry and self._current_entry[4]:
                        logger.info(f"{self._current_call} 已记录信息，重报 "
                                    f"{call} 不替换（识别修正仅限早期窗口）: {raw}")
                        self._speak(self._fmt(nc_cfg("ask_ack_text", default=
                            "抄收，{call_phonetic}，您的呼号已记录，"
                            "请报告您的QTH、使用设备、天线、功率，Over"),
                            call=self._current_call,
                            call_phonetic=callsign_phonetic(self._current_call)))
                        return
                    return self._replace_checkin(call, signal, wav, raw, session, score)
            # 呼号不相似（另一个人，无论 session）→ 插队：静默记录+排队，不打断
            return self._queue_interloper(call, signal, raw, session)
        return self._do_checkin(call, signal, wav, raw, session, score)

    # ---------- 拼读合并（VAD 切碎的逐字母解释法拼读） ----------
    @staticmethod
    def _is_spell_piece(raw, dur):
        """逐字母拼读片段特征：短段（≤2.5s）+ 短文本（≤16 字，含任意词）。
        刻意宽松（不要求含解释法词）——ASR 对解释法单词的听写很乱
        （21:15 实测 "无奈"=Nine、"弗雷"=Foxtrot），严格过滤会漏掉真实拼读；
        宽松进缓存的乱文本 decode 不出合法呼号时无害（超时自动清理）。"""
        raw = (raw or "").strip()
        if not raw or dur is None or dur > 2.5:
            return False
        return 1 <= len(raw) <= 16

    def _spell_flush(self, session):
        """处理该 session 已停顿的拼读缓存：拼接文本 decode 出高分呼号 →
        走 _handle_callsign 统一分支（抄收/修正/排队），无果则丢弃。
        只在重复请求流程中启用（_retry_pending 或额度已消耗）——拼读
        纠正发生在"请重复"之后；正常段直接解出呼号，不走此路径。
        词数 <5 视为半截（"Bravo Golf Nine Bravo"=BG9B 类 1 位后缀/未拼完）
        直接丢弃等友台重新完整拼读，避免半截合法呼号被误抄。"""
        buf = self._spell_buf.get(session)
        if not buf:
            return False
        text = buf["text"].strip()
        in_retry = self._retry_pending or self._retry_left < int(
            nc_cfg("max_retry", default=1))
        if not in_retry or not text or len(_tokenize(text)) < 5:
            self._spell_buf.pop(session, None)
            if in_retry and text and len(_tokenize(text)) >= 1:
                self._failed += 1    # 半截拼读失败：计入未抄收
            return False
        r = decode_callsign(text)
        call, score = r["callsign"], r["score"]
        if not call or score < int(nc_cfg("spell_min_score", default=80)):
            self._spell_buf.pop(session, None)
            self._failed += 1    # 一轮拼读未解出：计入未抄收（与低置信度语义一致）
            return False
        self._spell_buf.pop(session, None)
        logger.info(f"拼读合并解出呼号: {text!r} → {call}（置信度 {score}）")
        self._handle_callsign(call, r["signal"], score, text, "", session)
        return True

    def _spell_feed(self, raw, dur, session, wav):
        """拼读段累积：同 session 短段文本追加（间隔>gap 先 flush 上批）。
        只累积不立即解码——拼读是逐字母进行的，"Bravo Golf Nine Bravo"前
        4 词就合法（BG9B），立即解码会抄半截呼号；统一等友台拼完停顿后
        （主循环空闲轮 _spell_flush_all）再解码整批。返回 True 表示该段
        已作为拼读消费（不刷"请重复"）。"""
        now = time.time()
        gap = float(nc_cfg("spell_gap_seconds", default=4.0))
        buf = self._spell_buf.get(session)
        if buf and now - buf["ts"] > gap:
            self._spell_flush(session)
            buf = None
        if buf:
            buf["text"] = (buf["text"] + " " + (raw or "")).strip()[-80:]
            buf["ts"] = now
        else:
            self._spell_buf[session] = {"text": (raw or "").strip(), "ts": now}
        # 吞段即计入未抄收（与低置信度分支每段 +1 的统计语义一致；
        # 后续 flush 解出呼号会走正式抄收，成功计数以 checked 为准）
        self._failed += 1
        return True

    def _spell_flush_all(self):
        """主循环空闲轮调用：清理超时未处理的拼读缓存（友台拼完即停、
        无后续段触发时靠此收尾）。"""
        gap = float(nc_cfg("spell_gap_seconds", default=4.0))
        now = time.time()
        for session in list(self._spell_buf.keys()):
            if now - self._spell_buf[session]["ts"] > gap:
                self._spell_flush(session)

    def _do_checkin(self, call, signal, wav, raw, session, score, entry=None):
        """抄收一位友台：入册 + 恢复额度 + 建立当前友台上下文 + 播确认 + 实时落盘。
        entry 传入时复用该条目（插队者此前已静默记录，正式轮到时不再重复入册）。"""
        if entry is None:
            entry = [call, signal or "", wav or "", raw or "", ""]
            self._checked_in.append(entry)
        else:
            if signal and not entry[1]:
                entry[1] = signal          # 插队占位时缺信号，正式抄收补记
        self._checked_calls.add(call.upper())
        # 呼号段若已含结构化信息（"我的QTH在…"）一并记录
        fd = dict(extract_report_fields(raw))
        if fd or signal:
            fd.setdefault("signal", signal or "")
            self._fields.setdefault(call.upper(), {}).update(fd)
        self._checkin_times[call.upper()] = datetime.datetime.now().isoformat(timespec="seconds")
        self._retry_pending = False
        self._retry_left = int(nc_cfg("max_retry", default=1))  # 关键：成功抄收后恢复额度，
        # 否则下一个新友台首次未抄收也会被静默（实测 17:26:53 起机器人哑巴的根因）
        # 保存"当前友台"上下文：后续不带呼号的补充段按 session 归入该友台
        self._current_call = call.upper()
        self._current_session = session
        self._current_entry = entry
        self._current_active = True        # 抄收→追问→确认窗口：期间新呼号=插队，不打断
        self._asked_fields.setdefault(call.upper(), set())  # 结构化追问独立计数
        ack = self._fmt(nc_cfg("ack_text", default=
            "{call_phonetic}，这里是{ctrl_call}，抄收你的信号{report}，"
            "请报告您的QTH、使用设备、天线、功率以及抄收主控的信号报告。Over"),
            call=call, call_phonetic=callsign_phonetic(call), report=signal or "")
        logger.info(f"抄收 {call} 信号 {signal or '—'}（置信度 {score}）")
        self._speak(ack)
        self._flush_csv()          # 实时落盘：新友台抄收即写入

    def _replace_checkin(self, call, signal, wav, raw, session, score):
        """同 session 重报不同呼号（同一友台，前一次为 ASR 识别错误/口头重报）：
        用新呼号替换当前友台旧记录，重新抄收。CSV 里只留正确呼号，不排队不打断。"""
        cur = (self._current_call or "").upper()
        logger.info(f"同台友台重报呼号（识别修正）: {cur} → {call.upper()}")
        for i, e in enumerate(self._checked_in):
            if e[0].upper() == cur:
                self._checked_in.pop(i)
                break
        self._checked_calls.discard(cur.upper())
        self._fields.pop(cur.upper(), None)
        self._asked_fields.pop(cur.upper(), None)   # 追问计数随旧记录清除
        self._checkin_times.pop(cur.upper(), None)
        self._current_call = None     # 由 _do_checkin 重建上下文
        self._current_session = None
        self._current_entry = None
        self._do_checkin(call, signal, wav, raw, session, score)

    def _queue_interloper(self, call, signal, raw, session):
        """当前友台进行中收到新呼号（插队）：静默记录到点名 CSV，不播报回应、
        不强调秩序，加入等候队列；当前友台收尾后自动轮到（不丢不打断）。"""
        call = (call or "").upper()
        if not call:
            return
        if call in self._checked_calls \
                or any(w["call"] == call for w in self._waiting):
            logger.info(f"插队呼号 {call} 已在册/等待中，忽略")
            return
        entry = [call, signal or "", "", "", ""]   # 记录到文件（全量落盘随 _flush_csv）
        self._checked_in.append(entry)
        self._waiting.append({"call": call, "signal": signal or "", "session": session})
        logger.info(f"当前友台 {self._current_call} 进行中，{call} 插队已静默记录，等候排队")
        self._flush_csv()

    def _end_current(self, flush=True):
        """当前友台流程收尾：解除进行中状态；若有人在等候排队，自动轮到下一位。
        flush=True 时先补播未确认的信息合并确认（不丢不拖）；友台已主动确认
        （"正确"）则 flush=False，不再重复问"是否正确"。"""
        if flush:
            self._flush_pending_report(force=True)
        self._current_active = False
        if self._waiting:
            self._serve_next_waiting()

    def _serve_next_waiting(self):
        """轮到等候排队的下一位插队者：正式抄收（复用已记录的占位条目，播确认）。"""
        w = self._waiting.pop(0)
        entry = next((e for e in self._checked_in if e[0].upper() == w["call"]), None)
        logger.info(f"轮到排队友台 {w['call']} 正式抄收")
        self._do_checkin(w["call"], w.get("signal", ""), None, "",
                         w.get("session"), 100, entry=entry)

    def _looks_like_report(self, raw):
        """LLM 兜底触发预检：只有文本"看起来像"点名应答（含解释法词/呼号特征/
        信息关键词）才值得调 LLM——过滤 ASR 幻觉垃圾（"不让我就去死"、
        "少回答那这个就到这为止"等），实测垃圾文本也触发 LLM 白等 5s 拖慢点名。"""
        t = (raw or "").upper()
        if not t.strip():
            return False
        if re.search(r"\b(?:ALPHA|BRAVO|CHARLIE|DELTA|ECHO|FOXTROT|GOLF|HOTEL|"
                     r"INDIA|JULIET|KILO|LIMA|MIKE|NOVEMBER|OSCAR|PAPA|QUEBEC|"
                     r"ROMEO|SIERRA|TANGO|UNIFORM|VICTOR|WHISKEY|XRAY|YANKEE|"
                     r"ZULU|NINER)\b", t):
            return True
        if re.search(r"[A-Z]{1,3}\d[A-Z]{0,3}", t):
            return True
        if any(k in t for k in ("呼号", "这里是", "QTH", "设备", "天线",
                                "功率", "信号", "抄收", "主控", "点名")):
            return True
        return False

    def _llm_fix(self, raw, res):
        """低置信度时用 LLM 兜底修复呼号/信号（net_control.llm.enabled=true 时）。
        返回 {"callsign","signal","score"} 或 None（未启用/无合法呼号/调用失败）。
        LLM 失败绝不影响点名：任何异常只记日志，返回 None 走原流程。
        调用放线程并限时 8s（实测 GLM 慢时一次阻塞 19s 拖死点名节奏），
        且每轮点名最多 llm_max_calls（默认 3）次。"""
        if self._llm is None:
            if not nc_cfg("llm", "enabled", default=False) \
                    or not nc_cfg("llm", "api_key", default=""):
                self._llm = False
                return None
            self._llm = LlmClient(
                api_key=nc_cfg("llm", "api_key", default=""),
                model=nc_cfg("llm", "model", default="glm-4.5-flash"),
                base_url=nc_cfg("llm", "base_url",
                                default="https://open.bigmodel.cn/api/paas/v4"),
                timeout=float(nc_cfg("llm", "timeout", default=8)))
        if self._llm is False:
            return None
        max_calls = int(nc_cfg("llm", "max_calls", default=3))
        if self._llm_calls >= max_calls:
            return None
        if self._llm_fails >= 2:
            # 熔断：接口连续超时/异常（实测 GLM 持续 8s 超时），本轮不再调用
            if not getattr(self, "_llm_tripped", False):
                self._llm_tripped = True
                logger.warning("LLM 连续 2 次失败，本轮点名停用 LLM 兜底（防拖慢点名）")
            return None
        try:
            logger.info("LLM 兜底提取（低置信度）…")
            box = {}
            tt = threading.Thread(
                target=lambda: box.__setitem__("d", self._llm.extract(raw)),
                daemon=True)
            tt.start()
            tt.join(timeout=3)
            if tt.is_alive():
                logger.warning("LLM 兜底超时（3s），放弃本次修复")
                self._llm_fails += 1
                return None
            d = box.get("d", {})
            self._llm_calls += 1
        except Exception as e:
            logger.warning(f"LLM 调用失败: {e}")
            self._llm_fails += 1
            return None
        if self._llm_fails > 0:
            self._llm_fails = 0             # 一次成功即清零熔断计数
        call2 = (d.get("callsign") or "").strip().upper().replace(" ", "").replace("-", "")
        if not call2 or not re.fullmatch(r"B[A-Z]\d[A-Z]{1,3}", call2):
            logger.info(f"LLM 未给出合法呼号（{d!r}），维持原流程")
            return None
        sig2 = d.get("signal") or res.get("signal")
        logger.info(f"LLM 修复呼号: {res['callsign'] or '无'} → {call2}"
                    f"{'，信号 ' + str(sig2) if sig2 else ''}")
        return {"callsign": call2, "signal": sig2,
                "score": 95, "reasons": ["LLM 修复"]}

    def _speak_summary(self):
        n = len(self._checked_in)
        calls = "、".join(c for c, *_ in self._checked_in)
        summary_text = nc_cfg("summary_text", default=
            "本次点名共抄收{n}位友台：{calls}。")
        self._speak(self._fmt(summary_text, n=n, calls=calls))
        self._speak(self._fmt(nc_cfg("closing_text", default=
            "CQ CQ CQ，现在是北京时间{time}，{net_name}到此结束，我是本次主控{ctrl_call}，"
            "本次参与台网点名的共有{n}位友台，非常感谢各位友台积极参与，"
            "接下来各位友台可以自由通联，73！"), n=n))
        self.summary = {
            "checked_in": self._checked_in,
            "count": n,
            "duplicates": self._dups,
            "failed": self._failed,
            "started_at": self._started_at.isoformat() if self._started_at else "",
            "ended_at": datetime.datetime.now().isoformat(),
        }
        logger.info(f"点名结束：抄收 {n} 位，重复 {self._dups}，未抄收 {self._failed}")

    # ---------- 基础设施 ----------
    def _ensure_capture(self):
        if self._capture is not None:
            return
        save_dir = None
        if nc_cfg("save_audio", default=True):
            save_dir = nc_cfg("audio_dir", default="/app/net_records")
            try:
                Path(save_dir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"应答录音目录不可写（{e}），本次不落盘录音"
                               f"（容器需 chown 见 start.sh）")
                save_dir = None
        self._capture = VoiceCapture(
            save_dir=save_dir,
            on_segment=lambda pcm16, dur, wav, session: self._seg_queue.put(
                (pcm16, dur, wav, session)))
        if self.link is not None:
            self.link._on_downlink = self._on_downlink      # 注册下行分发（点名期间）
        logger.info(f"接收侧就绪（VAD 阈值 {self._capture.threshold}，"
                    f"静音收尾 {self._capture.silence_end_ms}ms，"
                    f"信令门控 {'开' if self._talking_gate else '关'}）")

    def _on_downlink(self, msg_type, payload):
        """keeper 线程回调：处理语音包（msg_type=1）与讲话信令（msg_type=15）。
        - 自己在播报中（busy 锁被占）：不采集应答（防把自家播报回声当应答），
          但仍做"抢占检测"——检测到非自己 session 的讲话信令/语音包即置位
          _preempted，播报线程据此立即放麦让位（先听后说原则的发射中兜底）
        - 信令门控（use_talking_gate=true，默认）：仅采集服务器广播过
          UserTalking 开始讲话（talking=true）的远端语音——过滤链路底噪/杂音
          误触发的假"应答段"；若 60s 内从未收到任何说话信令（平台不下发），
          自动降级为纯 VAD 采集
        - UserTalking 结束信令（talking=false）→ 立即切段，不等 VAD 静音超时
        - 自己 session 的信令（放麦回显）直接忽略"""
        try:
            own = None
            if self.link is not None and self.link._sess is not None:
                own = self.link._sess.session
            busy = self.link is not None and self.link._busy.is_set()
            if not busy:
                # 自动降级：60s 内从未收到任何开始讲话信令 → 纯 VAD（不丢应答）
                if (self._talking_gate and not self._ever_talk
                        and time.time() - self._gate_started > 60):
                    self._talking_gate = False
                    logger.warning("60s 内未收到任何说话信令（平台可能不下发），"
                                   "降级为纯 VAD 采集")
            if msg_type == 1:
                # 服务器下行按批转发（实测约 600ms 批 5 包×120ms），
                # 必须解析载荷内全部语音包，否则音频会被压缩成倍速
                voices = direct_announce.parse_udp_voice_multi(payload, own_session=own)
                if not voices:
                    return
                if busy:
                    # 自己在发射：任何非自己 session 的语音包 = 他人在讲话 → 抢占
                    if any(v["session"] != own for v in voices):
                        self._preempted.set()
                    return
                for voice in voices:
                    if self._talking_gate and voice["session"] not in self._talking_sessions:
                        continue              # 无"开始讲话"信令的包（底噪等），丢弃
                    if voice["session"] is not None:
                        self._talking_active[voice["session"]] = time.time()
                    if self._decoder is None:
                        self._decoder = direct_announce.OpusDecoder()
                    pcm = self._decoder.decode(voice["opus"])
                    if pcm:
                        self._capture.feed(pcm, voice["session"])
            elif msg_type == 15:
                # UserTalking: f1=session, f2=talking(0/1)
                d = direct_announce.pb_dict(payload)
                sess, talking = d.get(1), d.get(2)
                if sess is not None and sess == own:
                    return                        # 自己（放麦回显）的信令，忽略
                if busy:
                    if talking == 1:
                        self._preempted.set()    # 发射中他人按下 PTT → 抢占让位
                    return
                if talking == 0:
                    if sess is not None:
                        self._talking_sessions.discard(sess)
                        self._talking_active.pop(sess, None)
                    # PTT 抬起：延迟 ptt_release_delay_ms 再收尾（防断续断句），
                    # 延迟窗口内有声音会自动取消
                    self._capture.force_finalize(
                        defer_ms=nc_cfg("ptt_release_delay_ms", default=1000))
                elif talking == 1:
                    self._ever_talk = True
                    if sess is not None:
                        self._talking_sessions.add(sess)
                        self._talking_active[sess] = time.time()
                    self._capture.force_finalize()   # 上一位的段在此收尾（讲话人切换）
        except Exception:
            pass

    def _asr_text(self, pcm16):
        if self._asr is None:
            self._asr = AsrClient(
                api_key=nc_cfg("asr", "api_key", default=""),
                model=nc_cfg("asr", "model", default="qwen3-asr-flash"),
                base_url=nc_cfg("asr", "base_url",
                                default="https://dashscope.aliyuncs.com/compatible-mode/v1"),
                enable_itn=nc_cfg("asr", "enable_itn", default=True),
                language=nc_cfg("asr", "language", default=""))
        # 过短音频补静音到 asr_min_seconds（规避接口对极短/空音频的拒绝）
        min_secs = float(nc_cfg("asr_min_seconds", default=1.0))
        need = int(16000 * 2 * min_secs)
        if len(pcm16) < need:
            pcm16 = pcm16 + b"\x00" * (need - len(pcm16))
        # 裸 PCM → 标准 WAV 字节（服务端按 Data URL mediatype 解析，缺 WAV 头会 400）
        wav_bytes = pcm_to_wav_bytes(pcm16)
        try:
            return self._asr.transcribe(wav_bytes, context_words=self._context_words())
        except Exception as e:
            logger.error(f"ASR 调用失败: {e}")
            return ""

    def _context_words(self):
        words = list(PHONETIC_ITU.keys()) + list(PHONETIC_ZH.keys())
        words += list(self._checked_calls)
        words += [str(x).upper() for x in (nc_cfg("roster", default=[]) or [])]
        words += [str(x) for x in (nc_cfg("extra_vocab", default=[]) or [])]
        return words

    def _someone_speaking(self):
        """信道占用判据（任一命中即视为有人在讲话）：
        ① 信令层：服务器广播过"开始讲话"且尚未收到"结束"的远端 session 集合。
           结束信令（UserTalking talking=false）平台偶发丢失（实测 20:38 后
           session 卡住 → 每次播报前白等 30s），故对每个 talking session 做
           陈旧超时：超过 talking_stale_seconds（默认 8s）无该 session 新语音
           包即视为已讲完并清出，防"信道永久占用"自锁。
        ② VAD 层：正在采集中的语音段（含静音收尾窗口，确保对方真正讲完）。
           静音累计只在有新音频帧喂入时推进——对方讲完、链路静默后不再有帧，
           _speaking 会无限卡 True（实测"没人说话却等 30s"的根因之一）。
           故加 feed 活性检测：最近 vad_idle_seconds（默认 2s）无新帧即复位。"""
        stale = float(nc_cfg("talking_stale_seconds", default=4))
        for s in list(self._talking_sessions):
            last = self._talking_active.get(s, 0)
            if time.time() - last > stale:
                self._talking_sessions.discard(s)
                self._talking_active.pop(s, None)
                logger.info(f"讲话信令陈旧超时（{stale:.0f}s 无语音），"
                            f"释放 session {s} 的信道占用")
        if self._talking_sessions:
            return True
        if self._capture is not None:
            with self._capture._lock:      # 与 keeper 线程 feed 互斥，防读半状态
                if self._capture._speaking:
                    idle = float(nc_cfg("vad_idle_seconds", default=2))
                    last = self._capture.last_feed_at
                    if last is None or time.time() - last > idle:
                        # 正在采集但已 idle 超时：无新帧（对方讲完/链路静默），
                        # 强制复位采集，避免"信道永久占用"
                        if last is not None:
                            logger.info(f"VAD 采集空闲超时（{idle:.0f}s 无新帧），"
                                        f"复位说话状态")
                        self._capture._speaking = False
                        return False
                    return True
        return False

    def _capture_pump(self):
        """时间驱动收尾：链路静默（无新帧）时也推进切段——否则"收到应答段"
        日志滞后、段要等下一个讲话人 PTT 才被顶出（实测根因）。主循环/
        等待循环周期性调用，feed 恢复有帧时互不冲突。"""
        if self._capture is not None:
            try:
                self._capture.pump()
            except Exception:
                pass

    def _wait_channel_idle(self, drain=True, newer_than=None):
        """先听后说：抢麦前等待信道空闲。当前无人讲话 → 立即返回；
        有人在讲 → 等待对方讲完（最多 tx_wait_timeout 秒，超时仍发射，
        避免点名流程被无限拖住）。返回 True 表示可发射。

        newer_than：本句的播报序号。等待期间若已有更新的播报请求生成
        （self._speech_seq > newer_than），说明本句已过时（友台又说了后续
        内容/新友台上麦），放弃本句返回 False——只播最新话术，接话不滞后。

        等待期间**继续识别**：TTS 合成完成、等待发射的窗口里，友台可能接连
        上麦（多人排队），此时不能停泵丢段——循环里同步消费 _seg_queue 并
        _process_segment：识别到谁就点名谁（抄收→成为当前友台→对其播报），
        其余自然排队。识别产生的播报会先完成，再回到本播报。"""
        timeout = float(nc_cfg("tx_wait_timeout", default=30))
        if not self._someone_speaking():
            return True
        logger.info(f"信道占用中，等待对方讲完再播报（最多 {timeout:.0f}s），"
                    f"期间继续识别…")
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            if newer_than is not None and self._speech_seq > newer_than:
                logger.info("已生成更新的点名播报，放弃本句（接话只播最新）")
                return False
            if not self._someone_speaking():
                logger.info("信道已空闲，开始播报")
                return True
            # 等待期间继续识别：消费队列中积压的语音段（多人接连说话不丢）。
            # 发射线程内调用时 drain=False（避免在锁内触发嵌套播报→死锁）
            consumed = self._drain_queue() if drain else 0
            self._capture_pump()           # 时间驱动收尾：等待期间也让段正常切出
            if consumed:
                logger.info(f"等待期间已识别 {consumed} 段（友台轮流点名）")
                if not self._someone_speaking():
                    logger.info("信道已空闲，开始播报")
                    return True
            time.sleep(0.2)
        logger.warning(f"等待信道空闲超时（{timeout:.0f}s），仍尝试播报")
        return True

    def _drain_queue(self, depth=0):
        """消费 _seg_queue 中积压的语音段（等待信道/合成期间继续识别）。
        depth 防递归失控：识别段触发的新播报若再次进入 drain，最多嵌套 2 层。"""
        if depth >= 2:
            return 0
        consumed = 0
        try:
            while True:
                pcm16, dur, wav, session = self._seg_queue.get_nowait()
                self._process_segment(pcm16, dur, wav, session)
                consumed += 1
        except queue.Empty:
            pass
        return consumed

    def _pump_until(self, th, timeout):
        """等待 th 完成，期间周期性 pump（时间驱动切段）并继续识别队列。
        替代纯 join：合成/发射期间主线程阻塞时，链路静默的段也能按时切出
        （否则段要等 join 结束、主循环下次 pump 才切——实测日志滞后的另一来源）。"""
        deadline = time.time() + timeout
        while time.time() < deadline and th.is_alive() and not self._stop.is_set():
            self._capture_pump()
            self._drain_queue(depth=1)
            time.sleep(0.2)
        th.join(timeout=1)

    def _speak(self, text):
        if not text or self._stop.is_set():
            return
        text = text.strip()
        logger.info(f"点名播报: {text}")
        self._speech_seq += 1              # 新话术序号：旧话术等待发射时据此让位
        my_seq = self._speech_seq
        mp3 = ""
        try:
            if self.tts_func:
                mp3 = self.tts_func(text)
            else:
                # TTS 合成放后台线程：合成需要 3~20s（edge-tts 网络），期间
                # 主线程继续识别队列中的语音段，不阻塞点名节奏（实测 10s 合成
                # 期间友台说话被拖住是"识别中断"的根因之一）
                box = {}
                tt = threading.Thread(
                    target=lambda: box.__setitem__("mp3", synth_text(text)),
                    daemon=True)
                tt.start()
                self._drain_queue(depth=1)   # 合成期间继续识别
                self._pump_until(tt, 45)     # 合成期间持续收尾/识别
                mp3 = box.get("mp3", "")
        except Exception as e:
            logger.error(f"点名 TTS 异常: {e}")
        if not mp3:
            logger.error("点名 TTS 无输出，跳过该句播报")
            return
        if self._speech_seq > my_seq:      # 合成期间已有更新话术：跳过构建/发射
            logger.info("合成期间已生成更新的点名播报，跳过本句（旧话术）")
            return
        try:
            packets, n = direct_announce.build_audio(mp3)
        except Exception as e:
            logger.error(f"点名音频构建失败: {e}")
            return
        # ---- 发射放后台线程（play 阻塞 3~15s）----
        # 发射期间主线程继续 _drain_queue：实测"友台说完话日志延迟 6~12s 才
        # 刷出、完全无反应"的根因——旧代码发射（play）串行阻塞主线程，期间
        # 不消费 _seg_queue，友台段切出后要等主控发射完才被处理。
        # 嵌套播报（发射期间识别到的段触发的 _speak）经 _play_lock 排队，
        # 等当前发射完成后再播，不丢句、不死锁（_wait_channel_idle 在发射
        # 线程内 drain=False，避免锁内再触发嵌套播报）。
        play_box = {}

        def _do_play():
            try:
                with self._play_lock:
                    if self._stop.is_set():
                        return
                    # 等锁期间已有更新的播报请求：本句已过时，放弃（只播最新）
                    if self._speech_seq > my_seq:
                        logger.info("已生成更新的点名播报，跳过本句（旧话术）")
                        return
                    s = None
                    if self.link is not None:
                        for _ in range(30):    # 等广播让出 busy（含嵌套排队场景，最多30s）
                            s = self.link.ensure_session()
                            if s is not None or self._stop.is_set():
                                break
                            time.sleep(1.0)
                    if s is None and self.link is not None:
                        logger.warning("链路忙或不可用，跳过本句点名播报")
                        play_box["skipped"] = True
                        return
                    # 先听后说：抢麦前等待信道空闲（当前无人讲话才按下 PTT）
                    if s is not None:
                        if not self._wait_channel_idle(drain=False, newer_than=my_seq):
                            return          # 等信道期间已有更新播报，放弃本句
                    if self._stop.is_set():
                        return
                    try:
                        if s is None:          # 无常驻链路（独立运行场景）：临时短链
                            self._play_shortlink(packets)
                            return
                        # 发射中兜底：检测到他人语音立即放麦让位，不压对方
                        self._preempted.clear()
                        try:
                            with contextlib.redirect_stdout(_StdoutToLogger(logger)):
                                s.take_mic()
                                ok = s.play(packets,
                                            abort_check=lambda: self._preempted.is_set())
                        except Exception as e:
                            # 常驻抢麦失败（6s 无回执）：信道被真实占用但信令/VAD
                            # 未检出（中继转发下行收不到对方语音）、或服务器瞬时
                            # 不给麦（实测 20:30 准点播报失败后现场流程可成功；
                            # 20:07/20:51/22:22 点名播报失败后整句丢失）→ 延迟
                            # 3s 让服务器/信道让位，短链兜底重试，不丢关键播报。
                            if self._speech_seq > my_seq:
                                logger.info("常驻抢麦失败，期间已有更新播报，放弃本句")
                                return
                            logger.warning(f"常驻抢麦失败，3s 后短链兜底重试: {e}")
                            self.link.release()
                            time.sleep(3.0)
                            if self._speech_seq > my_seq:
                                logger.info("常驻抢麦失败，期间已有更新播报，放弃本句")
                                return
                            self._play_shortlink(packets)
                            return
                        play_box["ok"] = ok
                        if not ok:
                            logger.warning("点名播报被他人讲话抢占，"
                                           "已放麦让位（不重播，避免抢台循环）")
                    except Exception as e:
                        logger.error(f"点名播报失败: {e}")
                    finally:
                        if s is not None and self.link is not None:
                            self.link.release()
                        self._last_tx_end = time.time()   # 发射结束（回波过滤窗口起点）
            finally:
                play_box["done"] = True

        pt = threading.Thread(target=_do_play, daemon=True)
        pt.start()
        self._drain_queue(depth=1)   # 发射期间继续识别（嵌套播报经 _play_lock 排队）
        self._pump_until(pt, 60)     # 发射期间持续收尾/识别

    def _play_shortlink(self, packets):
        """临时短链播报（常驻不可用/抢麦失败兜底）：
        suspend 挂起常驻（防同账号互踢）→ 换新连接重试 ≤2 次 → resume。
        短链抢麦可能因连接抖动失败（实测 20:51:36 播报被吞）→ 换新连接重试，
        避免整句丢失。返回 True=成功。"""
        if self.link is not None and hasattr(self.link, "suspend"):
            self.link.suspend()
        last_err = None
        for _try in range(2):
            s2 = direct_announce.DirectAnnouncer(
                username=direct_announce.cfg_get("talk", "username", default=""),
                password=direct_announce.cfg_get("talk", "password", default=""))
            try:
                with contextlib.redirect_stdout(_StdoutToLogger(logger)):
                    s2.connect()
                    s2.take_mic()
                    s2.play(packets)
                if self.link is not None and hasattr(self.link, "resume"):
                    self.link.resume()   # 临时链已断开，恢复常驻保活
                return True
            except Exception as e:
                last_err = e
                try:
                    s2.close()
                except Exception:
                    pass
        if last_err is not None:
            logger.error(f"点名播报短链重试仍失败: {last_err}")
        if self.link is not None and hasattr(self.link, "resume"):
            self.link.resume()   # 临时链已断开，恢复常驻保活
        return False

    # ---------- 点名记录 CSV 实时落盘 ----------
    # 点名一开始就创建文件（写表头），此后每次抄收/补充信息立即全量重写。
    # 这样即使点名会话被中断（kill/容器重启），记录也已持久化——实测 seg 录音
    # 是实时写的所以看得到，而 summary_*.json 只在会话正常结束时才写，
    # 被中断就没有，这是"看不到 summary"的根因。
    CSV_HEADER = ["序号", "呼号", "信号报告", "QTH", "设备", "天线", "功率",
                  "抄收时间", "原始转录", "补充原文"]

    def _csv_path_for(self):
        audio_dir = nc_cfg("audio_dir", default="/app/net_records")
        Path(audio_dir).mkdir(parents=True, exist_ok=True)
        stamp = (self._started_at.strftime("%Y%m%d_%H%M%S")
                 if self._started_at
                 else datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        return str(Path(audio_dir) / f"点名记录_{stamp}.csv")

    def _ensure_csv(self):
        """点名开始时创建 CSV（写表头）。失败不阻断点名。"""
        if self._csv_path:
            return self._csv_path
        try:
            p = self._csv_path_for()
            with open(p, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(self.CSV_HEADER)
            self._csv_path = p
            logger.info(f"点名记录已创建: {p}")
        except Exception as e:
            logger.warning(f"点名记录 CSV 创建失败: {e}")
            self._csv_path = None
        return self._csv_path

    def _csv_rows(self):
        rows = []
        for i, (call, signal, wav, raw, info) in enumerate(self._checked_in, 1):
            fd = self._fields.get(call.upper(), {})
            rows.append([i, call, signal or fd.get("signal", ""),
                         fd.get("qth", ""), fd.get("device", ""),
                         fd.get("antenna", ""), fd.get("power", ""),
                         self._checkin_times.get(call.upper(), ""),
                         raw or "", info or ""])
        return rows

    def _flush_csv(self):
        """全量重写点名记录 CSV（表头+当前全部行）。返回路径或 None。"""
        p = self._ensure_csv()
        if not p:
            return None
        try:
            with open(p, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(self.CSV_HEADER)
                w.writerows(self._csv_rows())
            logger.info(f"点名记录已更新: {p}（{len(self._checked_in)} 位友台）")
            return p
        except Exception as e:
            logger.warning(f"点名记录 CSV 写入失败: {e}")
            return None

    def _export_csv(self, path=None):
        """导出点名记录 CSV（path=None 用实时文件；测试可显式传路径）。
        UTF-8 with BOM（Excel/WPS 直接打开中文不乱码）。返回路径或 None。"""
        if path is None:
            return self._flush_csv()
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(self.CSV_HEADER)
                w.writerows(self._csv_rows())
            logger.info(f"点名记录已导出: {path}（{len(self._checked_in)} 位友台）")
            return path
        except Exception as e:
            logger.warning(f"点名记录导出失败: {e}")
            return None

    def _is_echo(self, raw, dur):
        """中继台回波过滤（简化版）：只判"自己发射后 holdoff 窗口内的短段"。
        回波实测是 0.6s 级的尾音/杂音，**不会有可识别出文字的长回波**
        （用户实测结论）——能识别出文字的段（哪怕 ≤1.5s）一律走正常流程：
        新呼号跳过（_process_segment 已按 call 判断）、主控呼号忽略、空段
        静默忽略，各分支已兜住。
        不再使用文本重合判据：模板化开场"这里是BG9xx"与上段/播报文本连续
        重合 ≥70%，会把第二位友台报名、拼读重拼首段误杀（21:12-21:16 实测）。"""
        holdoff = float(nc_cfg("echo_holdoff_seconds", default=1.5))
        maxseg = float(nc_cfg("echo_segment_max_seconds", default=1.5))
        if self._last_tx_end is not None:
            if (time.time() - self._last_tx_end <= holdoff
                    and dur <= maxseg and not (raw or "").strip()):
                return True
        return False

    def _teardown(self):
        if self.link is not None and self.link._on_downlink is self._on_downlink:
            self.link._on_downlink = None           # 归还下行分发（不打扰播报）
        if self._decoder is not None:
            try:
                self._decoder.close()
            except Exception:
                pass
        audio_dir = nc_cfg("audio_dir", default="/app/net_records")
        try:
            Path(audio_dir).mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            with open(Path(audio_dir) / f"summary_{stamp}.json", "w", encoding="utf-8") as f:
                json.dump(self.summary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"点名摘要落盘失败: {e}")
        self._flush_csv()          # 点名记录 CSV 实时落盘（点名开始即建，此处收尾刷新一次）
        self.done.set()
        if self.on_done:
            try:
                self.on_done(self.summary)
            except Exception:
                pass
        logger.info("点名主播会话结束")


def main():
    ap = argparse.ArgumentParser(description="点名主播离线自测")
    ap.add_argument("--decode", help="离线解码：输入 ASR 转录文本，输出呼号/信号/置信度")
    ap.add_argument("--opus-roundtrip", action="store_true", help="Opus 编解码往返自测")
    ap.add_argument("--asr-test", nargs="?", const="", metavar="WAV",
                    help="ASR 接口自测：传 WAV 文件路径，或留空用 2 秒合成音测试"
                         "（打印完整识别结果/服务端错误正文，用于排查 Key/模型/地域/音频问题）")
    ap.add_argument("--asr-probe", nargs="?", const="", metavar="WAV",
                    help="system 词表格式探测：依次用 5 种请求变体调用 ASR，"
                         "打印各自 HTTP 状态码与错误正文/识别结果。已实测定位："
                         "system.content 必须用数组 [{\"type\":\"text\",\"text\":...}]"
                         "（list）才被接受，字符串（str/bare）一律 400，"
                         "无 system（none）可作基线。建议传真实录音 WAV 以同时"
                         "验证词表对呼号识别的增益")
    args = ap.parse_args()
    if args.decode:
        res = decode_callsign(args.decode)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    if args.opus_roundtrip:
        enc = direct_announce.OpusEncoder()
        dec = direct_announce.OpusDecoder()
        import math
        pcm = bytearray()
        for i in range(960):                        # 1秒 1kHz 正弦 16bit
            v = int(12000 * math.sin(2 * math.pi * 1000 * i / 48000))
            pcm += struct.pack("<h", v)
        opus = enc.encode_frame(bytes(pcm))
        out = dec.decode(opus)
        print(f"编码 {len(pcm)}B → Opus {len(opus)}B → 解码 {len(out)}B")
        print("往返长度一致:", len(out) == len(pcm))
        return
    if args.asr_test is not None:
        if args.asr_test:
            wav_bytes = Path(args.asr_test).read_bytes()      # 已是完整音频文件（WAV）
        else:
            import math
            pcm16 = bytearray()
            for i in range(16000 * 2):              # 2 秒 1kHz 正弦 16bit @16k
                v = int(12000 * math.sin(2 * math.pi * 1000 * i / 16000))
                pcm16 += struct.pack("<h", v)
            # 合成的是裸 PCM，需包 WAV 头（否则服务端按 audio/wav 解析失败报 400）
            wav_bytes = pcm_to_wav_bytes(bytes(pcm16))
        client = AsrClient(
            api_key=nc_cfg("asr", "api_key", default=""),
            model=nc_cfg("asr", "model", default="qwen3-asr-flash"),
            base_url=nc_cfg("asr", "base_url",
                            default="https://dashscope.aliyuncs.com/compatible-mode/v1"),
            enable_itn=nc_cfg("asr", "enable_itn", default=True),
            language=nc_cfg("asr", "language", default=""))
        print(f"ASR 自测: model={client.model} 音频={len(wav_bytes)}B"
              f"（约 {len(wav_bytes) / 32000:.1f}s @16k mono）")
        print(f"base_url={client.base_url}")
        print(f"api_key={'已配置' if client.api_key else '空（配置 net_control.asr.api_key）'}")
        try:
            text = client.transcribe(wav_bytes, context_words=["BRAVO", "BH3XX"])
            print(f"识别结果: {text!r}")
        except Exception as e:
            print(f"ASR 自测失败: {type(e).__name__}: {e}")
            print("若为 HTTP 400/404：检查 api_key、模型名、地域支持（美国地域不支持"
                  "OpenAI 兼容模式）；若是音频类报错请换用真实录音文件重试，"
                  "例如：python3 net_control.py --asr-test /app/net_records/seg_xxx.wav")
        return
    if args.asr_probe is not None:
        if args.asr_probe:
            wav_bytes = Path(args.asr_probe).read_bytes()
        else:
            import math
            pcm16 = bytearray()
            for i in range(16000 * 2):              # 2 秒 1kHz 正弦 16bit @16k
                v = int(12000 * math.sin(2 * math.pi * 1000 * i / 16000))
                pcm16 += struct.pack("<h", v)
            wav_bytes = pcm_to_wav_bytes(bytes(pcm16))
        client = AsrClient(
            api_key=nc_cfg("asr", "api_key", default=""),
            model=nc_cfg("asr", "model", default="qwen3-asr-flash"),
            base_url=nc_cfg("asr", "base_url",
                            default="https://dashscope.aliyuncs.com/compatible-mode/v1"),
            enable_itn=nc_cfg("asr", "enable_itn", default=True),
            language=nc_cfg("asr", "language", default=""))
        print(f"system 词表格式探测: model={client.model} 音频={len(wav_bytes)}B")
        print(f"base_url={client.base_url}  api_key={'已配置' if client.api_key else '空'}\n")
        print("已实测结论（2026-09-18）：list 数组格式成功，str/bare 字符串格式 400。\n")
        variants = [
            ("V1 list+opts   ", "list", True),
            ("V2 str+opts    ", "str", True),
            ("V3 bare+opts   ", "bare", True),
            ("V4 none+opts   ", "none", True),
            ("V5 str+no-opts ", "str", False),
        ]
        vocab = ["BRAVO", "HOTEL", "BH3XX", "BG9ABC", "泉盛", "咸阳市"]
        for label, style, with_opts in variants:
            body = client._build_body(wav_bytes, context_words=vocab,
                                      with_asr_opts=with_opts, sys_style=style)
            status, data = client._request(body)
            detail = data.decode("utf-8", "replace")
            if status == 200:
                try:
                    out = json.loads(detail)
                    content = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    print(f"{label} → HTTP 200  识别: {content!r}")
                except Exception as e:
                    print(f"{label} → HTTP 200  解析失败: {e}")
            else:
                print(f"{label} → HTTP {status}  {detail[:240]}")
        print("\n预期：V1/V4 成功（list=生产默认；none=降级兜底），V2/V3/V5 400。"
              "若 V1 对真实录音识别出呼号即证明词表生效（建议："
              "python3 net_control.py --asr-probe /app/net_records/seg_xxx.wav）。")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
