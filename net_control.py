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
import datetime
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


# ====================== WAV 落盘 ======================
def write_wav(path, pcm16, rate=16000):
    with open(path, "wb") as f:
        f.write(struct.pack("<4sI4s4sIHHIIHH", b"RIFF", 36 + len(pcm16), b"WAVE",
                            b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(struct.pack("<4sI", b"data", len(pcm16)))
        f.write(pcm16)


# ====================== 话音检测（VAD）与切段 ======================
class VoiceCapture:
    """话音检测与语音段切分：喂入 48kHz 单声道 PCM（下行 Opus 解码后），
    按 20ms 帧算能量，检测"起讲→讲话→静音收尾"，输出 16kHz 单声道完整段。
    带自适应噪声底；on_segment(pcm16, dur_s, wav_path) 在喂入线程中回调。"""
    FRAME_S = 960 * 2                                # 20ms @48k int16 字节数

    def __init__(self, threshold=None, silence_end_ms=None, min_segment_ms=None,
                 max_segment_ms=None, out_rate=16000, save_dir=None, on_segment=None):
        self.threshold = threshold if threshold is not None else nc_cfg("vad_threshold", default=800)
        self.silence_end_ms = silence_end_ms if silence_end_ms is not None else nc_cfg("silence_end_ms", default=600)
        self.min_segment_ms = min_segment_ms if min_segment_ms is not None else nc_cfg("min_segment_ms", default=400)
        self.max_segment_ms = max_segment_ms if max_segment_ms is not None else nc_cfg("max_segment_ms", default=15000)
        self.out_rate = out_rate
        self.save_dir = Path(save_dir) if save_dir else None
        self.on_segment = on_segment
        self._speaking = False
        self._buf48 = bytearray()
        self._silence_s = 0.0
        self._noise = 0.0
        self._seq = 0

    def _rms(self, frame):
        s = array.array('h')
        s.frombytes(frame)
        if not s:
            return 0.0
        return (sum(x * x for x in s) / len(s)) ** 0.5

    def _active_thr(self):
        return max(float(self.threshold), self._noise * 3.0, 300.0)

    def feed(self, pcm48):
        if not pcm48:
            return
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
            else:
                self._buf48 += frame
                if active:
                    self._silence_s = 0.0
                else:
                    self._silence_s += 0.02
                dur = len(self._buf48) / (48000 * 2)
                if (self._silence_s * 1000 >= self.silence_end_ms
                        or dur >= self.max_segment_ms / 1000):
                    self._finalize()

    def _finalize(self):
        pcm48 = bytes(self._buf48)
        self._speaking = False
        self._buf48 = bytearray()
        self._silence_s = 0.0
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
                self.on_segment(pcm16, dur, wav_path)
            except Exception as e:
                logger.warning(f"应答段回调异常: {e}")

    def force_finalize(self):
        """外部（如 UserTalking 结束信令）要求立即结束当前语音段。
        优于纯 VAD 静音等待：对方话音一停（松 PTT）即切段，不吞尾字。"""
        if self._speaking:
            self._finalize()


# ====================== ASR 客户端（qwen3-asr-flash，OpenAI 兼容） ======================
class AsrClient:
    """非流式转写：OpenAI 兼容 /chat/completions，System Message 传实体词表
    （点名名单、解释法词表、已抄收呼号）提升呼号识别。纯 urllib，无新增依赖。"""

    def __init__(self, api_key="", model="qwen3-asr-flash",
                 base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                 enable_itn=True, language="", timeout=30):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.enable_itn = enable_itn
        self.language = language
        self.timeout = timeout

    def _build_body(self, wav_bytes, context_words=None):
        uri = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii")
        messages = []
        if context_words:
            ctx = ("业余无线电点名应答转写。以下为背景实体词表，请优先正确识别："
                   + "、".join(context_words))
            messages.append({"role": "system", "content": ctx})
        messages.append({"role": "user",
                         "content": [{"type": "input_audio",
                                      "input_audio": {"data": uri}}]})
        body = {"model": self.model, "messages": messages, "stream": False}
        asr_opts = {"enable_itn": bool(self.enable_itn)}
        if self.language:
            asr_opts["language"] = self.language
        body["asr_options"] = asr_opts
        return body

    def transcribe(self, wav_bytes, context_words=None):
        body = self._build_body(wav_bytes, context_words)
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return (out.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


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
def synth_text(text, voice=None, cache_dir=None, timeout_inner=28, timeout_join=30,
               max_retries=2, retry_delay=5.0):
    """Edge-TTS 合成（懒加载），返回 mp3 路径；失败返回空串。
    与 announce.py 同款"缓存 + libmpg123 dry-run 校验"策略，键为全文 md5。"""
    import hashlib
    import asyncio
    if voice is None:
        voice = direct_announce.cfg_get("tts", "voice", default="zh-CN-XiaoxiaoNeural")
    if cache_dir is None:
        cache_dir = nc_cfg("tts_cache_dir", default=direct_announce.cfg_get(
            "paths", "cache_dir", default="/app/tts_cache"))
    cache_path = Path(cache_dir) / f"nc_{hashlib.md5(text.encode('utf-8')).hexdigest()}.mp3"
    if cache_path.exists() and direct_announce.dry_validate_mp3(cache_path):
        return str(cache_path)
    import edge_tts
    for attempt in range(1, max_retries + 1):
        err = []
        def _syn():
            async def _run():
                await edge_tts.Communicate(text, voice).save(str(cache_path))
            try:
                asyncio.run(asyncio.wait_for(_run(), timeout=timeout_inner))
            except Exception as e:
                err.append(f"{type(e).__name__}: {e}")
        t = threading.Thread(target=_syn, daemon=True)
        t.start()
        t.join(timeout=timeout_join)
        if not t.is_alive() and cache_path.exists() and direct_announce.dry_validate_mp3(cache_path):
            return str(cache_path)
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
        self._checked_in = []                   # [(call, signal, wav, raw)]
        self._checked_calls = set()
        self._dups = 0
        self._failed = 0
        self._retry_pending = False
        self._retry_left = int(nc_cfg("max_retry", default=1))
        self._asr = None
        self._llm = None
        self._decoder = None
        self._capture = None
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
            self._speak(nc_cfg("opening_text", default=
                "CQ CQ CQ，这里是{repeater_call}业余无线电中继台，现在是每周{weekday}晚"
                "{net_name}，我是今晚主控{ctrl_call}，今天是{date}，现在是北京时间{time}，"
                "我的QTH位于{main_qth}，所用设备{main_device}，{main_antenna}，"
                "{main_power}功率发射，现在开始台网点名，请抄收到信号的友台依次上台报告"
                "你的呼号、QTH、使用设备、天线、功率以及抄收到主控台的信号报告，"
                "这里是{ctrl_phonetic} {ctrl_call}，Over"))
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
        window = float(nc_cfg("listen_after_open_seconds", default=60))
        max_count = int(nc_cfg("max_checked_in", default=200))
        deadline = time.time() + window
        logger.info(f"点名开放收听窗口 {window:.0f} 秒")
        while (time.time() < deadline and len(self._checked_in) < max_count
               and not self._stop.is_set()):
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                pcm16, dur, wav = self._seg_queue.get(timeout=min(remain, 1.0))
            except queue.Empty:
                continue
            self._process_segment(pcm16, dur, wav)

    def _run_roster(self):
        roster = [str(x).upper() for x in (nc_cfg("roster", default=[]) or [])]
        logger.info(f"固定名单点名，共 {len(roster)} 位")
        for call in roster:
            if self._stop.is_set():
                break
            self._speak(self._fmt(nc_cfg("roster_call_text", default="请{call_phonetic}回答。Over"),
                                  call=call, call_phonetic=callsign_phonetic(call)))
            try:
                pcm16, dur, wav = self._seg_queue.get(
                    timeout=float(nc_cfg("roster_call_timeout", default=12)))
            except queue.Empty:
                self._speak(self._fmt(nc_cfg("no_reply_text",
                                             default="无人应答，继续下一位。")))
                continue
            self._process_segment(pcm16, dur, wav)
        logger.info("固定名单点名结束")

    # ---------- 应答处理 ----------
    def _process_segment(self, pcm16, dur, wav):
        logger.info(f"收到应答段 {dur:.1f}s（{Path(wav).name if wav else '未落盘'}）")
        raw = self._asr_text(pcm16)
        logger.info(f"ASR: {raw}")
        res = decode_callsign(raw, regex=nc_cfg("callsign_regex", default=""))
        call, signal, score = res["callsign"], res["signal"], res["score"]
        if call and is_duplicate(call, self._checked_calls):
            logger.info(f"重复抄收 {call}，跳过")
            self._dups += 1
            self._retry_pending = False
            self._speak(self._fmt(nc_cfg("dup_text", default="{call_phonetic} 已经抄收过，"
                                                             "请下一位友台。"),
                                  call=call, call_phonetic=callsign_phonetic(call)))
            return
        if call and score >= int(nc_cfg("confidence_threshold", default=60)):
            self._checked_in.append((call, signal or "", wav or "", raw or ""))
            self._checked_calls.add(call.upper())
            self._retry_pending = False
            ack = self._fmt(nc_cfg("ack_text", default=
                "{call_phonetic}，这里是{ctrl_call}，抄收你的信号{report}，"
                "请报告您的QTH、使用设备、天线、功率以及抄收主控的信号报告。Over"),
                call=call, call_phonetic=callsign_phonetic(call), report=signal or "")
            logger.info(f"抄收 {call} 信号 {signal or '—'}（置信度 {score}）")
            self._speak(ack)
            return
        # 低置信度：请求重复（限次）
        if self._retry_pending or self._retry_left <= 0:
            logger.warning(f"未抄收（{'/'.join(res['reasons'])}）文本: {raw}")
            self._failed += 1
            self._retry_pending = False
            return
        self._retry_pending = True
        self._retry_left -= 1
        target = call or ""
        logger.info(f"置信度 {score}，请求重复呼号")
        self._speak(self._fmt(nc_cfg("repeat_text", default=
            "{call_phonetic}，请重复一遍您的呼号。"), call=target,
            call_phonetic=callsign_phonetic(target) or "上一位友台"))

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
        save_dir = nc_cfg("audio_dir", default="/app/net_records") if nc_cfg("save_audio", default=True) else None
        self._capture = VoiceCapture(
            save_dir=save_dir,
            on_segment=lambda pcm16, dur, wav: self._seg_queue.put((pcm16, dur, wav)))
        if self.link is not None:
            self.link._on_downlink = self._on_downlink      # 注册下行分发（点名期间）
        logger.info(f"接收侧就绪（VAD 阈值 {self._capture.threshold}，"
                    f"静音收尾 {self._capture.silence_end_ms}ms）")

    def _on_downlink(self, msg_type, payload):
        """keeper 线程回调：处理语音包（msg_type=1）与讲话信令（msg_type=15）。
        - 自己发射中（busy 锁被占）不接收：防把自家播报/点名回声当应答
          （下行包是否带 session 由服务器决定，此守卫不依赖 session 字段，双保险）
        - UserTalking 结束信令（talking=false）→ 立即切段，不等 VAD 静音超时"""
        try:
            if self.link is not None and self.link._busy.is_set():
                return
            if msg_type == 1:
                voice = direct_announce.parse_udp_voice(payload, own_session=None)
                if voice is None:
                    return
                if self._decoder is None:
                    self._decoder = direct_announce.OpusDecoder()
                pcm = self._decoder.decode(voice["opus"])
                if pcm:
                    self._capture.feed(pcm)
            elif msg_type == 15:
                # UserTalking: f1=session, f2=talking(0/1)。talking=0 即对方松 PTT
                if direct_announce.pb_dict(payload).get(2) == 0:
                    self._capture.force_finalize()
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
        try:
            return self._asr.transcribe(pcm16, context_words=self._context_words())
        except Exception as e:
            logger.error(f"ASR 调用失败: {e}")
            return ""

    def _context_words(self):
        words = list(PHONETIC_ITU.keys()) + list(PHONETIC_ZH.keys())
        words += list(self._checked_calls)
        words += [str(x).upper() for x in (nc_cfg("roster", default=[]) or [])]
        words += [str(x) for x in (nc_cfg("extra_vocab", default=[]) or [])]
        return words

    def _speak(self, text):
        if not text or self._stop.is_set():
            return
        text = text.strip()
        logger.info(f"点名播报: {text}")
        mp3 = ""
        try:
            mp3 = self.tts_func(text) if self.tts_func else synth_text(text)
        except Exception as e:
            logger.error(f"点名 TTS 异常: {e}")
        if not mp3:
            logger.error("点名 TTS 无输出，跳过该句播报")
            return
        try:
            packets, n = direct_announce.build_audio(mp3)
        except Exception as e:
            logger.error(f"点名音频构建失败: {e}")
            return
        s = None
        if self.link is not None:
            for _ in range(10):                     # 等广播让出 busy（互斥等待，最多10s）
                s = self.link.ensure_session()
                if s is not None or self._stop.is_set():
                    break
                time.sleep(1.0)
        if s is None and self.link is not None:
            logger.warning("链路忙或不可用，跳过本句点名播报")
            return
        try:
            if s is None:                           # 无常驻链路（独立运行场景）：临时短链
                s2 = direct_announce.DirectAnnouncer(
                    username=direct_announce.cfg_get("talk", "username", default=""),
                    password=direct_announce.cfg_get("talk", "password", default=""))
                try:
                    with contextlib.redirect_stdout(_StdoutToLogger(logger)):
                        s2.connect()
                        s2.take_mic()
                        s2.play(packets)
                finally:
                    s2.close()
                return
            with contextlib.redirect_stdout(_StdoutToLogger(logger)):
                s.take_mic()
                s.play(packets)
        except Exception as e:
            logger.error(f"点名播报失败: {e}")
        finally:
            if s is not None and self.link is not None:
                self.link.release()

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
    ap.print_help()


if __name__ == "__main__":
    main()
