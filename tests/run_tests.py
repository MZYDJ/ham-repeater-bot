#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点名主播离线单元测试（无网络依赖）。
运行：python3 tests/run_tests.py
覆盖：字母解释法解码/呼号校验/信号提取/去重、Mumble varint 编解码、
UDP 语音包解析（含回声过滤）、Opus 编解码往返、VAD 切段、WAV 落盘、配置回退。
"""
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import direct_announce
import net_control
from net_control import (decode_callsign, extract_signal, is_duplicate,
                         VoiceCapture, write_wav, AsrClient, callsign_phonetic)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_varint_roundtrip():
    print("[varint 编解码往返]")
    for v in [0, 1, 30, 127, 128, 180, 200, 16383, 16384, 500000, 0x0FFFFFFF]:
        enc = direct_announce.m_varint_enc(v)
        dec, i = direct_announce.m_varint_dec(enc, 0)
        check(f"m_varint({v})", dec == v and i == len(enc),
              f"enc={enc.hex()} dec={dec} consumed={i}")


def test_parse_udp_voice():
    print("[UDP 语音包解析]")
    payload = bytes([0x20]) + direct_announce.m_varint_enc(42) \
        + direct_announce.m_varint_enc(206) + direct_announce.m_varint_enc(180) \
        + b"\x01" * 180
    v = direct_announce.parse_udp_voice(payload)
    check("解析 session/seq/len", v is not None and v["session"] == 42
          and v["seq"] == 206 and len(v["opus"]) == 180)
    check("回声过滤(own_session)", direct_announce.parse_udp_voice(payload, own_session=42) is None)
    check("非语音包", direct_announce.parse_udp_voice(bytes([0x40, 0x01, 0x02])) is None)
    check("长度不足", direct_announce.parse_udp_voice(bytes([0x20, 0x01])) is None)
    bad_len = bytes([0x20]) + direct_announce.m_varint_enc(1) \
        + direct_announce.m_varint_enc(1) + direct_announce.m_varint_enc(999) + b"\x01"
    check("长度与负载不符", direct_announce.parse_udp_voice(bad_len) is None)


def test_opus_roundtrip():
    print("[Opus 编解码往返（真实 libopus）]")
    import math
    enc = direct_announce.OpusEncoder()
    dec = direct_announce.OpusDecoder()
    # 编码器只接受 Opus 标准帧长（20ms=960 样本），与生产路径逐帧编码一致
    pcm_all = bytearray()
    opus_all = bytearray()
    for i in range(48000):                       # 1 秒 1kHz 正弦 16bit @48k
        v = int(12000 * math.sin(2 * math.pi * 1000 * i / 48000))
        pcm_all += struct.pack("<h", v)
    for off in range(0, len(pcm_all), 1920):
        opus_all += enc.encode_frame(bytes(pcm_all[off:off + 1920]))
    out = bytearray()
    for off in range(0, len(opus_all) - 29, 30):  # CBR 12kbps 下 20ms 帧恰 30B
        out += dec.decode(bytes(opus_all[off:off + 30]))
    check("Opus 往返长度", len(out) == len(pcm_all),
          f"enc={len(opus_all)}B out={len(out)}B in={len(pcm_all)}B")
    check("Opus 非静音", max(out) > 100 or min(out) < -100)
    # 单帧（20ms）往返
    pcm20 = bytes(pcm_all[:3840])
    opus20 = enc.encode_frame(pcm20)
    out20 = dec.decode(opus20)
    check("Opus 20ms 帧往返", len(out20) == len(pcm20), f"out={len(out20)} in={len(pcm20)}")
    dec.close()


def test_voice_capture():
    print("[VAD 切段]")
    import math
    tone = bytearray()
    for i in range(48000 * 3):                   # 3 秒 1kHz 正弦
        v = int(20000 * math.sin(2 * math.pi * 1000 * i / 48000))
        tone += struct.pack("<h", v)
    silence = bytearray(48000 * 1 * 2)           # 1 秒静音

    segs = []
    cap = VoiceCapture(threshold=2000, silence_end_ms=400, min_segment_ms=300,
                       save_dir=None, on_segment=lambda p, d, w: segs.append((d, w)))
    cap.feed(bytes(silence[:4800]))
    cap.feed(bytes(tone))
    cap.feed(bytes(silence))
    # 段 = 3s 语音 + 400ms 收尾静音
    check("说话段完整切出", len(segs) == 1 and 3.0 <= segs[0][0] <= 3.5,
          f"segs={[(round(d,1), w) for d, w in segs]}")

    segs2 = []
    cap2 = VoiceCapture(threshold=2000, silence_end_ms=400, min_segment_ms=300,
                        save_dir=None, on_segment=lambda p, d, w: segs2.append((d, w)))
    cap2.feed(bytes(tone[:4800]))                # 100ms 短音 → 低于 min_segment，丢弃
    cap2.feed(bytes(silence[:9600]))
    check("短音被过滤", len(segs2) == 0)

    segs3 = []
    cap3 = VoiceCapture(threshold=2000, silence_end_ms=400, min_segment_ms=300,
                        max_segment_ms=1000, save_dir=None,
                        on_segment=lambda p, d, w: segs3.append((d, w)))
    cap3.feed(bytes(tone))                       # 3s 音 → 1s 强制截断
    cap3.feed(bytes(silence[:9600]))
    check("超长段强制截断", len(segs3) >= 1 and segs3[0][0] <= 1.1,
          f"segs3={[round(d,1) for d,_ in segs3]}")

    segs4 = []
    cap4 = VoiceCapture(threshold=2000, silence_end_ms=5000, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w: segs4.append((d, w)))
    cap4.feed(bytes(tone[:9600]))                # 100ms 音（未达静音阈值 5s）
    cap4.force_finalize()                        # UserTalking 结束信令 → 立即切段
    check("结束信令立即切段", len(segs4) == 1 and 0.08 <= segs4[0][0] <= 0.12,
          f"segs4={[(round(d,1), w) for d, w in segs4]}")
    cap4.force_finalize()                        # 无活动段时无副作用
    check("空段无副作用", len(segs4) == 1)


def test_wav_and_resample():
    print("[WAV 落盘与重采样]")
    import math
    pcm48 = bytearray()
    for i in range(48000):
        v = int(12000 * math.sin(2 * math.pi * 1000 * i / 48000))
        pcm48 += struct.pack("<h", v)
    pcm16 = direct_announce.resample_linear(bytes(pcm48), 48000, 16000)
    check("重采样长度", len(pcm16) == 16000 * 2, f"out={len(pcm16)}")
    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "t.wav")
        write_wav(wav, pcm16)
        data = Path(wav).read_bytes()
        check("RIFF 头", data[:4] == b"RIFF" and data[8:12] == b"WAVE")
        rate = struct.unpack("<I", data[24:28])[0]
        check("采样率 16k", rate == 16000, f"rate={rate}")


def test_decode_callsign():
    print("[呼号解码（字母解释法 + 直读 + 中文音译 + 信号提取）]")
    r = decode_callsign("Bravo Hotel Three X-ray X-ray 信号五九")
    check("ITU 解释法", r["callsign"] == "BH3XX" and r["signal"] == "59",
          f"{r}")
    r = decode_callsign("抄收，这里是BH3XXX，信号59")
    check("呼号直读", r["callsign"] == "BH3XXX" and r["signal"] == "59", f"{r}")
    r = decode_callsign("博拉沃 霍特尔 三 艾克斯 艾克斯 信号五十九")
    check("中文音译", r["callsign"] == "BH3XX" and r["signal"] == "59", f"{r}")
    r = decode_callsign("抄收信号很好")
    check("无呼号", r["callsign"] is None and r["score"] < 60, f"{r}")
    r = decode_callsign("Bravo Hotel Three X-ray X-ray")
    check("无信号报告", r["callsign"] == "BH3XX" and r["signal"] is None, f"{r}")
    r = decode_callsign("BG9ABC")
    check("短呼号直读", r["callsign"] == "BG9ABC", f"{r}")
    r = decode_callsign("这里是一号台，请抄收")
    check("无合法呼号", r["callsign"] is None, f"{r}")
    r = decode_callsign("BH3XX59")               # 信号数字粘连 → 快路径不吞数字
    check("粘连数字不吞入", r["callsign"] == "BH3XX", f"{r}")
    check("大小写归一去重", is_duplicate("bh3xx", {"BH3XX"}))


def test_asr_body():
    print("[ASR 请求体（离线构造）]")
    c = AsrClient(api_key="sk-test")
    body = c._build_body(b"\x00\x00\x00\x00", context_words=["BRAVO", "BH3XX"])
    check("OpenAI 兼容结构", body["model"] == "qwen3-asr-flash"
          and body["messages"][0]["role"] == "system" and not body["stream"])
    check("itn 默认开", body["asr_options"]["enable_itn"] is True)
    sysmsg = body["messages"][0]["content"]
    check("词表进 System", "BH3XX" in sysmsg and "BRAVO" in sysmsg)


def test_config_defaults():
    print("[配置回退（无 config.json 时全默认）]")
    check("net_control 默认关闭", net_control.nc_cfg("enabled", default=False) is False)
    check("默认阈值", net_control.nc_cfg("vad_threshold", default=800) == 800)
    check("默认解释词可用", net_control.PHONETIC_ITU["bravo"] == "B")


def test_templates():
    print("[话术模板（TTS 占位符）]")
    check("解释法回读", callsign_phonetic("BH3XX") == "Bravo Hotel Three X-ray X-ray")
    check("解释法数字", callsign_phonetic("BG9ABC").endswith("Nine Alpha Bravo Charlie"))
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"weekday": "周一", "net_name": "应急通讯演练台网点名",
                     "repeater_call": "BR9AB", "ctrl_call": "BI9BZW",
                     "ctrl_phonetic": "Bravo India Nine Bravo Zulu Whiskey",
                     "main_qth": "咸阳市渭城区塔尔坡", "main_device": "泉盛K6",
                     "main_antenna": "原机天线", "main_power": "5瓦",
                     "frequency": "439.775", "offset": "7", "tone": "88.5"}
    opening = sess._fmt("CQ CQ CQ，这里是{repeater_call}业余无线电中继台，现在是每周{weekday}晚{net_name}，"
                        "我是今晚主控{ctrl_call}，今天是{date}，现在是北京时间{time}，我的QTH位于{main_qth}，"
                        "所用设备{main_device}，{main_antenna}，{main_power}功率发射，"
                        "这里是{ctrl_phonetic} {ctrl_call}，Over")
    check("动态占位符展开", "BR9AB" in opening and "周" in opening and "年" in opening
          and "点" in opening and "咸阳市渭城区塔尔坡" in opening
          and "Bravo India Nine" in opening, opening[:40])
    ack = sess._fmt("{call_phonetic}，这里是{ctrl_call}，抄收你的信号{report}，请报告您的QTH。Over",
                    call="BH3XX", call_phonetic=callsign_phonetic("BH3XX"), report="59")
    check("应答级占位符", ack.startswith("Bravo Hotel Three X-ray X-ray，这里是BI9BZW")
          and "信号59" in ack, ack[:50])
    missing = sess._fmt("这里有{不存在的占位符}")
    check("缺失占位符兜底", missing == "这里有{不存在的占位符}")
    sess._teardown_dir = None  # 防误删（未 start 的会话无副作用）



def main():
    print("== 点名主播离线单元测试 ==")
    for fn in [test_varint_roundtrip, test_parse_udp_voice, test_opus_roundtrip,
               test_voice_capture, test_wav_and_resample, test_decode_callsign,
               test_asr_body, test_config_defaults, test_templates]:
        fn()
    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
