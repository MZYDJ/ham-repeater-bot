"""播报看门狗判定逻辑回归测试（纯函数穷举 + 集成场景）。

运行：python3 tests/test_watchdog.py

覆盖：历次生产故障与用户发现的逻辑盲区——
  - 9/24 静默停播 → 准点缺失判定
  - 点名跨准点误报（22:10 点名跨 22:30）→ 点名豁免
  - 夜间误报（旧 gap 版）→ 时段外豁免
  - 9/30 首场缺失 → 容忍窗口/重启前豁免
  - 蓄水池失败打点盲区 → 现场失败才豁免重启（核心场景）
  - 现场成功清标 → 旧标记不掩盖后续真假死
  - TTS 失败窗口过期 → 恢复重启

两部分：
  1. 纯函数 _watchdog_decision 穷举：直接传参、不 mock 时钟，覆盖全部判定分支与边界。
  2. 集成 schedule_watchdog：mock 全局态，验证决策被执行层正确执行。
"""
import sys, types, datetime, json, tempfile, os
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 仓库根目录（announce 所在）

# ---------- import 垫片（本地无 apscheduler/edge_tts 时 import announce 顶层可过） ----------
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

# ---------- 临时 config 注入 ----------
_CFG_TEMPLATE = {
    "paths": {"cache_dir": ".cache", "log_dir": ".logs"},
    "notify": {"webhook_url": ""},
    "watchdog": {"slot_tolerance_seconds": 900, "tts_fail_window_seconds": 1800},
}
_tmp_cfg = tempfile.mktemp(suffix=".json")
json.dump(_CFG_TEMPLATE, open(_tmp_cfg, "w"))
os.environ["HAM_BOT_CONFIG"] = _tmp_cfg

import announce as A

D = datetime.datetime
_OK = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _OK, _FAIL
    if cond:
        _OK += 1
        print(f"  PASS {name}" + (f"  {detail}" if detail else ""))
    else:
        _FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ============ 第一部分：纯函数 _watchdog_decision 穷举 ============
def test_decision_pure():
    print("\n=== 纯函数判定分支与边界穷举 ===")
    kw = dict(last_announce_ok=0.0, last_net_skip_ts=0.0, last_tts_fail_ts=0.0,
              last_restart_ts=0.0, net_active=False,
              start_hour=7, end_hour=22, slot_tolerance_seconds=900,
              tts_fail_window_seconds=1800)
    T = lambda y, m, d, hh, mm: datetime.datetime(y, m, d, hh, mm)

    # 1. 点名进行中 → skip 静默
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), net_active=True, **{k: v for k, v in kw.items() if k != "net_active"})
    check("点名中 → skip 静默", a == "skip" and r == "")

    # 2. 时段外（23:00 查，cand=23:00 不在 7~22）→ skip
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 23, 0), **kw)
    check("时段外(23:00) → skip 静默", a == "skip" and r == "")

    # 3. 容忍窗口内（10:35 查 10:30，差 5min < 15min）→ skip
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 35), **kw)
    check("容忍窗口内(差5min) → skip", a == "skip" and r == "")

    # 4. 容忍窗口边界（10:45 查 10:30，差 15min = 900s，>= 边界）→ 继续判定
    kw4 = dict(kw, last_announce_ok=T(2026, 9, 30, 10, 0).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 45), **kw4)
    check("容忍窗口边界(差15min) → 继续判定", a == "restart")

    # 5. 重启前准点（cand < last_restart_ts）→ skip
    kw5 = dict(kw, last_restart_ts=T(2026, 9, 30, 10, 40).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw5)
    check("重启前准点 → skip 不追责", a == "skip" and r == "")

    # 6. 已成功打点 → skip
    kw6 = dict(kw, last_announce_ok=T(2026, 9, 30, 10, 30).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw6)
    check("已成功打点 → skip", a == "skip" and r == "")

    # 7. 点名跨准点跳过 → skip + 非空 reason（可打 INFO）
    kw7 = dict(kw, last_net_skip_ts=T(2026, 9, 30, 10, 30).timestamp())
    a, r, cand, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw7)
    check("点名跳过 → skip + reason 非空", a == "skip" and r != "" and cand.hour == 10 and cand.minute == 30)

    # 8. 现场 TTS 失败窗口内 → notify_only
    kw8 = dict(kw, last_tts_fail_ts=T(2026, 9, 30, 10, 40).timestamp())
    a, r, _, gap, tts_win = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw8)
    check("现场TTS失败窗口内 → notify_only", a == "notify_only" and "TTS" in r and tts_win == 1800)

    # 9. TTS 失败窗口边界（失败在 30min 前，>= 窗口）→ 恢复 restart
    kw9 = dict(kw, last_tts_fail_ts=T(2026, 9, 30, 10, 20).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw9)
    check("TTS失败窗口边界(30min) → 恢复 restart", a == "restart")

    # 10. TTS 失败窗口过期 → restart
    kw10 = dict(kw, last_tts_fail_ts=T(2026, 9, 30, 10, 0).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw10)
    check("TTS失败窗口过期 → restart", a == "restart")

    # 11. 无任何标记 → restart（真假死主路径）；last_announce_ok=0（进程初值）时 gap 为大数
    a, r, _, gap, _ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw)
    check("无标记 → restart + 准点在文案中", a == "restart" and "10:30" in r and gap > 0)

    # 12. 边界：END 场（22:30 在窗口内，22:50 查）→ 正常判定
    kw12 = dict(kw, last_announce_ok=T(2026, 9, 30, 22, 0).timestamp())
    a, r, cand, *_ = A._watchdog_decision(now=T(2026, 9, 30, 22, 50), **kw12)
    check("END场22:30 → 正常判定", a == "restart" and cand.hour == 22 and cand.minute == 30)

    # 13. 边界：START 场（07:30 在窗口内）→ 正常判定
    kw13 = dict(kw, last_announce_ok=T(2026, 9, 30, 7, 0).timestamp())
    a, r, cand, *_ = A._watchdog_decision(now=T(2026, 9, 30, 7, 50), **kw13)
    check("START场07:30 → 正常判定", a == "restart" and cand.hour == 7 and cand.minute == 30)

    # 14. 半夜 00:30 查（cand=00:30，hour=0 < 7）→ skip
    a, r, *_ = A._watchdog_decision(now=T(2026, 10, 1, 0, 30), **kw)
    check("半夜00:30 → skip 时段外", a == "skip" and r == "")

    # 15. gap 计算：最近成功 10:00、10:50 查 10:30 场 → gap=3000s
    kw15 = dict(kw, last_announce_ok=T(2026, 9, 30, 10, 0).timestamp())
    a, r, _, gap, _ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kw15)
    check("gap 计算正确(3000s)", a == "restart" and gap == 3000 and "3000" in r)


