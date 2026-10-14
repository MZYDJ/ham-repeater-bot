#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯播报版：TTS 蓄水池最终失败严重级别回归测试，无网络依赖。"""
import sys
import types
import datetime
import json
import tempfile
import os
import logging
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 让该测试不依赖 APScheduler / Edge-TTS 的真实运行环境。
if "apscheduler" not in sys.modules:
    aps = types.ModuleType("apscheduler")
    sched = types.ModuleType("apscheduler.schedulers")
    bg = types.ModuleType("apscheduler.schedulers.background")
    class FakeBg:
        def __init__(self, *args, **kwargs):
            pass
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

tmp_root = tempfile.mkdtemp(prefix="prefill_severity_test_")
config = {
    "paths": {
        "cache_dir": str(Path(tmp_root) / "cache"),
        "log_dir": str(Path(tmp_root) / "logs"),
    },
    "notify": {"webhook_url": ""},
    "announce": {
        "template": "测试播报 {year}-{month}-{day} {hour}:{minute_text}",
        "start_hour": 0,
        "end_hour": 23,
    },
}
config_path = str(Path(tmp_root) / "config.json")
with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f)
os.environ["HAM_BOT_CONFIG"] = config_path

import announce as A

def check(condition, message):
    if not condition:
        raise AssertionError(message)

check(A.PREFILL_ERROR_THRESHOLD_SECONDS == 86400,
      f"default threshold={A.PREFILL_ERROR_THRESHOLD_SECONDS}, expected 86400")

far_slot = datetime.datetime.now() + datetime.timedelta(hours=25)
with mock.patch.object(A, "get_upcoming_announce_times", return_value=[far_slot]), \
     mock.patch.object(A, "get_tts_file", return_value="") as failed_tts, \
     mock.patch.object(A.logger, "warning") as warning_log, \
     mock.patch.object(A.logger, "error") as error_log:
    A.tts_prefill_task()
check(warning_log.called and not error_log.called,
      f"far-future failure should be WARNING; warning={warning_log.call_count}, error={error_log.call_count}")
check(failed_tts.call_args.kwargs.get("final_failure_level") == logging.INFO,
      "prefill should defer final failure severity to deadline classification")

near_slot = datetime.datetime.now() + datetime.timedelta(minutes=10)
with mock.patch.object(A, "get_upcoming_announce_times", return_value=[near_slot]), \
     mock.patch.object(A, "get_tts_file", return_value=""), \
     mock.patch.object(A.logger, "warning") as warning_log, \
     mock.patch.object(A.logger, "error") as error_log:
    A.tts_prefill_task()
check(error_log.called and not warning_log.called,
      f"near-term failure should be ERROR; warning={warning_log.call_count}, error={error_log.call_count}")

print("PASS: TTS prefill threshold defaults to 24 hours")
print("PASS: far-future final failure is WARNING")
print("PASS: near-term final failure is ERROR")
