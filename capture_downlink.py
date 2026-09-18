#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下行语音包诊断 —— capture_downlink.py
登录常驻链路后被动接收 UDPTunnel(ID=1) 语音包，对每个包打印：
    - 实际包长 / 前16字节hex（核对 189B 结构与 session 注入）
    - parse_udp_voice_multi 解析出的 session / seq / 声明len / 实际负载长
    - Opus 解码后的样本数（关键：5760样本=120ms=6帧正常；960=20ms=1帧 → 倍速根源）
    - 到达时刻与间隔（核对 600ms 批 5 包节奏）

用法：
    python3 capture_downlink.py --seconds 40
    启动后让任一友台（或手机官方 App 另一账号）上台说一句完整的话（5~10秒）。

用于定位"落盘录音倍速+一卡一卡"：是解码样本数不对（帧结构），
还是包到达/丢包（节奏），还是格式假设错误（包长/session）。
"""
import argparse
import time
from collections import Counter

import direct_announce

def main():
    ap = argparse.ArgumentParser(description="下行语音包诊断（打印包结构+解码样本数）")
    ap.add_argument("--seconds", type=float, default=40.0,
                    help="监听秒数（期间需有人上台说话）")
    args = ap.parse_args()

    rows = []          # (t相对秒, 包长, session, seq, 声明len, 实际负载长, 解码样本数, 前16Bhex)
    dec = direct_announce.OpusDecoder()
    t0 = time.time()

    def on_downlink(msg_type, payload):
        if msg_type != 1 or len(payload) < 2:
            return
        try:
            voices = direct_announce.parse_udp_voice_multi(payload)
        except Exception as e:
            print("解析异常:", e)
            return
        if not voices:
            return
        for v in voices:
            samples = None
            try:
                pcm = dec.decode(v["opus"])
                samples = len(pcm) // 2      # 样本数（int16）
            except Exception as e:
                samples = "ERR:%s" % e
            rows.append((time.time() - t0, len(payload), v["session"], v["seq"],
                         len(v["opus"]), samples, payload[:16].hex()))

    link = direct_announce.PersistentAnnouncer(on_downlink=on_downlink)
    print("连接常驻链路（后台 Ping 保活）……")
    link.start()
    print(">>> 请让友台/手机官方 App（另一账号、同频道）上台说一句完整的话（5~10秒）<<<")
    print("监听 %.0f 秒……" % args.seconds)
    try:
        time.sleep(args.seconds)
    finally:
        link.stop()

    if not rows:
        print("\n未收到任何语音包（确认对方账号与脚本同频道且已上台说话）")
        return
    print("\n%-4s %-9s %-6s %-9s %-6s %-8s %-8s %s" %
          ("#", "t(s)", "包长", "session", "seq", "声明len", "解码样本", "前16B"))
    prev_seq = prev_t = None
    for idx, (t, blen, sess, seq, ln, samples, hx) in enumerate(rows, 1):
        ds = (seq - prev_seq) if prev_seq is not None else None
        dt = (t - prev_t) if prev_t is not None else None
        print("%-4d %-9.3f %-6d %-9d %-6d %-8d %-8s %s%s%s" %
              (idx, t, blen, sess, seq, ln, samples, hx,
               ("  Δseq=%+d" % ds) if ds is not None else "",
               ("  Δt=%+.3f" % dt) if dt is not None else ""))
        prev_seq, prev_t = seq, t

    print("\n===== 统计 =====")
    print("语音包总数: %d" % len(rows))
    print("包长集合: %s" % dict(Counter(r[1] for r in rows)))
    print("session 集合: %s" % dict(Counter(r[2] for r in rows)))
    print("seq 增量分布: %s" % dict(Counter(r[4] - r2[4] for r, r2 in zip(rows, rows[1:]) if r[4] and r2[4])))
    print("解码样本分布: %s" % dict(Counter(str(r[5]) for r in rows)))
    # 间隔统计
    its = [rows[i][0] - rows[i - 1][0] for i in range(1, len(rows))]
    if its:
        its.sort()
        print("到达间隔: 中位 %.3fs 均值 %.3fs 最小 %.3fs 最大 %.3fs" %
              (its[len(its) // 2], sum(its) / len(its), its[0], its[-1]))
    # 结论提示
    samples_counter = Counter(str(r[5]) for r in rows)
    common = samples_counter.most_common(1)
    if common:
        s = common[0][0]
        if s == "5760":
            print("\n→ 解码样本=5760（120ms/包，6帧）正常：倍速不是帧结构问题，检查包到达/丢包")
        elif s.isdigit() and int(s) < 5760:
            print("\n→ 解码样本=%s（<5760）：每包实际音频不足120ms → 倍速根源在此"
                  % s)
        else:
            print("\n→ 解码样本异常分布：%s" % dict(samples_counter))

if __name__ == "__main__":
    main()
