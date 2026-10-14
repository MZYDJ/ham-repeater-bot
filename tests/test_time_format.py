#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main 纯播报线：剩余时间格式化回归测试。"""
import os, sys, types, json, tempfile
from pathlib import Path
from unittest import mock

if "apscheduler" not in sys.modules:
    aps = types.ModuleType("apscheduler")
    sched = types.ModuleType("apscheduler.schedulers")
    bg = types.ModuleType("apscheduler.schedulers.background")
    class FakeBg:
        def __init__(self, *a, **k): pass
    bg.BackgroundScheduler = FakeBg
    sched.background = bg
    aps.schedulers = sched
    sys.modules["apscheduler"] = aps
    sys.modules["apscheduler.schedulers"] = sched
    sys.modules["apscheduler.schedulers.background"] = bg
if "edge_tts" not in sys.modules:
    et = types.ModuleType("edge_tts")
    et.Communicate = mock.MagicMock()
    sys.modules["edge_tts"] = et
root = tempfile.mkdtemp(prefix="time_format_test_")
cfg = {"paths":{"cache_dir":str(Path(root)/"cache"),"log_dir":str(Path(root)/"logs")},
       "notify":{"webhook_url":""},"tts":{"engine":"edge"},
       "announce":{"template":"test","start_hour":7,"end_hour":22}}
cfg_path = Path(root) / "config.json"
cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
os.environ["HAM_BOT_CONFIG"] = str(cfg_path)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import announce

cases = [(2522,"42分钟2秒"), (3600,"1小时"), (3661,"1小时1分钟"),
         (86400,"1天"), (-5,"0秒")]
for seconds, expected in cases:
    actual = announce.format_remaining_time(seconds)
    assert actual == expected, f"{seconds}: expected {expected!r}, got {actual!r}"
print(f"PASS: {len(cases)} duration-format cases")
