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