# ============ 第二部分：集成 schedule_watchdog ============
def _run_watchdog(now_dt):
    sent, restarts = [], []
    A._send_wechat = lambda c: sent.append(c) or True
    A._notify_then_restart = lambda reason: restarts.append(reason)
    # schedule_watchdog(now=...) 直接注入时间；time.time 仅供内部打点/节流与 now 对齐
    with mock.patch("announce.time.time", return_value=now_dt.timestamp()):
        A.schedule_watchdog(now=now_dt)
    return restarts, sent


def _reset():
    for v in ("_last_tts_fail_ts", "_last_net_skip_ts", "_last_tts_notify_ts", "_last_restart_ts"):
        setattr(A, v, 0.0)


def test_integration():
    print("\n=== 集成 schedule_watchdog（全局态）===")
    T = lambda y, m, d, hh, mm: datetime.datetime(y, m, d, hh, mm)

    # I1. 核心场景（用户发现）：蓄水池 get_tts_file 失败 → 不打点 → 准点缺失照常重启
    _reset()
    with mock.patch.object(A.edge_tts.Communicate, "save", side_effect=Exception("conn timeout")):
        r = A.get_tts_file("测试蓄水池失败文本", max_retries=1)
    check("I1a 蓄水池合成失败返回空", r == "")
    check("I1b 蓄水池失败不打点", A._last_tts_fail_ts == 0.0)
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    restarts, sent = _run_watchdog(T(2026, 9, 30, 10, 50))
    check("I1c 蓄水池失败不豁免 → 准点缺失重启", len(restarts) == 1 and "10:30" in restarts[0])

    # I2. 现场失败打点 → 窗口内只告警不重启
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 9, 30).timestamp()
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 10, 40).timestamp()):
        A._mark_tts_fail()                # 现场失败打点 10:40（10:50 检查时在 30min 窗口内）
    restarts, sent = _run_watchdog(T(2026, 9, 30, 10, 50))
    check("I2 现场失败 → 只告警不重启", len(restarts) == 0 and sent and "WARN" in sent[0])

    # I3. 点名跨准点 → 静默豁免（无重启、无推送）
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 22, 0).timestamp()
    A._last_net_skip_ts = T(2026, 9, 30, 22, 30).timestamp()
    restarts, sent = _run_watchdog(T(2026, 9, 30, 22, 45))
    check("I3 点名跨准点 → 静默豁免", len(restarts) == 0 and not sent)

    # I4. 无标记 → 告警+重启（真假死主路径；推送职责在 _notify_then_restart 内部，此处验证触发）
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    restarts, sent = _run_watchdog(T(2026, 9, 30, 10, 50))
    check("I4 无标记 → 告警+重启", len(restarts) == 1)

    # I5. 现场成功清标 → 后续缺失不被旧标记掩盖
    _reset()
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 10, 40).timestamp()):
        A._mark_tts_fail()
        A._mark_tts_ok()
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    restarts, sent = _run_watchdog(T(2026, 9, 30, 10, 50))
    check("I5 现场成功清标 → 后续缺失重启", len(restarts) == 1)

    # I6. 时段外夜间 → 静默（无重启、无推送）
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 22, 30).timestamp()
    restarts, sent = _run_watchdog(T(2026, 9, 30, 23, 0))
    check("I6 夜间时段外 → 静默", len(restarts) == 0 and not sent)

    # I7. 容忍窗口内（10:35 查 10:30）→ 静默
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    restarts, sent = _run_watchdog(T(2026, 9, 30, 10, 35))
    check("I7 容忍窗口内 → 静默", len(restarts) == 0 and not sent)


if __name__ == "__main__":
    test_decision_pure()
    test_integration()
    print(f"\n结果: PASS={_OK} FAIL={_FAIL}")
    sys.exit(1 if _FAIL else 0)
