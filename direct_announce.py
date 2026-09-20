#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滔滔链路直连播报客户端 —— direct_announce.py
管线: mp3 →(libmpg123)→ PCM 48k 单声道 →(libopus 12kbps CBR)→ 30字节/帧
      → 6帧/包(185B) → 匀速120ms/包 → UDPTunnel(ID=1)，抢麦/放麦全自动。
v12（配置独立版）: 所有可变参数迁移至 config.json（支持环境变量
HAM_BOT_CONFIG 指定路径，默认读取脚本同目录 config.json）。代码保持与
生产/开源一致，仅配置文件不同即可切换部署环境。函数 dry_validate_mp3
（libmpg123 完整解码 dry-run 校验）与 trim_silence（头尾静音裁剪）同步
保留在生产基准上。
v10: 预建链只做 connect+login（不提前抢麦——长时间"说话中"静默
会干扰频道）；抢麦由调用方控制时机（announce.py 在准点前 0.5s 发起），
LEAD_DELAY 移入 play() 统一保证 UserTalking→首包 0.5s 官方时序。
v9: DirectAnnouncer 类支持分步（connect/take_mic/play），配合
announce.py 预热阶段预建链+预构建音频包，使首包恰在准点整发出；凭据改为
构造函数参数，不再依赖模块级硬编码。v7: UserTalking 与首包之间加 0.5s 间隔
（官方 App 实测时序）——中继台靠该信令建链，首包紧跟会吞掉开头字。
v6: 发包节奏改匀速 120ms/包。此前照抄下行抓包的"600ms批5包"上送，
生产实测手机/中继台都卡顿丢字且卡顿点各自不同——突发让接收端浅 jitter buffer
溢出/下溢（实时对讲追求低延迟 buffer 很浅，不同设备处理策略不同→卡顿点不同）。
官方手机上行本是实时编码匀速节奏；600ms 批是下行/无线链路的聚合现象，不能反推
上行。另加 TCP_NODELAY 禁 Nagle（小包攒发会叠加节奏抖动）。
v3 定案包格式: [0x20][varint seq][varint len=180] + 180字节Opus = 185字节
（v1 len 编码 0x81B4→436 倍速；v2 误加上行 session→纯噪声。详见 HANDOFF.md）。
announce_once() 供 announce.py 调用。
依赖（仅系统库，无需 pip）:
    apt-get install -y libopus0 libmpg123-0
用法:
    # 本地管线自测（不联网）: 解码→编码→统计
    python3 direct_announce.py --mp3 xx.mp3 --dry-run
    # 完整播报（默认 totalkd.allptt.com:59638 TLS，参数取自 config.json）
    python3 direct_announce.py --mp3 xx.mp3
