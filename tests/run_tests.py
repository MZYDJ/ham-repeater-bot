#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点名主播离线单元测试（无网络依赖）。
运行：python3 tests/run_tests.py
覆盖：字母解释法解码/呼号校验/信号提取/去重、Mumble varint 编解码、
UDP 语音包解析（含回声过滤）、Opus 编解码往返、VAD 切段、WAV 落盘、配置回退。
"""
import csv
import http.client
import os
import struct
import sys
import tempfile
import time
import datetime
from pathlib import Path
from unittest import mock

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
    # 单帧（20ms=960样本）往返：encode → 30B → decode 还原 1920B
    pcm20 = bytes(pcm_all[:1920])
    opus20 = enc.encode_frame(pcm20)
    out20 = dec.decode(opus20)
    check("Opus 20ms 帧往返", len(out20) == len(pcm20), f"out={len(out20)} in={len(pcm20)}")
    # 6帧拼接 180B（生产下行形态，发送端逐帧编码后串联）→ decode 还原 6×1920B
    six = b"".join(enc.encode_frame(bytes(pcm_all[off:off + 1920]))
                   for off in range(0, 1920 * 6, 1920))
    out6 = dec.decode(six)
    check("Opus 6帧拼接往返", len(out6) == 1920 * 6,
          f"out={len(out6)} in={1920 * 6}")
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
                       save_dir=None, on_segment=lambda p, d, w, s: segs.append((d, w)))
    cap.feed(bytes(silence[:4800]))
    cap.feed(bytes(tone))
    cap.feed(bytes(silence))
    # 段 = 3s 语音 + 400ms 收尾静音
    check("说话段完整切出", len(segs) == 1 and 3.0 <= segs[0][0] <= 3.5,
          f"segs={[(round(d,1), w) for d, w in segs]}")

    segs2 = []
    cap2 = VoiceCapture(threshold=2000, silence_end_ms=400, min_segment_ms=300,
                        save_dir=None, on_segment=lambda p, d, w, s: segs2.append((d, w)))
    cap2.feed(bytes(tone[:4800]))                # 100ms 短音 → 低于 min_segment，丢弃
    cap2.feed(bytes(silence[:9600]))
    check("短音被过滤", len(segs2) == 0)

    segs3 = []
    cap3 = VoiceCapture(threshold=2000, silence_end_ms=400, min_segment_ms=300,
                        max_segment_ms=1000, save_dir=None,
                        on_segment=lambda p, d, w, s: segs3.append((d, w)))
    cap3.feed(bytes(tone))                       # 3s 音 → 1s 强制截断
    cap3.feed(bytes(silence[:9600]))
    check("超长段强制截断", len(segs3) >= 1 and segs3[0][0] <= 1.1,
          f"segs3={[round(d,1) for d,_ in segs3]}")

    segs4 = []
    cap4 = VoiceCapture(threshold=2000, silence_end_ms=5000, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w, s: segs4.append((d, w)))
    cap4.feed(bytes(tone[:9600]))                # 100ms 音（未达静音阈值 5s）
    cap4.force_finalize()                        # UserTalking 结束信令 → 立即切段
    check("结束信令立即切段", len(segs4) == 1 and 0.08 <= segs4[0][0] <= 0.12,
          f"segs4={[(round(d,1), w) for d, w in segs4]}")
    cap4.force_finalize()                        # 无活动段时无副作用
    check("空段无副作用", len(segs4) == 1)

    # 链路静默收尾：feed 停后（对方讲完、无新帧），pump 按实际时间推进切段
    segs5 = []
    cap5 = VoiceCapture(threshold=2000, silence_end_ms=400, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w, s: segs5.append((d, w)))
    cap5.feed(bytes(tone[:9600]))                # 200ms 音，说完后不再喂帧（链路静默）
    cap5.pump()                                  # 立即 pump：idle≈0，不收尾
    check("静默未满不切段", len(segs5) == 0)
    with mock.patch("net_control.time.time", return_value=cap5.last_feed_at + 1.0):
        cap5.pump()                              # 静默 1s > 400ms → 切段
    check("链路静默时间驱动切段", len(segs5) == 1 and 0.08 <= segs5[0][0] <= 0.12,
          f"segs5={[(round(d,2), w) for d, w in segs5]}")

    # PTT 抬起延迟窗口：defer 未耗尽时 pump 不切段；耗尽后切
    segs6 = []
    cap6 = VoiceCapture(threshold=2000, silence_end_ms=200, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w, s: segs6.append((d, w)))
    cap6.feed(bytes(tone[:9600]))
    cap6.force_finalize(defer_ms=600)            # 600ms 延迟收尾（防断续）
    with mock.patch("net_control.time.time", return_value=cap6.last_feed_at + 0.2):
        cap6.pump()                              # 延迟窗口内（0.2s<0.6s）：不切
    check("PTT 延迟窗口内不切段", len(segs6) == 0)
    with mock.patch("net_control.time.time", return_value=cap6.last_feed_at + 1.0):
        cap6.pump()                              # 1s：延迟耗尽 + 静音 0.4s>0.2s → 切
    check("延迟耗尽后切段", len(segs6) == 1,
          f"segs6={[(round(d,2), w) for d, w in segs6]}")

    # 信令整段（_ptt_bound）：talking=true 起讲的段，说话中停顿不切段，
    # 等 PTT 抬起（force_finalize）才作为完整一段切出（对讲机半双工边界）
    segs7 = []
    cap7 = VoiceCapture(threshold=2000, silence_end_ms=300, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w, s: segs7.append((d, w)))
    cap7.mark_ptt_bound()                        # UserTalking talking=true
    cap7.feed(bytes(tone[:9600]))                # 200ms 语音（起讲，继承 ptt_bound）
    with mock.patch("net_control.time.time", return_value=cap7.last_feed_at + 0.5):
        cap7.pump()                              # 静音 0.5s > silence_end 300ms，
                                                 # 但信令约束 → 不切段（停顿≠说完）
    check("信令整段：静音停顿不切段", len(segs7) == 0,
          f"segs7={[(round(d,2), w) for d, w in segs7]}")
    cap7.feed(bytes(tone[:9600]))                # 停顿后继续说（仍同一段）
    cap7.force_finalize(defer_ms=0)              # PTT 抬起 → 完整段切出
    check("信令整段：PTT 抬起才切", len(segs7) == 1 and 0.18 <= segs7[0][0] <= 0.22,
          f"segs7={[(round(d,2), w) for d, w in segs7]}")

    # 信令整段兜底：对方说完但 talking=false 丢失 → 静音超 2×silence_end 强制切
    segs8 = []
    cap8 = VoiceCapture(threshold=2000, silence_end_ms=300, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w, s: segs8.append((d, w)))
    cap8.mark_ptt_bound()
    cap8.feed(bytes(tone[:9600]))
    with mock.patch("net_control.time.time", return_value=cap8.last_feed_at + 0.7):
        cap8.pump()                              # 静音 0.7s ≥ 2×300ms → 兜底切段
    check("信令整段：静音超2倍兜底切段", len(segs8) == 1,
          f"segs8={[(round(d,2), w) for d, w in segs8]}")

    # 无信令约束（VAD 兜底）：照旧静音切段（平台不下发信令时的降级路径）
    segs9 = []
    cap9 = VoiceCapture(threshold=2000, silence_end_ms=300, min_segment_ms=50,
                        save_dir=None, on_segment=lambda p, d, w, s: segs9.append((d, w)))
    cap9.feed(bytes(tone[:9600]))                # 无 mark_ptt_bound → 普通 VAD 段
    with mock.patch("net_control.time.time", return_value=cap9.last_feed_at + 0.5):
        cap9.pump()                              # 静音 0.5s > 300ms → 切段
    check("无信令约束走 VAD 切段", len(segs9) == 1,
          f"segs9={[(round(d,2), w) for d, w in segs9]}")


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
    # 纯解释法（用户实测场景：回答呼号只用字母解释法）
    r = decode_callsign("Bravo India Nine Golf Charlie Whiskey")
    check("纯解释法提取", r["callsign"] == "BI9GCW" and r["score"] >= 90, f"{r}")
    r = decode_callsign("BravoIndiaNineGolfCharlieWhiskey")   # ASR 无空格连写
    check("解释法无空格连写", r["callsign"] == "BI9GCW", f"{r}")
    r = decode_callsign("Bravo India Nine GolfCharlie Whiskey")  # 部分连写
    check("解释法部分连写", r["callsign"] == "BI9GCW", f"{r}")
    r = decode_callsign("please repeat your callsign")
    check("普通英文句不产生呼号", r["callsign"] is None, f"{r}")
    # 重复呼号去重（实测 22:02 "B R九A B B R九A B" 被最左最长误取 BR9ABB）
    r = decode_callsign("B R九A B B R九A B。")
    check("重复呼号取单遍", r["callsign"] == "BR9AB", f"{r}")
    r = decode_callsign("B R九A B B R九A B B R九A B。")
    check("重复呼号三遍取单遍", r["callsign"] == "BR9AB", f"{r}")
    # Beta 词表（实测 22:03 "Bravo Romeo Nine Alpha Beta"→BR9AB 非 BR9ABE）
    r = decode_callsign("错了错了，这里是B二九A B，Bravo Romeo Nine Alpha Beta。")
    check("Beta 映射为B", r["callsign"] == "BR9AB", f"{r}")


def test_asr_body():
    print("[ASR 请求体（离线构造）]")
    c = AsrClient(api_key="sk-test")
    body = c._build_body(b"\x00\x00\x00\x00", context_words=["BRAVO", "BH3XX"])
    check("OpenAI 兼容结构", body["model"] == "qwen3-asr-flash"
          and body["messages"][0]["role"] == "system" and not body["stream"])
    check("itn 默认开", body["asr_options"]["enable_itn"] is True)
    # 生产默认：system.content 数组格式（实测 2026-09-18 唯一被服务端接受的格式）
    sysmsg = body["messages"][0]["content"]
    check("默认 system 为数组",
          isinstance(sysmsg, list) and sysmsg[0]["type"] == "text")
    sys_text = sysmsg[0]["text"]
    check("词表进 System", "BH3XX" in sys_text and "BRAVO" in sys_text)
    # system 词表格式变体（--asr-probe 探测用）
    b_str = c._build_body(b"\x00\x00\x00\x00", context_words=["BRAVO"], sys_style="str")
    check("str 变体为字符串", isinstance(b_str["messages"][0]["content"], str))
    b_bare = c._build_body(b"\x00\x00\x00\x00", context_words=["BRAVO"], sys_style="bare")
    check("bare 变体纯词表", b_bare["messages"][0]["content"] == "BRAVO")
    b_none = c._build_body(b"\x00\x00\x00\x00", context_words=["BRAVO"], sys_style="none")
    check("none 变体无 system", all(m["role"] != "system" for m in b_none["messages"]))
    b_noopts = c._build_body(b"\x00\x00\x00\x00", context_words=["BRAVO"],
                             with_asr_opts=False, sys_style="list")
    check("no-opts 变体无 asr_options", "asr_options" not in b_noopts)


def test_llm_gate_and_missing_fields():
    print("[LLM 触发预检 + 结构化追问]")
    ns = net_control.NetControlSession.__new__(net_control.NetControlSession)
    ns._fields = {}
    ns._asked_fields = {}
    ns._current_call = None
    check("垃圾文本不触发 LLM", ns._looks_like_report("不让我就去死") is False)
    check("英文解释法触发", ns._looks_like_report("Bravo Golf Nine Alpha") is True)
    check("中文呼号触发", ns._looks_like_report("这里是BG九BFZ") is True)
    check("空文本不触发", ns._looks_like_report("") is False)
    ns._fields["BG9ABC"] = {"qth": "团结路", "device": "全胜UV二"}
    missing = ns._missing_fields("BG9ABC")
    check("缺失字段计算", missing == ["signal", "antenna", "power"],
          f"missing={missing}")
    ns._fields["BG9XYZ"] = {"qth": "无", "device": "链路", "antenna": "没有天线",
                            "power": "无", "signal": "59"}
    check("无/没有视为已填", ns._missing_fields("BG9XYZ") == [])


def test_vocab_echo_reject():
    print("[ASR 词表回显校验（服务端把 system 当输入转写）]")
    from unittest import mock
    import direct_announce as _da
    echo = ('{"choices":[{"message":{"content":"业余无线电点名应答转写。'
            '以下为背景实体词表，请优先正确识别：alpha、bravo。"}}]}')
    ok = '{"choices":[{"message":{"content":"这里是BH3XX"}}]}'
    client = AsrClient(api_key="sk-test")

    def fake_req(body, retry_conn=True):
        has_sys = any(m.get("role") == "system" for m in body.get("messages", []))
        return (200, (echo.encode() if has_sys else ok.encode()))

    client._request = fake_req
    with mock.patch.dict(_da.CFG,
                         {"net_control": {"asr": {"sys_style": "list"}}}):
        text = client.transcribe(b"\x00" * 16000, context_words=["BRAVO", "BH3XX"])
    check("词表回显被拒并降级重试", text == "这里是BH3XX", f"text={text}")
    # 默认 none（不带 system）正常返回
    client2 = AsrClient(api_key="sk-test")
    client2._request = lambda body, retry_conn=True: (200, ok.encode())
    check("正常结果不受影响",
          client2.transcribe(b"\x00" * 16000) == "这里是BH3XX")


def test_conn_reuse():
    print("[ASR 长连接复用（keep-alive）]")
    from unittest import mock
    client = AsrClient(api_key="sk-test")
    calls = []
    fake_resp = mock.Mock()
    fake_resp.status = 200
    fake_resp.read.return_value = b'{"choices":[{"message":{"content":"OK"}}]}'
    fake_conn = mock.Mock()
    fake_conn.getresponse.return_value = fake_resp
    fake_conn.request.side_effect = lambda m, p, body, headers: calls.append(p)
    client._conn = fake_conn
    client._conn_host, client._conn_port = "dashscope.aliyuncs.com", 443
    s1, d1 = client._request({"a": 1})
    s2, d2 = client._request({"a": 2})
    check("同一连接对象复用", client._conn is fake_conn and len(calls) == 2,
          f"calls={len(calls)}")
    check("请求路径正确", all(p.endswith("/chat/completions") for p in calls), str(calls))
    check("响应读回", s2 == 200 and b"OK" in d2, f"s2={s2}")
    # 长连接失效（RemoteDisconnected）→ 重建（mock 新连接）后重试成功
    fake_conn2 = mock.Mock()
    fake_conn2.getresponse.side_effect = [
        http.client.RemoteDisconnected("server closed")]
    client._conn = fake_conn2
    client._conn_host, client._conn_port = "dashscope.aliyuncs.com", 443
    with mock.patch("net_control.http.client.HTTPSConnection", return_value=fake_conn):
        s3, d3 = client._request({})
    check("连接失效重建重试", s3 == 200 and b"OK" in d3, f"s3={s3}")


def test_config_defaults():
    print("[配置回退（无 config.json 时全默认）]")
    check("net_control 默认关闭", net_control.nc_cfg("enabled", default=False) is False)
    check("默认阈值", net_control.nc_cfg("vad_threshold", default=800) == 800)
    check("默认解释词可用", net_control.PHONETIC_ITU["bravo"] == "B")


def test_retry_reset():
    print("[重试额度恢复（成功抄收后不再哑巴）]")
    sess = net_control.NetControlSession(link=None)
    sess._teardown_dir = None
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda text: spoken.append(text)

    # 段1：无当前友台时友台直接报信息但无呼号 → 引导补报呼号（额度 1→0）
    sess._asr_text = lambda pcm: "我的QTH是在咸阳市，设备即时通，天线原机天线，五瓦功率发射"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("无呼号段触发引导播报", len(spoken) == 1 and "呼号" in spoken[0],
          f"spoken={spoken}")
    check("引导后额度已用尽", sess._retry_left == 0)

    # 段2：下一位友台成功抄收（session=2）→ 额度恢复 + 当前友台上下文建立
    sess._asr_text = lambda pcm: "这里是BH3XX，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("成功抄收", len(spoken) == 2 and "Bravo Hotel Three X-ray X-ray" in spoken[1],
          f"spoken={spoken}")
    check("抄收后额度恢复", sess._retry_left == 1 and not sess._retry_pending,
          f"retry_left={sess._retry_left}")

    # 段3：同 session 友台补充信息（无呼号，正常点名流程）→ 结构化提取并复诵确认
    sess._asr_text = lambda pcm: "我的设备是泉盛K6，天线原机天线，五瓦"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    # 合并确认：补充段只累积不立即播（避免连播打断），force flush 后统一确认一次
    check("同台补充信息归入（不立即播）", len(spoken) == 2
          and sess._current_entry[4] and "泉盛K6" in sess._current_entry[4],
          f"spoken={spoken} entry={sess._current_entry}")
    sess._flush_pending_report(force=True)
    check("合并确认含全部字段", len(spoken) == 3 and "信息已记录" in spoken[2]
          and "设备 泉盛K6" in spoken[2] and "天线 原机天线" in spoken[2]
          and "功率 5 瓦" in spoken[2],
          f"spoken={spoken}")
    check("补充段不消耗额度", sess._retry_left == 1, f"retry_left={sess._retry_left}")
    # 合并确认后不再立即追问缺失字段（22:06:16→22:06:34 主控连播两句抢话）；
    # 缺失项等友台确认（"正确"）后再追问
    check("合并确认后不立即追问", len(spoken) == 3,
          f"spoken={spoken}")

    # 段3b：友台确认"正确" → 缺失字段（QTH）此时追问
    sess._asr_text = lambda pcm: "正确"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("确认后追问缺失字段", len(spoken) == 4 and "请再补充您的QTH" in spoken[3],
          f"spoken={spoken}")

    # 段4：新 session 无呼号（新友台没报呼号）→ 引导报呼号
    sess._asr_text = lambda pcm: "这里是，我的设备是泉盛K6"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=3)
    check("新友台无呼号仍引导", len(spoken) == 5 and "呼号" in spoken[4],
          f"spoken={spoken}")

    # 段5：同新 session 连续无呼号（额度尽）→ 静默但计数（用不同文本，
    # 避免与段4 相同文本被回波过滤判为回波）
    sess._asr_text = lambda pcm: "信号很好"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=3)
    check("额度用尽后静默", len(spoken) == 5 and sess._failed == 1,
          f"spoken={spoken} failed={sess._failed}")


def test_info_followup():
    print("[补充信息归入当前友台（点名流程无呼号段）]")
    sess = net_control.NetControlSession(link=None)
    sess._teardown_dir = None
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda text: spoken.append(text)

    # 友台1 报呼号（session=1）
    sess._asr_text = lambda pcm: "这里是BH3XX，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    # 友台1 补充 QTH/设备（同 session、无呼号）→ 归入 BH3XX 并复诵
    sess._asr_text = lambda pcm: "我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("补充段归入友台1", sess._current_call == "BH3XX"
          and "QTH" in (sess._current_entry[4] or ""),
          f"call={sess._current_call} entry={sess._current_entry}")
    # 合并确认：补充段只累积不立即播，force flush 后统一确认一次
    check("补充段不立即播报", len(spoken) == 1, f"spoken={spoken}")
    sess._flush_pending_report(force=True)
    check("补充段复诵信息", len(spoken) == 2 and "信息已记录" in spoken[1]
          and "QTH 咸阳市渭城区" in spoken[1] and "设备 泉盛K6" in spoken[1]
          and "功率 5 瓦" in spoken[1],
          f"spoken={spoken}")
    # 空段（0.5s 环境声）→ 静默忽略，不播报不归入
    n = len(spoken)
    sess._asr_text = lambda pcm: ""
    sess._process_segment(b"\x00" * 32000, 0.5, None, session=1)
    sess._asr_text = lambda pcm: "。"
    sess._process_segment(b"\x00" * 32000, 0.5, None, session=1)
    sess._asr_text = lambda pcm: "嗯"
    sess._process_segment(b"\x00" * 32000, 0.5, None, session=1)
    check("空段/语气词不播报不归入", len(spoken) == n
          and sess._current_entry[4] == "我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦",
          f"spoken={spoken} entry={sess._current_entry}")
    # 短确认语 → 确认收尾，不归入
    sess._asr_text = lambda pcm: "正确"
    sess._process_segment(b"\x00" * 32000, 0.5, None, session=1)
    check("确认语收尾", len(spoken) == n + 1 and "感谢确认" in spoken[-1],
          f"spoken={spoken}")
    check("确认语不归入", sess._current_entry[4] == "我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦",
          f"entry={sess._current_entry}")
    # 纠正语（无新呼号）→ 请重报
    sess._asr_text = lambda pcm: "不对，我再说一遍，QTH在西安"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("纠正语请重报", len(spoken) == n + 2 and "重" in spoken[-1],
          f"spoken={spoken}")
    # "不正确"纠正语（实测 19:48 漏判场景）→ 走纠正分支，不复诵
    sess._asr_text = lambda pcm: "不正确，不正确，请重复抄收"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("不正确走纠正分支", len(spoken) == n + 3 and "重" in spoken[-1],
          f"spoken={spoken}")
    # 无结构化字段的询问语（实测 19:47 被复诵成废话）→ 引导补报信息
    sess._asr_text = lambda pcm: "主控是否抄收？"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("询问抄收状态引导补报", len(spoken) == n + 4 and "呼号已记录" in spoken[-1],
          f"spoken={spoken}")
    # 无实义噪音（"那主播"）→ 静默忽略，不播报不归入
    sess._asr_text = lambda pcm: "那主播"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("无实义噪音静默", len(spoken) == n + 4,
          f"spoken={spoken}")
    check("噪音不归入", sess._current_entry[4] == "我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦",
          f"entry={sess._current_entry}")
    # 友台2 报呼号（session=2）→ 抄收并替换当前友台
    sess._asr_text = lambda pcm: "这里是BG9ABC，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("新友台替换上下文", sess._current_call == "BG9ABC" and sess._current_session == 2
          and [c for c, *_ in sess._checked_in] == ["BH3XX", "BG9ABC"],
          f"call={sess._current_call}")
    # 友台2 补充（session=2、无呼号）→ 归入友台2 的条目
    sess._asr_text = lambda pcm: "我的QTH在咸阳市"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("补充段归入友台2", sess._checked_in[1][4] == "我的QTH在咸阳市",
          f"entry={sess._checked_in[1]}")
    check("友台1 条目未被污染",
          sess._checked_in[0][4] == "我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦",
          f"entry={sess._checked_in[0]}")
    # 结构化字段已在点名中永久化（CSV 导出数据源）
    check("结构化字段记录", sess._fields.get("BH3XX", {}).get("qth") == "咸阳市渭城区"
          and sess._fields.get("BH3XX", {}).get("device") == "泉盛K6"
          and sess._fields.get("BH3XX", {}).get("power") == "5 瓦"
          and sess._fields.get("BG9ABC", {}).get("qth") == "咸阳市",
          f"fields={sess._fields}")
    # 设备新句式："用的是手机/使用的是手机"（实测 22:40 丢失"手机"导致反复追问）
    sess._asr_text = lambda pcm: "天线是没有天线，用的是手机"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("用的是手机提取设备", sess._fields.get("BG9ABC", {}).get("device") == "手机"
          and "手机" in (sess._current_entry[4] or ""),
          f"fields={sess._fields} entry={sess._current_entry}")
    # 重复补充同值字段（"没有天线"已记录过）→ 静默，不重复复诵
    n3 = len(spoken)
    sess._asr_text = lambda pcm: "天线是没有天线"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("重复补充静默不播报", len(spoken) == n3, f"spoken={spoken}")
    # "不是已经说过了吗"反问 → 不判纠正（实测 22:43 误播"抄收有误"）
    n4 = len(spoken)
    sess._asr_text = lambda pcm: "我的设备，我的车贴是刚才不是已经说过了吗？你没有记住吗"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    check("反问不是不判纠正", len(spoken) == n4 and "抄收有误" not in "".join(spoken),
          f"spoken={spoken}")


def test_export_csv():
    print("[点名记录 CSV 导出]")
    sess = net_control.NetControlSession(link=None)
    sess._teardown_dir = None
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    sess._speak = lambda text: None
    sess._started_at = datetime.datetime(2026, 9, 18, 19, 45, 0)
    sess._asr_text = lambda pcm: "这里是BH3XX，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    sess._asr_text = lambda pcm: "我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    sess._asr_text = lambda pcm: "这里是BG9ABC"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=2)
    with tempfile.TemporaryDirectory() as td:
        out = sess._export_csv(path=str(Path(td) / "点名记录_20260918_194500.csv"))
        check("导出返回路径", out is not None and out.endswith("点名记录_20260918_194500.csv"),
              f"out={out}")
        rows = list(csv.reader(open(out, encoding="utf-8-sig")))
        check("表头正确", rows[0] == ["序号", "呼号", "信号报告", "QTH", "设备",
                                     "天线", "功率", "抄收时间", "原始转录", "补充原文"],
              f"header={rows[0]}")
        check("数据行数", len(rows) == 3, f"rows={rows}")
        check("友台1 结构化列", rows[1][1] == "BH3XX" and rows[1][2] == "59"
              and rows[1][3] == "咸阳市渭城区" and rows[1][4] == "泉盛K6"
              and rows[1][5] == "原机天线" and rows[1][6] == "5 瓦",
              f"row={rows[1]}")
        check("友台2 行", rows[2][1] == "BG9ABC" and rows[2][2] == "",
              f"row={rows[2]}")


def test_report_clean():
    print("[补充信息清洗 clean_report_text]")
    c = net_control.clean_report_text
    check("Q T 归一", c("我的Q T在咸阳市") == "我的QTH在咸阳市", c("我的Q T在咸阳市"))
    check("Q T H 归一", "QTH" in c("Q T H 咸阳"))
    check("W 转瓦", c("5W功率") == "5 瓦功率", c("5W功率"))
    check("纯标点过滤", c("。") == "" and c("…") == "")
    check("语气词过滤", c("嗯") == "" and c("嗯嗯") == "")
    check("保留实质", c("对，我的QTH在咸阳") == "对，我的QTH在咸阳")


def test_report_fields():
    print("[结构化字段提取 extract_report_fields]")
    f = net_control.extract_report_fields
    r = f("我的QTH在咸阳市渭城区，设备泉盛K6，原机天线，五瓦功率发射")
    d = dict(r)
    check("QTH/设备/天线/功率全提取", d.get("qth") == "咸阳市渭城区"
          and d.get("device") == "泉盛K6" and d.get("antenna") == "原机天线"
          and d.get("power") == "5 瓦", f"{r}")
    r = f("Q T H 咸阳，设备是即时通，天线原机天线，5W")
    d = dict(r)
    check("Q T 展开/阿拉伯功率", d.get("qth") == "咸阳" and d.get("device") == "即时通"
          and d.get("power") == "5 瓦", f"{r}")
    r = f("信号五九")
    check("信号报告提取", dict(r).get("signal") == "59", f"{r}")
    r = f("主控是否抄收")
    check("询问语无字段", r == [], f"{r}")
    r = f("那主播")
    check("噪音无字段", r == [], f"{r}")


def test_cosyvoice_tts():
    print("[CosyVoice TTS 配置与引擎选择]")
    import direct_announce as da
    saved = dict(da.CFG)
    try:
        da.CFG.clear()
        da.CFG.update({"asr": {"api_key": ""},
                       "tts": {"cosyvoice_voice": "sound_x"}})
        try:
            net_control._cosyvoice_synth("测试", Path("/tmp/nc_cv_test.mp3"), timeout=1)
            check("缺 key 应报错", False, "未报错")
        except RuntimeError as e:
            check("缺 key 报错", "asr.api_key" in str(e), str(e))

        da.CFG.clear()
        da.CFG.update({"asr": {"api_key": "sk-t"}, "tts": {}})
        try:
            net_control._cosyvoice_synth("测试", Path("/tmp/nc_cv_test.mp3"), timeout=1)
            check("缺音色应报错", False, "未报错")
        except RuntimeError as e:
            check("缺音色报错", "cosyvoice_voice" in str(e), str(e))

        # 合成失败（缺 key 等）→ synth_text 重试后返回空串，不崩溃、不阻塞点名
        da.CFG.clear()
        da.CFG.update({"asr": {"api_key": ""},
                       "tts": {"engine": "cosyvoice"},
                       "net_control": {"tts_cache_dir": "/tmp/nc_cv_cache"}})
        out = net_control.synth_text("测试播报", timeout_inner=1, timeout_join=2,
                                     max_retries=1, retry_delay=0)
        check("缺配置合成返回空", out == "", f"out={out!r}")
        # engine=edge 分支（缺 key 时走 edge 也会失败返回空，不抛异常）
        da.CFG.clear()
        da.CFG.update({"asr": {"api_key": ""},
                       "tts": {"engine": "edge"},
                       "net_control": {"tts_cache_dir": "/tmp/nc_cv_cache"}})
        out = net_control.synth_text("测试播报", timeout_inner=1, timeout_join=2,
                                     max_retries=1, retry_delay=0)
        check("edge 分支失败返回空", out == "", f"out={out!r}")
    finally:
        da.CFG.clear()
        da.CFG.update(saved)


def test_wait_channel_idle():
    print("[先听后说：抢麦前等待信道空闲]")
    sess = net_control.NetControlSession(link=None)
    sess._teardown_dir = None
    sess._capture = None
    # 无活跃讲话 → 立即可发射
    sess._talking_sessions = set()
    check("空闲立即放行", sess._wait_channel_idle() is True)
    # 信令层有人在讲 → 等待（模拟 0.3s 后对方讲完）
    sess._talking_sessions = {999}
    t0 = time.time()
    def _release_after(t):
        pass
    # 起线程模拟对方 0.3s 后讲完
    import threading as _th
    def _clear():
        _th.Event().wait(0.3)
        sess._talking_sessions.discard(999)
    th = _th.Thread(target=_clear)
    th.start()
    ok = sess._wait_channel_idle()
    th.join()
    check("占用中等待放行", ok is True and 0.2 <= time.time() - t0 <= 3.0,
          f"ok={ok} dt={time.time() - t0:.2f}")
    # 等待超时（tx_wait_timeout=1s）仍发射，不卡死
    sess._talking_sessions = {888}
    import direct_announce
    old = direct_announce.CFG.get("net_control", {}).get("tx_wait_timeout")
    direct_announce.CFG.setdefault("net_control", {})["tx_wait_timeout"] = 1
    t0 = time.time()
    ok = sess._wait_channel_idle()
    dt = time.time() - t0
    if old is None:
        direct_announce.CFG["net_control"].pop("tx_wait_timeout", None)
    else:
        direct_announce.CFG["net_control"]["tx_wait_timeout"] = old
    check("超时兜底发射", ok is True and dt < 3.0,
          f"ok={ok} dt={dt:.2f}")


def test_echo_filter():
    print("[中继台回波过滤 _is_echo（简化：仅短段+空文本）]")
    sess = net_control.NetControlSession(link=None)
    sess._speak = lambda t: None
    check("未发射过非回波", not sess._is_echo("", 0.6))
    sess._last_tx_end = time.time()
    check("窗口内短空段→回波", sess._is_echo("", 0.5), f"last={sess._last_tx_end}")
    check("窗口内短段有文字→非回波（回波无长文字，实测结论）",
          not sess._is_echo("抄收你的信号", 0.8))
    check("窗口内短段有呼号→非回波",
          not sess._is_echo("这里是BH3XX信号59", 0.8))
    check("段太长→非回波", not sess._is_echo("", 3.0))
    sess._last_tx_end = time.time() - 10
    check("超时窗外→非回波", not sess._is_echo("", 0.5))
    # 端到端：放麦后紧接的空段不触发"请重复呼号"、不耗额度
    sess._last_tx_end = time.time()
    sess._asr_text = lambda p: ""
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._process_segment(b"\x00" * 32000, 0.6, None, session=1)
    check("回波段静默不播报", spoken == [], f"spoken={spoken}")
    check("额度未消耗", sess._retry_left == 1, f"left={sess._retry_left}")


def test_wait_idle_consumes_queue():
    print("[等待发射期间继续识别]")
    sess = net_control.NetControlSession(link=None)
    sess._asr_text = lambda p: "这里是BG9ABC，信号59"
    sess._seg_queue.put((b"\x00" * 32000, 1.0, None, 3))
    sess._speak = lambda t: None
    calls = {"n": 0}

    def fake_speaking():
        calls["n"] += 1
        return calls["n"] < 3          # 前两次有人讲，第三次空闲

    sess._someone_speaking = fake_speaking
    sess._wait_channel_idle()
    check("等待期间消费队列并抄收",
          [c for c, *_ in sess._checked_in] == ["BG9ABC"],
          f"checked={sess._checked_in}")
    check("当前友台已切换", sess._current_call == "BG9ABC", f"call={sess._current_call}")


def test_mixed_callsign_decode():
    print("[中英混合呼号解码（BJ九EFU / BI九DGI）]")
    r = net_control.decode_callsign("主控主控，这里是BJ九EFU，BJ九EFU，能否超收")
    check("BJ九EFU 解出 BJ9EFU", r["callsign"] == "BJ9EFU" and r["score"] >= 60,
          f"{r}")
    r = net_control.decode_callsign(
        "总控总控，这里是BI九DGI，这里是BI九DGI，Bravo India Nine，这是塔克，India，是否可以操作")
    check("BI九DGI 解出 BI9DGI（不被 Bravo…India 干扰）",
          r["callsign"] == "BI9DGI" and r["score"] >= 60, f"{r}")


def test_ctrl_call_filter():
    print("[主控自身呼号过滤]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._asr_text = lambda p: "这里是B I九B Z W，2.03 10"
    sess._process_segment(b"\x00" * 32000, 6.7, None, session=1)
    check("主控呼号不抄收不播报", spoken == [] and sess._checked_in == [],
          f"spoken={spoken} checked={sess._checked_in}")


def test_same_session_new_call():
    print("[同 session 友台补报新呼号不被补充信息吞]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._asr_text = lambda p: "这里是BH3XX"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    sess._asr_text = lambda p: "我的QTH在咸阳市"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    # 同 session 新友台补报完整呼号 → 不被补充信息吞；但呼号完全不相似
    # （BJ9EFU vs BH3XX，编辑距离大）→ 判定为另一友台插队排队，不顶当前友台
    sess._asr_text = lambda p: "这里是BJ九EFU，能否超收"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("不相似呼号排队不替换",
          [c for c, *_ in sess._checked_in] == ["BH3XX", "BJ9EFU"]
          and sess._current_call == "BH3XX"
          and any(w["call"] == "BJ9EFU" for w in sess._waiting),
          f"checked={sess._checked_in} cur={sess._current_call} "
          f"waiting={sess._waiting}")


def test_echo_other_speaker():
    print("[对方讲完后的回波过滤]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    sess._asr_text = lambda p: "主控主控，这里是BH3XX"
    sess._process_segment(b"\x00" * 32000, 2.4, None, session=7)
    # 紧接的短段且文本与上一段重合 → 判回波
    sess._asr_text = lambda p: "这里是BH3XX"
    sess._process_segment(b"\x00" * 32000, 0.6, None, session=7)
    check("对方回波被过滤", [c for c, *_ in sess._checked_in] == ["BH3XX"],
          f"checked={sess._checked_in}")
    # 紧接但文本不同（真实抢答）→ 不误杀
    sess._asr_text = lambda p: "这里是BG9ABC"
    sess._process_segment(b"\x00" * 32000, 0.8, None, session=7)
    check("不同文本不误杀", [c for c, *_ in sess._checked_in] == ["BH3XX", "BG9ABC"],
          f"checked={sess._checked_in}")


def test_llm_fallback():
    print("[LLM 兜底修复（llm.enabled=true）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    sess._speak = lambda t: None

    class FakeLLM:
        calls = 0
        def extract(self, text):
            type(self).calls += 1
            return {"callsign": "BG9BFZ", "signal": "59", "copied": True, "note": ""}

    sess._llm = FakeLLM()          # 注入可用 LLM（跳过 enabled/api_key 检查）
    # decode 解不出的文本（B9BFZ 缺 G，格式不合法）→ 低置信度 → LLM 兜底
    sess._asr_text = lambda p: "主控主控，这里是B九B F Z"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=9)
    check("LLM 被调用", FakeLLM.calls == 1, f"calls={FakeLLM.calls}")
    check("LLM 修复后抄收", [c for c, *_ in sess._checked_in] == ["BG9BFZ"],
          f"checked={sess._checked_in}")
    check("信号由 LLM 补提", sess._checked_in[0][1] == "59",
          f"sig={sess._checked_in[0][1]}")
    # 未启用 LLM（默认）→ 不调用、不阻塞原流程
    sess2 = net_control.NetControlSession(link=None)
    sess2._net_ctx = {"ctrl_call": "BI9BZW"}
    sess2._speak = lambda t: None
    sess2._asr_text = lambda p: "主控主控，这里是B九B F Z"
    sess2._process_segment(b"\x00" * 32000, 1.0, None, session=9)
    check("未启用时不调用不抄收", sess2._checked_in == [] and sess2._llm is False,
          f"checked={sess2._checked_in}")


def test_vad_stuck_release():
    print("[VAD 采集卡死复位]")
    sess = net_control.NetControlSession(link=None)
    sess._speak = lambda t: None
    # 模拟：正在采集但 3s 无新帧（对方讲完、链路静默）→ 判空闲并复位
    sess._capture = net_control.VoiceCapture(on_segment=lambda *a: None)
    sess._capture._speaking = True
    sess._capture.last_feed_at = time.time() - 3
    check("VAD idle 超时判空闲", not sess._someone_speaking())
    check("VAD 已复位", sess._capture._speaking is False)
    # 最近有帧 → 判占用
    sess._capture._speaking = True
    sess._capture.last_feed_at = time.time()
    check("VAD 活跃判占用", sess._someone_speaking())


def test_llm_call_cap():
    print("[LLM 兜底限次（每轮≤3 次，防超时拖死）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    sess._speak = lambda t: None

    class FakeLLM:
        calls = 0
        def extract(self, text):
            type(self).calls += 1
            return {"callsign": "BG9BFZ", "signal": "", "copied": True, "note": ""}

    sess._llm = FakeLLM()
    for i in range(4):
        sess._asr_text = (lambda p, i=i: f"主控主控，这里是B九B F Z 第{i}遍")
        sess._process_segment(b"\x00" * 32000, 1.0, None, session=100 + i)
    check("LLM 只调 3 次", FakeLLM.calls == 3, f"calls={FakeLLM.calls}")


def test_tts_synth_drains_queue():
    print("[TTS 合成期间继续识别（不阻塞队列）]")
    import direct_announce as _da
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    orig_synth = net_control.synth_text
    orig_build = _da.build_audio

    def slow_synth(text):
        time.sleep(1.2)                    # 模拟慢 TTS（3~20s 的真实场景）
        return "/tmp/_test_slow.mp3"

    net_control.synth_text = slow_synth
    _da.build_audio = lambda *a: (_ for _ in ()).throw(RuntimeError("测试跳过播放"))
    try:
        # 队列里积压一位友台的段；合成期间应被消费识别
        sess._asr_text = lambda p: "这里是BG9ABC"
        sess._seg_queue.put((b"\x00" * 32000, 1.0, None, 42))
        sess._speak("测试播报文本")
        check("合成期间消费队列并抄收",
              [c for c, *_ in sess._checked_in] == ["BG9ABC"],
              f"checked={sess._checked_in}")
    finally:
        net_control.synth_text = orig_synth
        _da.build_audio = orig_build


def test_speech_supersede_waits():
    print("[接话只播最新：新话术生成后旧话术让位]")
    sess = net_control.NetControlSession(link=None)
    sess._capture_pump = lambda: None
    sess._drain_queue = lambda depth=0: 0
    sess._someone_speaking = lambda: True   # mock 信道一直被占用
    # 1) 无新话术：等到超时仍返回 True（原行为不变）
    sess._speech_seq = 0
    t0 = time.time()
    ok = sess._wait_channel_idle(drain=False, newer_than=0)
    check("无新话术等满超时仍发射", ok is True and time.time() - t0 >= 1.5,
          f"ok={ok} t={time.time()-t0:.2f}")
    # 2) 等待期间新话术生成（序号前进）→ 立即放弃本句返回 False
    sess._speech_seq = 1
    t0 = time.time()
    ok = sess._wait_channel_idle(drain=False, newer_than=0)
    check("有新话术立即放弃旧话术", ok is False and time.time() - t0 < 1.0,
          f"ok={ok} t={time.time()-t0:.2f}")
    # 3) 信道空闲：无论序号如何立即返回 True
    sess._someone_speaking = lambda: False
    sess._speech_seq = 5
    check("信道空闲立即发射", sess._wait_channel_idle(drain=False, newer_than=5) is True)


def test_interloper_queued():
    print("[插队：当前友台进行中，新呼号静默记录+排队，收尾后轮候]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    # A 正常抄收（进入进行中状态）
    sess._asr_text = lambda p: "这里是BG9ABC"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("A 抄收且进行中", sess._current_active and
          [c for c, *_ in sess._checked_in] == ["BG9ABC"], f"checked={sess._checked_in}")
    # B 插队：高分新呼号 → 不播报、入册、排队
    n0 = len(spoken)
    sess._asr_text = lambda p: "这里是BG9XYZ"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=8)
    check("插队静默不播报", len(spoken) == n0, f"spoken={spoken}")
    check("插队已入册", [c for c, *_ in sess._checked_in] == ["BG9ABC", "BG9XYZ"],
          f"checked={sess._checked_in}")
    check("插队已排队", [w["call"] for w in sess._waiting] == ["BG9XYZ"],
          f"waiting={sess._waiting}")
    # 重复插队忽略（不重复入册/排队）
    sess._asr_text = lambda p: "BG9XYZ再次呼叫"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=9)
    check("重复插队忽略", len(sess._waiting) == 1 and len(sess._checked_in) == 2,
          f"waiting={sess._waiting} checked={sess._checked_in}")
    # A 补充全部字段后确认 → 收尾 → B 自动轮候正式抄收（播 ack）
    sess._fields["BG9ABC"] = {"signal": "59", "qth": "咸阳", "device": "手机",
                              "antenna": "无", "power": "5瓦"}
    sess._asr_text = lambda p: "完全正确"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("A 确认后轮到 B 正式抄收",
          [c for c, *_ in sess._checked_in] == ["BG9ABC", "BG9XYZ"]
          and not sess._waiting and sess._current_call == "BG9XYZ",
          f"checked={sess._checked_in} waiting={sess._waiting} cur={sess._current_call}")
    check("B 已播确认", any("X-ray Yankee Zulu" in s and "抄收" in s for s in spoken)
          and sess._current_call == "BG9XYZ",
          f"spoken={spoken}")


def test_same_session_replace():
    print("[同 session 重报不同呼号 = 识别修正替换（如 ASR 把 BFZ 听成 BLZ 后重报）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    # 第一次识别错误：BLZ（session=7）
    sess._asr_text = lambda p: "主控主控，这里是BG九BLZ请求参加测试点名"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("错误呼号先被抄收", sess._current_call == "BG9BLZ", f"cur={sess._current_call}")
    # 同 session 重报正确呼号 BFZ → 替换，CSV 只留 BFZ
    sess._asr_text = lambda p: "主控，我的呼号是B G九B F Z，Bravo Golf Nine Bravo Foxtrot Zulu"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("同session重报替换",
          [c for c, *_ in sess._checked_in] == ["BG9BFZ"]
          and sess._current_call == "BG9BFZ"
          and "BG9BLZ" not in sess._checked_calls,
          f"checked={sess._checked_in} cur={sess._current_call}")
    # 不播报请重复/不排队（走正常抄收确认）
    check("替换后正常抄收确认", any("Foxtrot Zulu" in s and "抄收" in s for s in spoken),
          f"spoken={spoken}")


def test_report_merged_confirm():
    print("[信息合并确认：一句话被 VAD 切成多段 → 只播一次完整确认]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    import direct_announce as _da
    _da.CFG.setdefault("net_control", {})["report_merge_gap_seconds"] = 0.5
    # 友台报呼号（抄收，播 ack）
    sess._asr_text = lambda p: "这里是BG9BFZ"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    n0 = len(spoken)
    # 同一句话被切成 3 段：设备 → 功率 → QTH（间隔 < 合并窗口）
    sess._asr_text = lambda p: "使用设备全胜U二"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("第一段不立即播报", len(spoken) == n0, f"spoken={spoken}")
    sess._asr_text = lambda p: "五瓦"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("第二段仍不播报", len(spoken) == n0, f"spoken={spoken}")
    sess._asr_text = lambda p: "QTH团结路"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("第三段仍不播报", len(spoken) == n0, f"spoken={spoken}")
    # 停稳超过合并窗口 → 播一次合并确认（含全部字段）；缺失字段不再立即追问
    # （等友台确认后追问，实测 22:06:16→22:06:34 连播两句抢话的根因）
    time.sleep(0.8)
    sess._flush_pending_report()
    check("合并确认只播一次且含全部字段",
          len(spoken) == n0 + 1 and "设备 全胜U2" in spoken[n0]
          and "功率 5 瓦" in spoken[n0] and "QTH 团结路" in spoken[n0],
          f"spoken={spoken}")
    # 再次 flush（无新内容）→ 不重复播
    sess._flush_pending_report(force=True)
    check("无新内容不重复确认", len(spoken) == n0 + 1, f"spoken={spoken}")


def test_join_intent_guide():
    print("[报名意图但呼号未解出 → 引导重报（不静默，实测 20:15:15 场景）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    # 友台抄收（session=7）→ 当前友台进行中
    sess._asr_text = lambda p: "这里是BG9BFZ，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    n0 = len(spoken)
    # 同 session 报"这里B九B L Z请求参加点名测试"（呼号漏字母未解出）
    sess._asr_text = lambda p: "主控主控，这里B九B L Z请求参加点名测试"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("报名意图引导重报", len(spoken) == n0 + 1 and "请再报一次您的完整呼号" in spoken[-1],
          f"spoken={spoken}")


def test_checkedin_repeat_feedback():
    print("[已点过呼号再次报到 → 直接反馈'已经抄收过'（不静默）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BG9BFZ，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    # 收尾（确认"正确"）后，再报同呼号（新 session，另一台设备重复上台）
    sess._asr_text = lambda p: "正确"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    n0 = len(spoken)
    sess._asr_text = lambda p: "这里是BG9BFZ请求参加点名"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=8)
    check("已点过直接反馈", len(spoken) == n0 + 1
          and ("已经抄收过" in spoken[-1] or "感谢确认" in spoken[-1]),
          f"spoken={spoken}")


def test_replace_guard():
    print("[已记录信息后同 session 重报不同呼号 → 不替换（保护已确认记录）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    # 抄收 BFZ（session=7），补充信息 → entry[4] 非空
    sess._asr_text = lambda p: "这里是BG9BFZ，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    sess._asr_text = lambda p: "我的QTH在咸阳，设备泉盛K6"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    # 同 session 又报 BLZ（高分不同呼号）→ 不替换、不静默，播"呼号已记录"
    sess._asr_text = lambda p: "这里是BG9BLZ请求参加点名测试"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("已记录信息不替换",
          [c for c, *_ in sess._checked_in] == ["BG9BFZ"]
          and sess._current_call == "BG9BFZ"
          and "呼号已记录" in spoken[-1],
          f"checked={sess._checked_in} cur={sess._current_call} spoken={spoken}")


def test_idle_recall():
    print("[空闲重新呼叫：点名中长时间无应答 → 重播开场呼叫]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW", "repeater_call": "BR9AB",
                     "net_name": "测试点名", "ctrl_phonetic": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    import threading as _th
    sess._stop = _th.Event()
    import direct_announce as _da
    _da.CFG.setdefault("net_control", {})["idle_call_seconds"] = 1
    _da.CFG.setdefault("net_control", {})["max_net_seconds"] = 60
    t = _th.Timer(2.5, sess._stop.set)
    t.start()
    try:
        sess._run_open()
    finally:
        t.cancel()
    check("空闲 1s 后重播呼叫", any("CQ" in s for s in spoken), f"spoken={spoken}")


def test_suspend_resume():
    print("[常驻链路 suspend/resume：临时直连期间暂停保活，防同账号互踢]")
    import direct_announce as _da
    link = _da.PersistentAnnouncer(username="u", password="p")
    check("初始未挂起", not link._suspended and link._backoff == 30.0)
    link.suspend()
    check("suspend 置位", link._suspended is True)
    link._backoff = 600.0          # 模拟退避翻倍
    link.resume()
    check("resume 复位并重置退避",
          not link._suspended and link._backoff == 30.0)


def test_keeper_busy_cede_and_waitfor_onmsg():
    print("[常驻抢麦失败根因：busy 期间 keeper 让位 + wait_for 回执不被 on_msg 劫走]")
    import direct_announce as _da
    import struct as _st

    class _FakeSock:
        def __init__(self):
            self.buf = b""
            self.injected = []
        def settimeout(self, t):
            pass
        def recv(self, n):
            if self.injected:
                self.buf += self.injected.pop(0)
            if not self.buf:
                raise _sok.timeout
            d, self.buf = self.buf[:n], self.buf[n:]
            return d

    def _frame(t, payload):
        return _st.pack(">HI", t, len(payload)) + payload

    # 1) busy 期间 keeper 不 drain（socket 由播报线程独占读）
    link = _da.PersistentAnnouncer(username="u", password="p")
    fake_c = type("FC", (), {"drain_calls": 0, "drain": lambda self, idle_rounds=2: setattr(self, "drain_calls", self.drain_calls + 1) or []})()
    link._sess = type("S", (), {"c": fake_c, "session": 1, "close": lambda self: None})()
    link._busy.set()
    for _ in range(5):
        if link._busy.is_set():
            continue
        link._sess.c.drain()
    check("busy 期间 keeper 不 drain", fake_c.drain_calls == 0,
          f"drain_calls={fake_c.drain_calls}")
    link._busy.clear()

    # 2) wait_for 等待 ApplyMic 回执时，途中下行经 on_msg 转发、回执不被劫走
    import socket as _sok
    applymic_ack = _frame(14, b"\x10\x00\x18\x01")
    talk_sig = _frame(15, b"\x08\xff\xff\xff\xff\x07\x10\x00\x18\x01")
    sock = _FakeSock()
    sock.injected = [talk_sig, applymic_ack]
    c = _da.Client.__new__(_da.Client)
    c.sock = sock
    c.buf = b""
    c.send = lambda t, p: None
    got = []
    t, p = c.wait_for(6, (14,), on_msg=lambda t2, p2: got.append(t2))
    check("回执类型 14 被 wait_for 返回", t == 14, f"t={t}")
    check("途中下行 15 经 on_msg 转发", got == [15], f"got={got}")


def test_power_highpower_and_device_gt12():
    print("[20:49 实测长句：功率档位词+设备型号中文数字（森海科斯G T幺二）]")
    text = ("我QTH是兴平南关西路，设备情况森海科斯G T幺二，原机天线，"
            "高功率，24楼高度发射，主控是否超收？Over")
    fields = dict(net_control.extract_report_fields(text))
    check("QTH 提取", fields.get("qth") == "兴平南关西路", f"fields={fields}")
    check("设备去噪声+型号数字化",
          fields.get("device") == "森海科斯GT12",
          f"fields={fields}")
    check("天线原机天线", fields.get("antenna") == "原机天线", f"fields={fields}")
    check("高功率提取", fields.get("power") == "高功率", f"fields={fields}")


def test_duplicate_report_fields():
    print("[重复抄收但补报缺失字段：20:51:26 '抄你的信号五九' → 应补录而非跳过]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BI9BZY"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("首次抄收", sess._current_call == "BI9BZY"
          and "抄收" in spoken[0] and "Zulu Yankee" in spoken[0],
          f"cur={sess._current_call} spoken={spoken}")
    # 已记 QTH/设备/天线，缺信号 → 友台重复报呼号+信号59
    sess._asr_text = lambda p: "我的QTH是咸阳，设备手机，天线原装天线"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    n0 = len(spoken)
    sess._asr_text = lambda p: "主控主控，这里是B I九B Z Y，抄你的信号五九，是否抄收？Over"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("重复呼号但补报信号59已记录",
          sess._fields.get("BI9BZY", {}).get("signal") == "59"
          and not any("已经抄收过" in s for s in spoken[n0:]),
          f"fields={sess._fields} spoken={spoken[n0:]}")
    # 纯重复（无新字段）→ 当前友台重复报自己呼号：视为确认收尾推进流程，
    # 播"感谢确认"而非"已经抄收过"（实测 22:24:38 友台重报自己呼号让流程
    # 继续，旧话术像指责；本次修复改为友好确认收尾）
    n1 = len(spoken)
    sess._asr_text = lambda p: "这里是BI9BZY"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=1)
    check("纯重复视为确认收尾",
          any("感谢确认" in s for s in spoken[n1:]),
          f"spoken={spoken[n1:]}")


def test_long_confirm_accepted():
    print("[长确认句（>12字）→ 确认收尾：22:16:29'主播超说完全正确，主播点名辛苦'、"
          "22:49:36'数控操作非常正确'均被 len<=12 吞掉 → 点名卡死]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BG9BFZ，信号59"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    n0 = len(spoken)
    # 长确认句（18字，带告别语）→ 收尾（不是"忽略"）
    sess._asr_text = lambda p: "主播超说完全正确，主播点名辛苦，再见，七三"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("长确认句收尾", len(spoken) > n0
          and any("感谢确认" in s or "请下一位友台" in s for s in spoken[n0:]),
          f"spoken={spoken[n0:]}")
    check("长确认句已收尾", not sess._current_active, f"active={sess._current_active}")


def test_finish_intent_ends():
    print("[结束意图（不想补充了/完成点名）→ 直接收尾不再追问缺失字段："
          "22:48:21'不想补充了，我完成点名'被忽略 → 流程拖沓]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BG9BFZ"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    n0 = len(spoken)
    # 友台主动结束（信息未补全也不追问）
    sess._asr_text = lambda p: "不想补充了，我完成点名，主控辛苦了，七三"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("结束意图收尾", len(spoken) > n0
          and any("感谢确认" in s or "请下一位友台" in s for s in spoken[n0:]),
          f"spoken={spoken[n0:]}")
    check("结束意图不追问", not any("请再补充" in s for s in spoken[n0:]),
          f"spoken={spoken[n0:]}")
    check("结束意图已收尾", not sess._current_active, f"active={sess._current_active}")


def test_current_idle_timeout_yields():
    print("[当前友台静默超时 → 播'暂时无回应'收尾让位：22:40 BR9AB 播完"
          "'是否正确？Over'后 7 分钟无回应、22:16 确认句被吞挂 8 分钟]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BG9AA"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("A 抄收进行中", sess._current_active, f"active={sess._current_active}")
    # B 插队排队
    sess._asr_text = lambda p: "这里是BG9BB"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=8)
    check("B 已排队", [w["call"] for w in sess._waiting] == ["BG9BB"],
          f"waiting={sess._waiting}")
    # 把 A 的静默计时拨旧（超过默认 90s）→ 触发超时收尾让位 B
    sess._current_touch -= 91
    n0 = len(spoken)
    check("超时收尾让位", sess._check_current_idle()
          and any("暂时无回应" in s for s in spoken[n0:]),
          f"spoken={spoken[n0:]}")
    check("让位后轮到 B", sess._current_call == "BG9BB"
          and not sess._waiting and sess._current_active,
          f"cur={sess._current_call} waiting={sess._waiting}")
    # 未超时不触发
    sess._asr_text = lambda p: "抄收正确"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=8)
    n1 = len(spoken)
    check("未超时不收尾", not sess._check_current_idle()
          and len(spoken) == n1, f"spoken={spoken[n1:]}")


def test_similar_interloper_dedup():
    print("[插队相似呼号合并：22:46:15 BI9BCC 插队后 22:46:33 BA9BCC 重复"
          "（ASR 听写 I→A）→ 应判为同一人忽略，不重复入队]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    # 当前友台 A 进行中
    sess._asr_text = lambda p: "这里是BG9AA"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    # 插队 BI9BCC（session=8）
    sess._asr_text = lambda p: "这里是BI9BCC，请求参加点名"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=8)
    check("BI9BCC 已排队", [w["call"] for w in sess._waiting] == ["BI9BCC"],
          f"waiting={sess._waiting}")
    # 相似呼号 BA9BCC（编辑距离1）再来 → 忽略，不重复入队
    sess._asr_text = lambda p: "这里是BA9BCC，请求参加点名"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=9)
    check("相似插队忽略", len(sess._waiting) == 1
          and len(sess._checked_in) == 2,
          f"waiting={sess._waiting} checked={sess._checked_in}")
    # 完全不同的呼号仍正常排队
    sess._asr_text = lambda p: "这里是BG9CC"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=10)
    check("不同呼号正常排队", [w["call"] for w in sess._waiting] == ["BI9BCC", "BG9CC"],
          f"waiting={sess._waiting}")


def test_relay_same_session_interloper():
    print("[中继转发同 session：A 刚抄收 B 报不相似呼号 → 排队不顶 A（实测干扰场景）]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BG9AA"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("A 被抄收", sess._current_call == "BG9AA", f"cur={sess._current_call}")
    # 同一 session（中继转发）B 报完全不相似的呼号 → 排队，不替换 A
    sess._asr_text = lambda p: "这里是BG9BB"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("同session不相似→排队不顶A",
          sess._current_call == "BG9AA"
          and [c for c, *_ in sess._checked_in] == ["BG9AA", "BG9BB"]
          and any(w["call"] == "BG9BB" for w in sess._waiting),
          f"cur={sess._current_call} checked={sess._checked_in} "
          f"waiting={sess._waiting}")
    # 当前友台收尾 → 自动轮到 B
    sess._end_current()
    check("收尾后轮到 B", sess._current_call == "BG9BB",
          f"cur={sess._current_call}")


def test_similar_callsign_cross_session_replace():
    print("[跨 session 相似呼号（BLZ↔BFZ 编辑距离1）→ 识别修正替换]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "这里是BG9BLZ"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("BLZ 先被抄收", sess._current_call == "BG9BLZ", f"cur={sess._current_call}")
    # 另一设备（session=8）报相似呼号 → 识别修正，替换为 BFZ
    sess._asr_text = lambda p: "这里是BG九BFZ，Bravo Foxtrot Zulu"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=8)
    check("相似呼号跨session替换",
          [c for c, *_ in sess._checked_in] == ["BG9BFZ"]
          and sess._current_call == "BG9BFZ"
          and "BG9BLZ" not in sess._checked_calls
          and not sess._waiting,
          f"checked={sess._checked_in} cur={sess._current_call} "
          f"waiting={sess._waiting}")


def test_spell_merge_retry():
    print("[拼读合并：被请重复后逐字母拼读碎段 → 停顿后合并解出完整呼号]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    # 先造成重复请求流程（低置信度无呼号段消耗额度）
    sess._asr_text = lambda p: "哦我要高分耐"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=5)
    check("进入重复请求流程", sess._retry_pending and sess._retry_left == 0,
          f"pending={sess._retry_pending} left={sess._retry_left}")
    n0 = len(spoken)
    # 逐字母拼读（每段 1s 短段，同 session）：Bravo Golf Nine Bravo Foxtrot Zulu
    for pt in ["Bravo", "Golf", "Nine", "Bravo", "Foxtrot", "Zulu"]:
        sess._asr_text = lambda p, t=pt: t
        sess._process_segment(b"\x00" * 32000, 1.0, None, session=5)
    check("拼读段不刷请重复", len(spoken) == n0, f"spoken={spoken}")
    # 友台拼完停顿 → 主循环空闲轮 flush → 合并解出 BG9BFZ
    sess._spell_buf[5]["ts"] -= 5.0   # 模拟停顿超过 spell_gap_seconds
    sess._spell_flush_all()
    check("拼读合并解出 BG9BFZ",
          sess._current_call == "BG9BFZ"
          and [c for c, *_ in sess._checked_in] == ["BG9BFZ"],
          f"cur={sess._current_call} checked={sess._checked_in}")
    check("合并后恢复重复额度", sess._retry_left == 1 and not sess._retry_pending,
          f"left={sess._retry_left}")


def test_spell_merge_not_half():
    print("[拼读合并：半截拼读（BG9B）不立即误抄，等待完整拼读]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    sess._capture_pump = lambda: None
    sess._asr_text = lambda p: "哦我要高分耐"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=6)
    # 只拼 4 词（半截合法呼号 BG9B）后停顿 → flush 不应抄半截 BG9B
    for pt in ["Bravo", "Golf", "Nine", "Bravo"]:
        sess._asr_text = lambda p, t=pt: t
        sess._process_segment(b"\x00" * 32000, 1.0, None, session=6)
    sess._spell_buf[6]["ts"] -= 5.0
    sess._spell_flush_all()
    check("半截 BG9B 不误抄",
          sess._current_call is None
          and [c for c, *_ in sess._checked_in] == [],
          f"cur={sess._current_call} checked={sess._checked_in}")
    # 友台重新完整拼读（半截已丢弃，新缓存）→ 合并解出 BG9BFZ
    for pt in ["Bravo", "Golf", "Nine", "Bravo", "Foxtrot", "Zulu"]:
        sess._asr_text = lambda p, t=pt: t
        sess._process_segment(b"\x00" * 32000, 1.0, None, session=6)
    sess._spell_buf[6]["ts"] -= 5.0
    sess._spell_flush_all()
    check("重新完整拼读解出 BG9BFZ",
          sess._current_call == "BG9BFZ"
          and [c for c, *_ in sess._checked_in] == ["BG9BFZ"],
          f"cur={sess._current_call} checked={sess._checked_in}")


def test_correct_extract_and_replace():
    print("[纠正分支：直接提取正确信息并替换抄收]")
    sess = net_control.NetControlSession(link=None)
    sess._net_ctx = {"ctrl_call": "BI9BZW"}
    spoken = []
    sess._speak = lambda t: spoken.append(t)
    # 先抄收 BG9AFF
    sess._asr_text = lambda p: "这里是BG9AFF"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    # 友台纠正：含正确呼号但 decode 解不出（B9BFZ 缺 G）→ LLM 提取 → 替换旧抄收
    class FakeLLM:
        def extract(self, text):
            return {"callsign": "BG9BFZ", "signal": "59", "copied": True, "note": ""}
    sess._llm = FakeLLM()
    sess._asr_text = lambda p: "呼号不正确，我的呼号是B九BFZ"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("替换后仅剩新呼号", [c for c, *_ in sess._checked_in] == ["BG9BFZ"],
          f"checked={sess._checked_in}")
    check("旧呼号已移除", "BG9AFF" not in sess._checked_calls)
    check("上下文切换", sess._current_call == "BG9BFZ")
    check("信号保留", sess._checked_in[0][1] == "59", f"sig={sess._checked_in[0][1]}")
    # 提取失败（LLM 无结果）→ 请重报
    class FakeLLM2:
        def extract(self, text):
            return {"callsign": "", "signal": "", "copied": True, "note": ""}
    sess._llm = FakeLLM2()
    n = len(spoken)
    sess._asr_text = lambda p: "不正确，请重复"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("提取失败才请重报", len(spoken) == n + 1 and "重复" in spoken[-1],
          f"spoken={spoken[-1:]}")
    # 纠正段提取到与当前友台相同的呼号 → 确认无误收尾（不再请重报）
    sess._llm = FakeLLM()    # 返回 BG9BFZ == 当前友台
    sess._asr_text = lambda p: "呼号不对，我的呼号是B九BFZ"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=7)
    check("确认无误播收尾", "确认无误" in spoken[-1], f"spoken={spoken[-1:]}")


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
               test_asr_body, test_vocab_echo_reject, test_conn_reuse,
               test_config_defaults, test_retry_reset, test_llm_gate_and_missing_fields,
               test_info_followup, test_report_clean, test_report_fields,
               test_cosyvoice_tts,
               test_wait_channel_idle, test_export_csv, test_echo_filter,
               test_wait_idle_consumes_queue, test_mixed_callsign_decode,
               test_ctrl_call_filter, test_same_session_new_call,
               test_echo_other_speaker, test_llm_fallback, test_vad_stuck_release,
               test_llm_call_cap, test_tts_synth_drains_queue,
               test_speech_supersede_waits,
               test_interloper_queued, test_idle_recall,
               test_same_session_replace, test_report_merged_confirm,
               test_join_intent_guide, test_checkedin_repeat_feedback,
               test_replace_guard,
               test_suspend_resume,
               test_keeper_busy_cede_and_waitfor_onmsg,
               test_power_highpower_and_device_gt12, test_duplicate_report_fields,
               test_relay_same_session_interloper, test_similar_callsign_cross_session_replace,
               test_spell_merge_retry, test_spell_merge_not_half,
               test_correct_extract_and_replace, test_long_confirm_accepted,
               test_finish_intent_ends, test_current_idle_timeout_yields,
               test_similar_interloper_dedup, test_templates]:
        fn()
    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
