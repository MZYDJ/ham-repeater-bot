#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点名会话状态机测试（离线，桩 ASR/TTS）。
运行：python3 tests/test_session.py
覆盖：正常抄收→确认、重复抄收→提示跳过、低置信度→请重复（限次）→未抄收、
     固定名单模式超时跳过、开放模式窗口结束汇总。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import net_control
from net_control import NetControlSession

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


class FakeAsr:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
    def transcribe(self, wav_bytes, context_words=None):
        self.calls += 1
        return self.texts.pop(0) if self.texts else ""


class SpyTTS:
    def __init__(self):
        self.said = []
    def __call__(self, text):
        self.said.append(text)
        return ""          # 空路径：_speak 记录后跳过播报，状态机照常推进


def test_open_flow():
    print("[开放点名：抄收/去重/低置信度]")
    tts = SpyTTS()
    sess = NetControlSession(link=None, tts_func=tts)
    sess._asr = FakeAsr([
        "Bravo Hotel Three X-ray X-ray 信号五九",   # 正常抄收
        "Bravo Hotel Three X-ray X-ray 信号五九",   # 重复
        "信号很好",                                  # 低置信度 → 请重复
        "BG9ABC",                                   # 重复请求后直读合法呼号 → 抄收（额度恢复）
        "这个听不清",                                # 新 session 说话人无呼号 → 再请重复
        "还是听不清",                                # 连续低置信度（额度用尽）→ 未抄收
    ])
    # session 序列：前 3 段无 session 归属（尚未抄收任何友台）；段4 抄收 BG9ABC（session=1）；
    # 段5/6 为新说话人（session=2，区别于当前友台）→ 不归入补充信息，走引导/静默
    for s in [None, None, None, 1, 2, 2]:
        sess._process_segment(b"\x00" * 32000, 1.0, "", s)
    check("抄收 BH3XX", [c for c, *_ in sess._checked_in] == ["BH3XX", "BG9ABC"])
    check("重复计数", sess._dups == 1)
    check("抄收 BG9ABC", any(c == "BG9ABC" for c, *_ in sess._checked_in))
    check("未抄收计数", sess._failed == 1)
    sess._speak_summary()          # 触发摘要生成（真实流程在 _run 结尾自动调用）
    check("状态机摘要", sess.summary["count"] == 2 and sess.summary["failed"] == 1
          and sess.summary["duplicates"] == 1)


def test_roster_timeout():
    print("[固定名单：超时跳过]")
    tts = SpyTTS()
    sess = NetControlSession(link=None, tts_func=tts)
    sess._asr = FakeAsr([])
    import queue as _q
    sess._seg_queue = _q.Queue()
    skipped = []
    def stub_roster():
        for call in ["BH3EFG", "BG9ABC"]:
            sess._speak(f"请{call}回答。")
            try:
                sess._seg_queue.get(timeout=0.01)
            except _q.Empty:
                skipped.append(call)
                sess._speak("无人应答，继续下一位。")
    sess._run_roster = stub_roster
    sess._run_roster()
    check("全部超时跳过", skipped == ["BH3EFG", "BG9ABC"], str(skipped))
    said = " | ".join(tts.said)
    check("播报无人应答", "无人应答" in said, said)


def test_opening_fmt_and_timing():
    print("[开场白占位符 + 总时长/安静结束]")
    import queue as _q
    orig_cfg = net_control.nc_cfg
    def small_cfg(*path, **kw):
        key = path[0]
        default = kw.get("default")
        if key == "listen_after_open_seconds":
            return 0.1          # 最短窗口
        if key == "max_net_seconds":
            return 1            # 总时长 1s
        if key == "quiet_end_seconds":
            return 0.1          # 连续安静 0.1s 即收尾
        if key == "grace_seconds":
            return 2
        if key == "save_audio":
            return False        # 测试环境不落盘
        return orig_cfg(*path, **kw)
    net_control.nc_cfg = small_cfg
    try:
        tts = SpyTTS()
        sess = NetControlSession(link=None, tts_func=tts)
        sess._asr = FakeAsr([])
        sess._run()                             # 同步跑完整会话（开场→窗口→汇总）
    finally:
        net_control.nc_cfg = orig_cfg
    if not tts.said:
        check("开场白已播报", False, "无任何播报")
        return
    opening = tts.said[0]
    check("开场白占位符已替换", "{" not in opening and "CQ" in opening, opening)
    check("开场白含主控呼号", "BI9BZW" in opening, opening)
    has_closing = any("到此结束" in t or "73" in t for t in tts.said)
    check("到点安静后已收尾", has_closing, str(tts.said))


def main():
    print("== 点名会话状态机测试 ==")
    test_open_flow()
    test_roster_timeout()
    test_opening_fmt_and_timing()
    print(f"\n结果: PASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