"""
import argparse
import array
import ctypes
import ctypes.util
import json
import os
import socket
import ssl
import struct
import threading
import time
# ====================== 配置加载 ======================
# 配置来源优先级：环境变量 HAM_BOT_CONFIG 指定路径 > 脚本同目录 config.json
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
def load_config(path=None):
    """加载 JSON 配置文件。找不到或不合法时返回空 dict（代码内置默认值兜底）。
    返回 (配置dict, 实际使用的配置路径)。"""
    p = path or os.environ.get("HAM_BOT_CONFIG") or DEFAULT_CONFIG
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, p
        return data, p
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}, p
CFG, CFG_PATH = load_config()
def cfg_get(*path, default=None):
    """从配置 dict 按路径取值，缺失返回 default。"""
    node = CFG
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
_talk = cfg_get("talk", default={}) or {}
_client = _talk.get("client_profile", {}) or {}
HOST = _talk.get("host", "totalkd.allptt.com")
PORT = _talk.get("port", 59638)
USE_TLS = _talk.get("use_tls", True)
USERNAME = _talk.get("username", "")
PASSWORD = _talk.get("password", "")
RELEASE = _client.get("release", "V2.8.5")
OS_NAME = _client.get("os_name", "Android")
OS_VERSION = _client.get("os_version", "16")
MODEL = _client.get("model", "PKT110")
# ---------------- protobuf 编解码（与 validate_client.py 同源已验证） ----------------
def varint_enc(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)
def varint_dec(data, i):
    result, shift = 0, 0
    while i < len(data):
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
    raise ValueError("varint 越界")
def enc_uint(field, value):
    return varint_enc(field << 3) + varint_enc(value)
def enc_str(field, value):
    raw = value.encode("utf-8")
    return varint_enc((field << 3) | 2) + varint_enc(len(raw)) + raw
def build_version():
    """Version 上报：release/os 是服务器侧"登录类型"显示的数据源（UserState
    f22/f23/f24 广播给频道成员）。v12 前沿用网页客户端 "web" 被显示为"浏览器
    登陆"；v13 对齐频道内真实手机 App 画像（peek_release.py 实测 BI9BZW）。
    version 号沿用 1.2.4 编码（服务器已验证接受）。"""
    return (enc_uint(1, (1 << 16) | (2 << 8) | 4) +
            enc_str(2, RELEASE) + enc_str(3, OS_NAME) + enc_str(4, OS_VERSION))
def build_login(username, password, ent_id=None, model=None):
    # ent_id 省略时编码整体不含该字段（与网页客户端一致；显式=0 会报 Ent not exist）
    p = enc_str(1, username) + enc_str(2, password)
    if ent_id is not None:
        p += enc_uint(4, ent_id)
    if model:
        p += enc_str(6, model)   # 设备型号（UserState f25 广播；该组织未开设备白名单）
    return p
def build_apply_mic(apply):
    return varint_enc(2 << 3) + varint_enc(1 if apply else 0)
def build_user_talking(session, talking):
    """UserTalking 上报（ID=15）。官方客户端抢麦成功后立即上报 talking=true，
    松 PTT 上报 talking=false；服务器广播给频道成员（UI 显示说话人）并
    触发录音/中继台链路。实测字段: f1=自己session, f2=talking, f9=voice_cast=0。
    （2026-08-29 listen_capture --all 抓包确认，缺失此上报=不显示说话人+中继台不转发）"""
    return enc_uint(1, session) + enc_uint(2, 1 if talking else 0) + enc_uint(9, 0)
def decode_pb(data, limit=30):
    out, i = [], 0
    try:
        while i < len(data) and len(out) < limit:
            tag, i = varint_dec(data, i)
            field, wire = tag >> 3, tag & 7
            if wire == 0:
                val, i = varint_dec(data, i)
                out.append((field, val))
            elif wire == 2:
                ln, i = varint_dec(data, i)
                raw = data[i:i + ln]
                i += ln
                try:
                    out.append((field, "'%s'" % raw.decode("utf-8")))
                except UnicodeDecodeError:
                    out.append((field, raw[:16].hex() + ".."))
            elif wire == 5:
                out.append((field, "f32:" + data[i:i + 4].hex())); i += 4
            elif wire == 1:
                out.append((field, "f64:" + data[i:i + 8].hex())); i += 8
            else:
                break
    except Exception:
        pass
    return out
def pb_dict(payload):
    d = {}
    for f, v in decode_pb(payload, limit=64):
        if f not in d:
            d[f] = v
    return d
def frame(msg_type, payload):
    return struct.pack(">HI", msg_type, len(payload)) + payload
# ---------------- libopus 编码器（ctypes 直调，已验证） ----------------
def load_lib(names):
    name = ctypes.util.find_library(names[0])
    for cand in ([name] if name else []) + names:
        try:
            return ctypes.cdll.LoadLibrary(cand)
        except OSError:
            continue
    return None
class OpusEncoder:
    OPUS_APPLICATION_VOIP = 2048
    OPUS_SET_BITRATE = 4002
    OPUS_SET_VBR = 4006
    def __init__(self, rate=48000, channels=1, bitrate=None):
        lib = load_lib(["opus", "libopus.so.0", "libopus.so"])
        if lib is None:
            raise RuntimeError("未找到 libopus，请先执行: apt-get install -y libopus0")
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16),
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte),
                                    ctypes.c_int]
        self.lib = lib
        err = ctypes.c_int(0)
        raw = lib.opus_encoder_create(rate, channels,
                                      self.OPUS_APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not raw:
            raise RuntimeError("opus_encoder_create 失败: err=%s" % err.value)
        self.state = ctypes.c_void_p(raw)
        if bitrate is None:
            bitrate = cfg_get("audio", "opus_bitrate", default=12000)
        lib.opus_encoder_ctl(self.state, self.OPUS_SET_BITRATE, bitrate)  # 12kbps
        lib.opus_encoder_ctl(self.state, self.OPUS_SET_VBR, 0)           # CBR
    def encode_frame(self, pcm_bytes):
        """960样本 int16 LE (1920字节) → Opus 帧（CBR 12kbps 下应恰为 30 字节）"""
        n_in = len(pcm_bytes) // 2
        pcm = (ctypes.c_int16 * n_in).from_buffer_copy(pcm_bytes)
        out = (ctypes.c_ubyte * 4000)()
        n = self.lib.opus_encode(self.state, pcm, n_in, out, 4000)
        if n < 0:
            raise RuntimeError("opus_encode 错误码 %d" % n)
        return ctypes.string_at(ctypes.addressof(out), n)
class OpusDecoder:
    """Opus 解码器（ctypes 直调 libopus0）：链路下行语音包 → PCM s16le 单声道 48kHz。
    点名主播接收侧用，与 OpusEncoder 镜像。libopus0 已在镜像内，无新增依赖。
    线程安全：decode/close 互斥（点名会话 teardown 时 keeper 线程可能正在
    decode，直接 destroy 会 use-after-free 触发 libopus 内部断言崩溃——
    日志特征: "assertion failed: st->channels == 1 || st->channels == 2"）。"""
    def __init__(self, rate=48000, channels=1):
        lib = load_lib(["opus", "libopus.so.0", "libopus.so"])
        if lib is None:
            raise RuntimeError("未找到 libopus，请先执行: apt-get install -y libopus0")
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte),
                                    ctypes.c_int32, ctypes.POINTER(ctypes.c_int16),
                                    ctypes.c_int32, ctypes.c_int]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib = lib
        self._lock = threading.Lock()
        err = ctypes.c_int(0)
        raw = lib.opus_decoder_create(rate, channels, ctypes.byref(err))
        if err.value != 0 or not raw:
            raise RuntimeError("opus_decoder_create 失败: err=%s" % err.value)
        self.state = ctypes.c_void_p(raw)
    def decode(self, opus_bytes):
        """Opus 包 → PCM s16le 单声道字节流（48kHz）。

        平台关键事实：下行 180B 语音包是发送端**逐帧编码后串联**的
        6 个独立 20ms Opus 帧（build_audio：每帧 30B CBR，6 帧拼一包），
        并非 RFC 多帧包。若把 180B 整体喂给 opus_decode，只会解出首帧
        （960 样本=20ms），其余 150B 被忽略 → 录音变成约 6 倍速+跳变
        （capture_downlink.py 实测：整包解码全部只出 960 样本）。
        修复：按 30B 帧切分、逐帧解码、拼接输出。尾帧不足 30B 补零
        （与发送端尾包补零一致）；无效帧输出静音帧，时长不塌陷。"""
        with self._lock:
            if self.state is None or self.lib is None:
                return b""
            if not opus_bytes:
                return b""
            frame_size = 30
            data = opus_bytes
            tail = len(data) % frame_size
            if tail:
                data = data + b"\x00" * (frame_size - tail)
            out = bytearray()
            for off in range(0, len(data), frame_size):
                frame = data[off:off + frame_size]
                buf_in = (ctypes.c_ubyte * frame_size).from_buffer_copy(frame)
                buf_out = (ctypes.c_int16 * 5760)()
                n = self.lib.opus_decode(self.state, buf_in, frame_size,
                                         buf_out, 5760, 0)
                if n < 0:
                    n = 960               # 无效/补零帧 → 一帧静音，时长不塌陷
                out += ctypes.string_at(ctypes.addressof(buf_out), n * 2)
            return bytes(out)
    def close(self):
        with self._lock:
            try:
                if self.state is not None:
                    self.lib.opus_decoder_destroy(self.state)
            except Exception:
                pass
            self.state = None
# ---------------- libmpg123 解码器（ctypes 直调） ----------------
class Mpg123Decoder:
    """mp3 → PCM s16le 单声道 48kHz。libmpg123 内部完成下混+重采样。"""
    ENC_SIGNED_16 = 0xD0    # MPG123_ENC_16|MPG123_ENC_SIGNED|0x10（fmt123.h）
    MONO = 1                # MPG123_MONO
    OK, DONE, ERR = 0, -12, -1
    NEW_FORMAT = -11        # "下次调用格式变化"提示，非错误（首次read必触发）
    def __init__(self):
        lib = load_lib(["mpg123", "libmpg123.so.0", "libmpg123.so"])
        if lib is None:
            raise RuntimeError("未找到 libmpg123，请先执行: apt-get install -y libmpg123-0")
        lib.mpg123_init.restype = ctypes.c_int
        lib.mpg123_new.restype = ctypes.c_void_p
        lib.mpg123_new.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        lib.mpg123_open.restype = ctypes.c_int
        lib.mpg123_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.mpg123_format.restype = ctypes.c_int
        lib.mpg123_format.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_int, ctypes.c_int]
        lib.mpg123_read.restype = ctypes.c_int
        lib.mpg123_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_size_t)]
        lib.mpg123_delete.argtypes = [ctypes.c_void_p]
        lib.mpg123_strerror.restype = ctypes.c_char_p
        lib.mpg123_strerror.argtypes = [ctypes.c_void_p]
        lib.mpg123_getformat.restype = ctypes.c_int
        lib.mpg123_getformat.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long),
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        self.lib = lib
        lib.mpg123_init()
    def decode(self, path, want_rate=48000):
        """返回 (PCM s16le 单声道字节流, 源采样率)，已重采样到 want_rate。
        注意: libmpg123 只支持降采样不做上采样——强制 format(48000) 会把 24kHz
        样本按 48kHz 标签原样输出 → 时长减半+音调翻倍（v3 倍速 bug 根因）。
        故按 native 格式解码（默认 s16），读出真实源采样率后自行重采样。"""
        err = ctypes.c_int(0)
        mh = self.lib.mpg123_new(None, ctypes.byref(err))
        if not mh:
            raise RuntimeError("mpg123_new 失败: err=%d" % err.value)
        try:
            if self.lib.mpg123_open(mh, str(path).encode()) != self.OK:
                raise RuntimeError("mpg123_open 失败: %s" %
                                   self.lib.mpg123_strerror(mh).decode("utf-8", "replace"))
            chunks, buf, done = [], (ctypes.c_ubyte * 16384)(), ctypes.c_size_t(0)
            while True:
                r = self.lib.mpg123_read(mh, buf, len(buf), ctypes.byref(done))
                if done.value:
                    chunks.append(ctypes.string_at(buf, done.value))
                if r == self.DONE:       # -12 正常读完
                    break
                if r == self.ERR:        # -1 真错误
                    raise RuntimeError("mpg123_read 失败: %s" %
                                       self.lib.mpg123_strerror(mh).decode("utf-8", "replace"))
                # r==OK(0) 或 NEW_FORMAT(-11): 继续读
            rate, ch, enc = ctypes.c_long(0), ctypes.c_int(0), ctypes.c_int(0)
            self.lib.mpg123_getformat(mh, ctypes.byref(rate), ctypes.byref(ch), ctypes.byref(enc))
            if enc.value != self.ENC_SIGNED_16:
                raise RuntimeError("mpg123 非预期输出编码 %d（期望 s16）" % enc.value)
            pcm = b"".join(chunks)
            if ch.value == 2:            # 立体声源下混单声道
                s = array.array('h'); s.frombytes(pcm)
                pcm = array.array('h', ((s[i] + s[i + 1]) // 2
                                        for i in range(0, len(s) - 1, 2))).tobytes()
            return resample_linear(pcm, rate.value, want_rate), rate.value
        finally:
            self.lib.mpg123_delete(mh)
def dry_validate_mp3(path, min_seconds=0.15):
    """libmpg123 完整解码 dry-run 校验：识别头部损坏 + 尾部截断不完整。
    与播报共用同一个解码器，校验通过=播报必然能解码。"""
    try:
        pcm, _ = Mpg123Decoder().decode(str(path))
        return len(pcm) >= 48000 * 2 * min_seconds   # 至少 min_seconds 秒有效音频
    except Exception:
        return False
def resample_linear(pcm, src_rate, dst_rate=48000):
    """s16le 单声道 PCM 线性插值重采样（语音足够；24k→48k 频谱无损感）"""
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = array.array('h')
    samples.frombytes(pcm)
    n_src = len(samples)
    n_dst = int(round(n_src * dst_rate / src_rate))
    if n_dst < 1:
        return b""
    out = array.array('h')
    step = (n_src - 1) / (n_dst - 1) if n_dst > 1 else 0.0
    pos = 0.0
    for _ in range(n_dst):
        i0 = int(pos)
        frac = pos - i0
        i1 = i0 + 1 if i0 + 1 < n_src else i0
        out.append(int(samples[i0] * (1.0 - frac) + samples[i1] * frac))
        pos += step
    return out.tobytes()
# ---------------- 音频管线 ----------------
FRAME_BYTES = 960 * 2          # Opus 帧：960样本 int16 = 20ms @48kHz
PACKET_PERIOD = cfg_get("timing", "packet_period", default=0.12)  # 每包120ms匀速
LEAD_DELAY = cfg_get("timing", "lead_delay", default=0.5)         # UserTalking→首包 0.5s
TAIL_FLUSH = cfg_get("timing", "tail_flush", default=0.3)         # 发包后尾巴冲刷
TRIM_SILENCE = cfg_get("audio", "trim_silence", default=True)     # 头尾静音裁剪开关
def m_varint_enc(n):
    """Mumble UDP 语音包头 varint 编码（前缀式，区别于 protobuf varint）"""
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([0x80 | (n >> 8), n & 0xFF])
    if n < 0x200000:
        return bytes([0xC0 | (n >> 16), (n >> 8) & 0xFF, n & 0xFF])
    if n < 0x10000000:
        return bytes([0xE0 | (n >> 24), (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])
    raise ValueError("varint 值过大: %d" % n)
def m_varint_dec(data, i):
    """Mumble UDP 语音包头 varint 解码（前缀式，与 m_varint_enc 镜像）。"""
    b = data[i]
    if b < 0x80:
        return b, i + 1
    if b < 0xC0:
        return ((b & 0x3F) << 8) | data[i + 1], i + 2
    if b < 0xE0:
        return ((b & 0x1F) << 16) | (data[i + 1] << 8) | data[i + 2], i + 3
    if b < 0xF0:
        return ((b & 0x0F) << 24) | (data[i + 1] << 16) | (data[i + 2] << 8) | data[i + 3], i + 4
    raise ValueError("Mumble varint 越界")
def parse_udp_voice_multi(payload, own_session=None):
    """解析下行 UDPTunnel 载荷中的**全部**语音包（服务器按批转发，
    一个载荷可能串联多个连续语音包，如 600ms 批 5 包×120ms）。
    若只取第一个包，其余包被丢弃会导致录音变成倍速+断续。
    返回 list of dict(session, seq, opus)。"""
    out = []
    i = 0
    n = len(payload) if payload else 0
    while i + 2 <= n:
        if (payload[i] & 0xE0) != 0x20:
            break                          # 非 Opus 语音，停止
        try:
            session, j = m_varint_dec(payload, i + 1)
            seq, j = m_varint_dec(payload, j)
            ln, j = m_varint_dec(payload, j)
        except (ValueError, IndexError):
            break
        if ln <= 0 or j + ln > n:
            break
        if own_session is None or session != own_session:
            out.append({"session": session, "seq": seq,
                        "opus": payload[j:j + ln]})
        i = j + ln
    return out


def parse_udp_voice(payload, own_session=None):
    """解析链路下行语音包（UDPTunnel 内层，msg_type=1）。
    标准 Mumble 下行格式: [0x20][varint session][varint seq][varint len][Opus数据]。
    返回 dict(session, seq, opus) 或 None（非语音/长度异常/自己回声）。
    own_session 传入时丢弃自己的回声包（防点名把播报内容识别成应答）。
    兼容单包调用：内部走 parse_udp_voice_multi 取第一个。"""
    pkts = parse_udp_voice_multi(payload, own_session)
    return pkts[0] if pkts else None
def trim_silence(pcm, threshold=None, min_ms=None):
    """s16le 单声道 48kHz PCM 头尾静音截断，各保留 min_ms 余量。"""
    if threshold is None:
        threshold = cfg_get("audio", "trim_threshold", default=200)
    if min_ms is None:
        min_ms = cfg_get("audio", "trim_min_ms", default=50)
    if not pcm:
        return pcm
    samples = array.array('h')
    samples.frombytes(pcm)
    n = len(samples)
    keep = int(48000 * min_ms / 1000)
    start = 0
    for i in range(n):
        if abs(samples[i]) > threshold:
            start = max(0, i - keep)
            break
    else:
        return b""
    end = n
    for i in range(n - 1, -1, -1):
        if abs(samples[i]) > threshold:
            end = min(n, i + keep + 1)
            break
    return samples[start:end].tobytes()
def build_audio(mp3_path):
    """mp3 → (语音包列表, 总帧数)。帧不足30字节补零，尾包不足6帧补零帧。"""
    pcm, src_rate = Mpg123Decoder().decode(mp3_path)
    if TRIM_SILENCE:
        pcm = trim_silence(pcm)
        print("已裁切头尾静音（trim_silence on）")
    print("mp3 解码: 源 %dHz → 重采样 48000Hz 单声道" % src_rate)
    if len(pcm) < FRAME_BYTES:
        raise RuntimeError("mp3 解码结果为空（%.1fKB），文件损坏或路径错误" % (len(pcm) / 1024))
    enc = OpusEncoder()
    frames = []
    for off in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[off:off + FRAME_BYTES]
        if len(chunk) < FRAME_BYTES:                 # 末尾不足一帧补零
            chunk += b"\x00" * (FRAME_BYTES - len(chunk))
        f = enc.encode_frame(chunk)
        if len(f) > 30:
            raise RuntimeError("Opus 帧长 %d 超过30，请检查码率设置" % len(f))
        frames.append(f + b"\x00" * (30 - len(f)))  # 统一30字节
    # 6帧合一包 185 字节: [0x20][varint seq][varint len=180] + 180字节（标准 Mumble 上行格式）
    # session 字段是服务器下行转发时才注入的来源标记，上行不能带（v2 教训）。
    # len 必须编码为 0x80 0xB4=180（v1 教训: 0x81 0xB4 会被解码成 436）。
    # seq 每次上台从 200 起每包 +6（listen_capture.py 官方下行实测）。
    packets = []
    for i in range(0, len(frames), 6):
        chunk = frames[i:i + 6]
        while len(chunk) < 6:
            chunk.append(b"\x00" * 30)
        head = bytes([0x20]) + m_varint_enc(200 + i) + m_varint_enc(180)
        packets.append(head + b"".join(chunk))
    return packets, len(frames)
# ---------------- 连接与消息循环（与 validate_client.py 同源已验证） ----------------
class Client:
    def __init__(self, host, port, use_tls):
        self.sock = socket.create_connection((host, port), timeout=10)
        # 禁用 Nagle：185B 语音小包需即时发出，攒包会叠加节奏抖动（官方客户端同款）
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.sock = ctx.wrap_socket(self.sock, server_hostname=host)
        self.buf = b""
    def send(self, msg_type, payload):
        self.sock.sendall(frame(msg_type, payload))
    def pump(self, seconds):
        msgs = []
        end = time.time() + seconds
        self.sock.settimeout(0.3)
        while time.time() < end:
            try:
                d = self.sock.recv(8192)
                if not d:
                    break
                self.buf += d
            except socket.timeout:
                continue
            except OSError:
                break
            while len(self.buf) >= 6:
                t, l = struct.unpack(">HI", self.buf[:6])
                if t > 49 or l > 500000:
                    print("!! 收到非法帧，连接异常")
                    return msgs
                if len(self.buf) < 6 + l:
                    break
                msgs.append((t, self.buf[6:6 + l]))
                self.buf = self.buf[6 + l:]
        return msgs
    def drain(self, idle_rounds=2):
        """非阻塞尽量读空当前可读数据并解析成消息（守护线程高频泵用）。
        连续 idle_rounds 轮（每轮50ms超时）无新数据即返回，单次耗时<100ms。"""
        msgs = []
        idle = 0
        self.sock.settimeout(0.05)
        while idle < idle_rounds:
            try:
                d = self.sock.recv(65536)
                if not d:
                    break                      # 对端关闭
                self.buf += d
                idle = 0
            except socket.timeout:
                idle += 1
            except OSError:
                break
            while len(self.buf) >= 6:
                t, l = struct.unpack(">HI", self.buf[:6])
                if t > 49 or l > 500000:
                    return msgs                # 非法帧
                if len(self.buf) < 6 + l:
                    break
                msgs.append((t, self.buf[6:6 + l]))
                self.buf = self.buf[6 + l:]
        return msgs
    def wait_for(self, seconds, wanted_types, on_msg=None):
        end = time.time() + seconds
        while time.time() < end:
            for t, p in self.pump(0.5):
                if t in wanted_types:
                    return t, p
                if on_msg is not None:        # 非目标消息转发（播报线程独占读期间不丢下行）
                    try:
                        on_msg(t, p)
                    except Exception:
                        pass
        return None, None
    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
def login(c, username, password, ent_id=None, model=None):
    """Version+Login → 等 ServerSync。Reject type=0 是平台自定义的登录成功通知，忽略。"""
    c.send(0, build_version())
    c.send(3, build_login(username, password, ent_id, model))
    deadline = time.time() + 10
    while time.time() < deadline:
        for t, p in c.pump(0.5):
            if t == 7:
                return pb_dict(p).get(1)
            if t == 6 and pb_dict(p).get(1) != 0:
                raise RuntimeError("登录被拒: %s" % pb_dict(p))
    raise RuntimeError("10秒内未收到 ServerSync")
# ---------------- 主入口（函数化，供 announce.py 调度器调用） ----------------
class DirectAnnouncer:
    """直连播报会话，支持分步执行供 announce.py 预建链复用：
    connect() 建链+登录 → take_mic() 抢麦+上报开始说话 → play(packets) 匀速发包+放麦。
    announce_once() 是一步到位的便捷组合（CLI / 容错兜底用）。
    凭据由构造函数传入，不依赖模块级硬编码。"""
    def __init__(self, host=HOST, port=PORT, use_tls=True,
                 username=USERNAME, password=PASSWORD, ent_id=None, model=MODEL,
                 on_msg=None):
        self.host, self.port, self.use_tls = host, port, use_tls
        self.username, self.password, self.ent_id = username, password, ent_id
        self.model = model
        self.on_msg = on_msg            # 可选下行回调（t, p）：播报线程读到非目标消息时
                                        # 转发（点名接收侧在抢麦/发包期间不能丢下行）
        self.c = None
        self.session = None
    def connect(self):
        """建链（TCP+TLS）+ 登录，拿回 session id"""
        self.c = Client(self.host, self.port, self.use_tls)
        print("已连接 %s:%d (%s)" % (self.host, self.port, "TLS" if self.use_tls else "明文"))
        self.session = login(self.c, self.username, self.password, self.ent_id, self.model)
        print("登录成功 session=%s" % self.session)
    def take_mic(self):
        """抢麦 + 上报开始说话（UI显示说话人/中继台建链触发）。
        播报线程独占读取 socket（PersistentAnnouncer.keeper 在 busy 期间让位）：
        wait_for 读到 ApplyMic 回执前，途中收到的其他下行（他人抢台信令/语音包）
        经 on_msg 回调出去，点名接收侧不丢数据。"""
        c = self.c
        c.send(14, build_apply_mic(True))
        t, p = c.wait_for(6, (14,), on_msg=self.on_msg)
        d = pb_dict(p) if t == 14 else {}
        if t is None or not d.get(3):
            raise RuntimeError("抢麦失败: %s" % (d or "6秒无响应"))
        c.send(15, build_user_talking(self.session, True))
        print("抢麦成功，已上报 UserTalking(talking=true)")
    def play(self, packets, verbose=True, abort_check=None):
        """UserTalking 之后先等 LEAD_DELAY（官方时序 0.5s，中继台靠该间隔建链），
        再匀速发包(120ms/包)→尾巴冲刷→上报停止说话→放麦→收回执。成功返回 True。
        必须在 take_mic() 之后调用。
        abort_check：每批发包前调用的回调（返回 True 表示信道被他人占用/抢台，
        立即停止发包并放麦让位，返回 False）。用于点名播报时检测他人讲话。
        发包循环内周期性 pump 下行：①保持他人抢台检测（abort_check 依赖
        下行 UserTalking 信令）；②busy 期间 socket 由本线程独占读取（keeper
        让位），非目标消息经 on_msg 转发给点名接收侧。"""
        c = self.c
        if verbose:
            print("延迟 %.0fms 后开始发包" % (LEAD_DELAY * 1000))
        time.sleep(LEAD_DELAY)
        start = time.time()
        n_sent = 0
        for i, pkt in enumerate(packets):     # 匀速 120ms/包（官方实时编码节奏，v6）
            if abort_check is not None and abort_check():
                # 他人抢台/讲话 → 立即放麦让位（不发送剩余包，避免与对方抢信道）
                c.send(15, build_user_talking(self.session, False))
                c.send(14, build_apply_mic(False))
                if verbose:
                    print("播报被抢占，已放麦让位（已发 %d/%d 包）" % (n_sent, len(packets)))
                return False
            c.send(1, pkt)
            n_sent += 1
            if verbose and n_sent % 50 == 0:
                print("  已发 %d/%d 包 (%.0f%%)" % (n_sent, len(packets), 100 * n_sent / len(packets)))
            # 发包间隙读下行：放在 sleep 前（pump 最多阻塞 20ms，被等待窗口吸收，
            # 不破坏 120ms 绝对节奏）。保持他人抢台检测 + busy 期 socket 独占读取。
            if self.on_msg is not None:
                for t, p in c.pump(0.02):
                    self.on_msg(t, p)
            target = start + (i + 1) * PACKET_PERIOD   # 绝对时钟对齐，消除累计漂移
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
        time.sleep(TAIL_FLUSH)                 # 尾巴冲刷（官方同款300ms，可配置）
        c.send(15, build_user_talking(self.session, False))  # 上报停止说话
        c.send(14, build_apply_mic(False))
        if verbose:
            print("发包完成，已放麦（耗时 %.1f秒）" % (time.time() - start))
        for t, p in c.pump(0.5):        # 收回执（v8: 3s→0.5s，回执即刻到达，仅诊断用）：
                                        # 自己session的UserTalking广播(含服务器录音url)=完全认可
            if t in (14, 15) and verbose:
                print("    <- %s: %s" % ("ApplyMic" if t == 14 else "UserTalking", pb_dict(p)))
        return True
    def close(self):
        if self.c:
            self.c.close()
            self.c = None
class PersistentAnnouncer:
    """常驻直连会话（v11.1）：守护线程小粒度循环（每0.5s 醒一次，锁内 Ping 保活
    + drain 读空下行，单次锁持有<100ms）。服务器画像 = 长在线用户（拟真防风控）。
    线程安全设计（v11.1 修正，v11 的 ensure_session 直接碰 socket 与守护线程
    pump 竞争，回显被抢走导致健康检查等满超时+误重建，首包晚 8 秒）：
    - 所有 socket IO 只在守护线程锁内发生，且锁内复检 busy（防 TOCTOU）
    - ensure_session() 纯状态查询不碰 socket：健康判据 = _last_ok（守护线程
      最近收到 Ping 回显的时刻）距今 ≤8s；ping 周期 2.5s，3 个周期容错
    - 播报独占（busy）期间守护线程完全不碰 socket
    被顶（UserRemove ID=20 带自己 session）→ 事件回调 + 退避重连（翻倍至30min
    上限防互踢风暴）；断线异常 → 守护线程自动重连，失败 10s 后再试。"""
    def __init__(self, host=HOST, port=PORT, use_tls=True,
                 username=USERNAME, password=PASSWORD, ent_id=None, model=MODEL,
                 ping_interval=2.5, on_event=None, on_downlink=None, on_msg=None):
        self._cfg = dict(host=host, port=port, use_tls=use_tls,
                         username=username, password=password, ent_id=ent_id,
                         model=model)
        self._ping_interval = ping_interval
        self._on_event = on_event          # on_event(kind, detail)：removed/reconnected/dead
        self._on_downlink = on_downlink    # on_downlink(msg_type, payload)：下行泵每帧回调（点名接收侧用）
        self.on_msg = on_msg               # 播报线程独占读期间的额外下行回调（同上，busy 期由
                                           # DirectAnnouncer 内部 pump 触发，点名接收侧不丢数据）
        self._sess = None                  # 当前 DirectAnnouncer
        self._lock = threading.Lock()      # 串行化 socket IO 与会话获取
        self._busy = threading.Event()     # 播报独占标志
        self._stop = threading.Event()
        self._thread = None
        self._backoff = 30.0               # 被顶重连退避秒数
        self._last_ping = 0.0
        self._last_ok = 0.0                # 最近一次收到 Ping 回显的时刻
        self._suspended = False            # 临时直连播报期间暂停保活/重连（防同账号互踢）
    # ---- 生命周期 ----
    def start(self):
        with self._lock:
            self._rebuild()
            self._sess.c.send(5, b"")              # 立即 Ping，加速首个回显
            self._last_ping = time.time()
        self._thread = threading.Thread(target=self._keeper, daemon=True,
                                        name="native-link-keeper")
        self._thread.start()
    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            if self._sess:
                self._sess.close()
                self._sess = None
    def _rebuild(self):
        """建立新连接替换旧的（须持锁调用）"""
        s = DirectAnnouncer(**self._cfg, on_msg=self.on_msg)
        s.connect()
        if self._sess:
            try:
                self._sess.close()
            except Exception:
                pass
        self._sess = s
        self._last_ok = 0.0                        # 等首个 Ping 回显到达后才算健康
        return s
    # ---- 守护线程 ----
    def _keeper(self):
        while not self._stop.is_set():
            time.sleep(0.5)
            if self._busy.is_set():
                # 播报独占期间**完全不碰 socket**：take_mic/play 在播报线程内
                # 独占读取（on_msg 泵下行），keeper 若同时 drain 会把 ApplyMic
                # 回执/下行消息抢走 → 主线程 wait_for 6s 无回执报"抢麦失败"
                # （实测 22:05-22:06 连续 3 次常驻抢麦失败、短链兜底的根因）。
                # 他人抢台检测由 DirectAnnouncer.play 的 on_msg 泵保持，不丢。
                continue
            removed = False
            with self._lock:
                if self._suspended:
                    # 临时直连播报（announce_once/点名短链）期间：不 Ping 不重连，
                    # 避免与临时连接并发登录同一账号触发服务器"同账号互踢"
                    # （实测 20:30 常驻链路被顶、退避重连又顶回直连的互踢循环）
                    continue
                if self._busy.is_set():            # 锁内复检（防 TOCTOU）
                    continue
                s = self._sess
                if s is not None and s.c is not None:
                    try:
                        if time.time() - self._last_ping >= self._ping_interval:
                            s.c.send(5, b"")       # Ping（空消息，网页版同款）
                            self._last_ping = time.time()
                        for t, p in s.c.drain():   # 下行泵：读空即走（<100ms）
                            if t == 5:
                                self._last_ok = time.time()
                            elif t == 20 and pb_dict(p).get(1) == s.session:
                                removed = True     # 自己被顶（同账号别处登录）
                            if self._on_downlink is not None:
                                try:
                                    self._on_downlink(t, p)   # 分发下行帧（点名接收侧消费）
                                except Exception:
                                    pass
                    except Exception:
                        try:
                            s.close()
                        except Exception:
                            pass
                        self._sess = None
            if removed:
                self._emit("removed", "同账号在别处登录，常驻连接被顶")
                with self._lock:
                    if self._sess:
                        try:
                            self._sess.close()
                        except Exception:
                            pass
                    self._sess = None
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 1800)   # 退避翻倍防互踢风暴
                continue
            if self._sess is None:                 # 异常断开/被顶退避后 → 重连
                self._reconnect_safe()
    def _reconnect_safe(self):
        ok = False
        with self._lock:
            if self._busy.is_set() or self._stop.is_set():
                return
            try:
                s = self._rebuild()
                s.c.send(5, b"")                   # 立即 Ping，加速首个回显
                self._last_ping = time.time()
                self._backoff = 30.0
                ok = True
                self._emit("reconnected", "常驻链路重连成功")
            except Exception as e:
                self._emit("dead", "重连失败: %s" % e)
        if not ok:
            time.sleep(10)
    def _emit(self, kind, detail):
        if self._on_event:
            try:
                self._on_event(kind, detail)
            except Exception:
                pass
    # ---- 播报接口 ----
    def ensure_session(self):
        """返回健康的独占会话；不健康返回 None（调用方走临时短链兜底）。
        纯状态查询（不碰 socket，与守护线程零竞争）：健康判据 = 守护线程最近
        8 秒内收到过 Ping 回显。返回的会话处于独占态，播完必须 release()。
        忙锁已占用时返回 None（点名与播报互斥：同一时刻只允许一个发射者）。"""
        with self._lock:
            s = self._sess
            if (not self._busy.is_set() and s is not None and s.c is not None
                    and time.time() - self._last_ok <= 8.0):
                self._busy.set()
                return s
        return None
    def release(self):
        """播报结束，连接归还守护线程继续保活"""
        self._busy.clear()
    def suspend(self):
        """临时直连播报期间暂停守护保活/重连，并**主动断开现有连接**让出登录名额
        （防同账号互踢：残留登录状态的常驻连接会被临时连接顶掉，resume 后重连又
        顶回临时连接——实测 20:51 短链抢麦被重连常驻顶掉而失败）。resume 后重连。"""
        with self._lock:
            self._suspended = True
            if self._sess is not None:
                try:
                    self._sess.close()
                except Exception:
                    pass
                self._sess = None
                self._last_ok = 0.0
    def resume(self):
        """恢复常驻链路保活/重连（临时直连播报已结束，可安全回到单连接）。"""
        with self._lock:
            self._suspended = False
            self._backoff = 30.0          # 重置退避：尽快恢复正常保活，避免长退避空窗
def announce_once(mp3_path, host=HOST, port=PORT, use_tls=True, ent_id=None,
                  username=USERNAME, password=PASSWORD, verbose=True):
    """完整播报一次（一步到位）：编码→登录→抢麦→延迟LEAD_DELAY→匀速发包→放麦。
    供 CLI 与 announce.py 容错兜底；准点播报请用 DirectAnnouncer 预建链分步调用。"""
    packets, n_frames = build_audio(mp3_path)
    if verbose:
        print("音频就绪: %.1f秒 → %d 帧 → %d 包（每%.0fms一包，预计上台 %.1f秒）" %
              (n_frames * 0.02, n_frames, len(packets),
               PACKET_PERIOD * 1000, len(packets) * PACKET_PERIOD))
    s = DirectAnnouncer(host, port, use_tls, username, password, ent_id)
    try:
        s.connect()
        s.take_mic()
        s.play(packets, verbose)   # play 内部先等 LEAD_DELAY（v7 官方时序，中继台建链需要）
        return True
    finally:
        s.close()
def main():
    ap = argparse.ArgumentParser(description="滔滔直连播报客户端")
    ap.add_argument("--mp3", required=True, help="播报 mp3 文件路径")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--tls", action="store_true", default=True)
    ap.add_argument("--ent-id", type=int, default=None)
    ap.add_argument("--config", default=None,
                    help="配置文件路径（默认取环境变量 HAM_BOT_CONFIG 或同目录 config.json）")
    ap.add_argument("--dry-run", action="store_true", help="只跑本地音频管线，不联网")
    args = ap.parse_args()
    if args.config:
        # 重载配置：更新模块级 CFG 与派生常量（全局 dict 刷新，避免 import 期绑定）
        new_cfg, _ = load_config(args.config)
        _t = new_cfg.get("talk", {}) or {}
        _cl = _t.get("client_profile", {}) or {}
        g = globals()
        g["CFG"] = new_cfg
        g["HOST"] = _t.get("host", "totalkd.allptt.com")
        g["PORT"] = _t.get("port", 59638)
        g["USE_TLS"] = _t.get("use_tls", True)
        g["USERNAME"] = _t.get("username", "")
        g["PASSWORD"] = _t.get("password", "")
        g["RELEASE"] = _cl.get("release", "V2.8.5")
        g["OS_NAME"] = _cl.get("os_name", "Android")
        g["OS_VERSION"] = _cl.get("os_version", "16")
        g["MODEL"] = _cl.get("model", "PKT110")
    if args.dry_run:
        packets, n_frames = build_audio(args.mp3)
        print("管线自测: %.1f秒 → %d 帧 → %d 包" % (n_frames * 0.02, n_frames, len(packets)))
        print("首包 %d 字节: %s ..." % (len(packets[0]), packets[0][:16].hex()))
        lens = set(len(p) for p in packets)
        print("包长集合: %s（应全为185）" % sorted(lens))
        return
    ok = announce_once(args.mp3, args.host, args.port, args.tls, args.ent_id)
    print("\n===== %s =====" % ("播报完成" if ok else "播报失败"))
if __name__ == "__main__":
    main()
