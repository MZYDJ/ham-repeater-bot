#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点名主播离线单元测试（无网络依赖）。
运行：python3 tests/run_tests.py
覆盖：字母解释法解码/呼号校验/信号提取/去重、Mumble varint 编解码、
UDP 语音包解析（含回声过滤）、Opus 编解码往返、VAD 切段、WAV 落盘、配置回退。
"""
import http.client
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
    check("同台补充信息归入", len(spoken) == 3 and "信息已记录" in spoken[2]
          and "设备 泉盛K6" in spoken[2] and "天线 原机天线" in spoken[2]
          and "功率 5 瓦" in spoken[2]
          and sess._current_entry[4] and "泉盛K6" in sess._current_entry[4],
          f"spoken={spoken} entry={sess._current_entry}")
    check("补充段不消耗额度", sess._retry_left == 1, f"retry_left={sess._retry_left}")

    # 段4：新 session 无呼号（新友台没报呼号）→ 引导报呼号
    sess._asr_text = lambda pcm: "这里是，我的设备是泉盛K6"
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=3)
    check("新友台无呼号仍引导", len(spoken) == 4 and "呼号" in spoken[3],
          f"spoken={spoken}")

    # 段5：同新 session 连续无呼号（额度尽）→ 静默但计数
    sess._process_segment(b"\x00" * 32000, 1.0, None, session=3)
    check("额度用尽后静默", len(spoken) == 4 and sess._failed == 1,
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
    check("QTH/设备/天线/功率全提取", d.get("QTH") == "咸阳市渭城区"
          and d.get("设备") == "泉盛K6" and d.get("天线") == "原机天线"
          and d.get("功率") == "5 瓦", f"{r}")
    r = f("Q T H 咸阳，设备是即时通，天线原机天线，5W")
    d = dict(r)
    check("Q T 展开/阿拉伯功率", d.get("QTH") == "咸阳" and d.get("设备") == "即时通"
          and d.get("功率") == "5 瓦", f"{r}")
    r = f("信号五九")
    check("信号报告提取", dict(r).get("信号") == "59", f"{r}")
    r = f("主控是否抄收")
    check("询问语无字段", r == [], f"{r}")
    r = f("那主播")
    check("噪音无字段", r == [], f"{r}")


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
               test_asr_body, test_conn_reuse, test_config_defaults, test_retry_reset,
               test_info_followup, test_report_clean, test_report_fields,
               test_wait_channel_idle, test_templates]:
        fn()
    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
