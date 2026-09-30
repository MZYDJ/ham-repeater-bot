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

# 原始函数引用（部分用例用真实实现测冷却/发送失败，需先恢复被 lambda 替换的模块全局）
_ORIG_SEND_WECHAT = A._send_wechat
_ORIG_NOTIFY_RESTART = A._notify_then_restart

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


# ============ 第三部分：纯函数补充（组合/配置变体/边界） ============
def test_decision_extra():
    print("\n=== 纯函数补充（组合/非默认配置/边界）===")
    kw = dict(last_announce_ok=0.0, last_net_skip_ts=0.0, last_tts_fail_ts=0.0,
              last_restart_ts=0.0, net_active=False,
              start_hour=7, end_hour=22, slot_tolerance_seconds=900,
              tts_fail_window_seconds=1800)
    T = lambda y, m, d, hh, mm: datetime.datetime(y, m, d, hh, mm)

    # E1. 点名跳过 + TTS 失败同时存在 → 点名豁免优先（skip）
    kwe1 = dict(kw, last_net_skip_ts=T(2026, 9, 30, 10, 30).timestamp(),
                last_tts_fail_ts=T(2026, 9, 30, 10, 40).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kwe1)
    check("E1 点名跳过+TTS失败共存 → 点名豁免优先", a == "skip" and "点名" in r)

    # E2. TTS 失败 + 近期自重启（< cand，无"重启前准点"干扰）→ notify_only 优先
    #      （冷却期拦截在 _notify_then_restart 执行层，X2 覆盖；此处验证纯函数层 TTS 优先）
    kwe2 = dict(kw, last_tts_fail_ts=T(2026, 9, 30, 10, 40).timestamp(),
                last_restart_ts=T(2026, 9, 30, 10, 20).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kwe2)
    check("E2 TTS失败+近期重启 → notify_only 优先", a == "notify_only")

    # E3. 非默认时段：start=8 end=21 → 22:00 场时段外 skip；07:30 skip；21:30 正常判定
    kw3 = dict(kw, start_hour=8, end_hour=21)
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 22, 50), **kw3)
    check("E3a 非默认时段 22:00 场 → skip", a == "skip")
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 7, 50), **kw3)
    check("E3b 非默认时段 07:30 场 → skip", a == "skip")
    kw3b = dict(kw3, last_announce_ok=T(2026, 9, 30, 21, 0).timestamp())
    a, r, cand, *_ = A._watchdog_decision(now=T(2026, 9, 30, 21, 50), **kw3b)
    check("E3c 非默认时段 21:30 场 → 正常判定", a == "restart" and cand.hour == 21 and cand.minute == 30)

    # E4. 非默认 TTS 窗口：tts_win=600（10min）
    kw4 = dict(kw, tts_fail_window_seconds=600)
    kwe4 = dict(kw4, last_tts_fail_ts=T(2026, 9, 30, 10, 45).timestamp())   # 5min 前
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kwe4)
    check("E4a tts_win=600 失败5min前 → notify_only", a == "notify_only")
    kwe4b = dict(kw4, last_tts_fail_ts=T(2026, 9, 30, 10, 35).timestamp())  # 15min 前
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 50), **kwe4b)
    check("E4b tts_win=600 失败15min前 → restart", a == "restart")

    # E5. 非默认容忍窗口：slot_tolerance=300（5min）
    kw5 = dict(kw, slot_tolerance_seconds=300)
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 34), **kw5)   # 差4min < 5min
    check("E5a 容忍300s 差4min → skip", a == "skip")
    kw5b = dict(kw5, last_announce_ok=T(2026, 9, 30, 10, 0).timestamp())
    a, r, *_ = A._watchdog_decision(now=T(2026, 9, 30, 10, 36), **kw5b)   # 差6min >= 5min
    check("E5b 容忍300s 差6min → 继续判定", a == "restart")

    # E6. 00:00 整边界（cand=00:00，hour=0 < 7 → skip）
    a, r, *_ = A._watchdog_decision(now=T(2026, 10, 1, 0, 0), **kw)
    check("E6 半夜00:00整 → skip 时段外", a == "skip" and r == "")

    # E7. 22:45 查 22:30 END 场（差 900s 整 = 容忍窗口边界，应判定非跳过）
    kw7 = dict(kw, last_announce_ok=T(2026, 9, 30, 22, 0).timestamp())
    a, r, cand, *_ = A._watchdog_decision(now=T(2026, 9, 30, 22, 45), **kw7)
    check("E7 END场22:45（差900s整）→ 判定", a == "restart" and cand.minute == 30)


# ============ 第四部分：执行层补充（通知/重启/恢复/文件/异常） ============
def test_execution_extra():
    print("\n=== 执行层补充（通知节流/冷却/恢复通知/文件/异常）===")
    T = lambda y, m, d, hh, mm: datetime.datetime(y, m, d, hh, mm)

    # X1. notify_only 降频节流：30min 内第二次 → 不推送
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 9, 30).timestamp()
    A._last_tts_fail_ts = T(2026, 9, 30, 10, 40).timestamp()
    A._last_tts_notify_ts = T(2026, 9, 30, 10, 42).timestamp()   # 8min 前推过
    sent, restarts = [], []
    A._send_wechat = lambda c: sent.append(c) or True
    A._notify_then_restart = lambda reason: restarts.append(reason)
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 10, 50).timestamp()):
        A.schedule_watchdog(now=T(2026, 9, 30, 10, 50))
    check("X1 TTS告警30min内第二次 → 节流不推送", not sent and not restarts)

    # X2. restart 冷却期内（用真实 _notify_then_restart 验证冷却逻辑）：不 execv，降频推送
    #      last_restart_ts 须 < cand（否则命中"重启前准点"豁免，先被 skip）
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    A._last_restart_ts = T(2026, 9, 30, 10, 0).timestamp()      # 冷却中（50min < 2h），且 < cand=10:30
    sent = []
    A._send_wechat = lambda c: sent.append(c) or True
    A._notify_then_restart = _ORIG_NOTIFY_RESTART               # 恢复真实冷却逻辑
    execv_calls = []
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 10, 50).timestamp()), \
            mock.patch.object(A.os, "execv", side_effect=lambda *a: execv_calls.append(a)):
        A.schedule_watchdog(now=T(2026, 9, 30, 10, 50))
    check("X2 冷却期内 → 不 execv", not execv_calls)
    check("X2b 冷却期内 → 推送'仍异常'状态", len(sent) == 1 and "冷却" in sent[0])

    # X3. restart 冷却到期 → 完整重启链（发送 → sleep → 写标记 → execv）
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    A._last_restart_ts = T(2026, 9, 30, 8, 0).timestamp()         # 2h50min 前，已出冷却
    sent, execv_calls = [], []
    A._send_wechat = lambda c: sent.append(c) or True
    A._notify_then_restart = _ORIG_NOTIFY_RESTART               # 恢复真实重启链
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 10, 50).timestamp()), \
            mock.patch.object(A.os, "execv", side_effect=lambda *a: execv_calls.append(a)), \
            mock.patch("announce.logging.shutdown", return_value=None), \
            mock.patch("announce.time.sleep", return_value=None):
        A.schedule_watchdog(now=T(2026, 9, 30, 10, 50))
    check("X3a 冷却到期 → 发送ERROR后 execv", execv_calls and sent and "ERROR" in sent[0])
    check("X3b 冷却到期 → 写重启时间戳文件", A._restart_ts_file.exists())
    check("X3c 冷却到期 → 写恢复待确认标记", A._recovery_pending_file.exists() and A._recovery_pending)

    # X4. _mark_announce_ok 恢复通知：自重启后首个准点成功 → 推送 OK + 清标记文件
    _reset()
    A._recovery_pending_file.write_text("1", encoding="utf-8")
    A._recovery_pending = True
    sent = []
    A._send_wechat = lambda c: sent.append(c) or True
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 11, 0).timestamp()):
        A._mark_announce_ok()
    check("X4a 恢复 → 推送[播报服务 OK]", len(sent) == 1 and "OK" in sent[0] and "恢复" in sent[0])
    check("X4b 恢复 → 清标记与文件", not A._recovery_pending and not A._recovery_pending_file.exists())
    check("X4c 恢复 → 更新打点", A._last_announce_ok == T(2026, 9, 30, 11, 0).timestamp())

    # X5. _mark_announce_ok 非恢复路径：只打点不推送
    _reset()
    A._recovery_pending = False
    sent = []
    A._send_wechat = lambda c: sent.append(c) or True
    with mock.patch("announce.time.time", return_value=T(2026, 9, 30, 11, 30).timestamp()):
        A._mark_announce_ok()
    check("X5 非恢复 → 只打点不推送", not sent and A._last_announce_ok == T(2026, 9, 30, 11, 30).timestamp())

    # X6. 恢复通知推送失败：_send_wechat 返回 False → 状态仍正确（标记已清，不崩）
    _reset()
    A._recovery_pending_file.write_text("1", encoding="utf-8")
    A._recovery_pending = True
    A._send_wechat = lambda c: False
    A._mark_announce_ok()
    check("X6 恢复推送失败 → 标记仍清、不崩", not A._recovery_pending and not A._recovery_pending_file.exists())

    # X7. _mark_announce_ok 恢复文件 unlink 失败 → warning 不崩、仍推送
    #      （Path.unlink 是 C 属性不可 patch，替换整个文件对象为 MagicMock）
    _reset()
    A._recovery_pending = True
    sent = []
    A._send_wechat = lambda c: sent.append(c) or True
    fake_file = mock.MagicMock()
    fake_file.unlink.side_effect = OSError("perm")
    with mock.patch.object(A, "_recovery_pending_file", fake_file):
        A._mark_announce_ok()
    check("X7 unlink失败 → 不崩、仍推送、标记已清", len(sent) == 1 and not A._recovery_pending)

    # X8. _load_restart_ts：不存在 → 0.0；合法 → float；损坏 → 0.0
    _reset()
    A._restart_ts_file.unlink(missing_ok=True)   # 清除 X3 冷却到期测试的残留
    check("X8a 无文件 → 0.0", A._load_restart_ts() == 0.0)
    A._restart_ts_file.write_text(str(T(2026, 9, 30, 10, 0).timestamp()), encoding="utf-8")
    check("X8b 合法文件 → float", A._load_restart_ts() == T(2026, 9, 30, 10, 0).timestamp())
    A._restart_ts_file.write_text("not-a-number", encoding="utf-8")
    check("X8c 损坏文件 → 0.0", A._load_restart_ts() == 0.0)
    A._restart_ts_file.unlink(missing_ok=True)

    # X9. _mark_net_skip 格起点口径（与看门狗 cand 一致）
    _reset()
    A._mark_net_skip(T(2026, 9, 30, 22, 35))
    check("X9a 22:35 跳过 → 记录 22:30", A._last_net_skip_ts == T(2026, 9, 30, 22, 30).timestamp())
    A._mark_net_skip(T(2026, 9, 30, 22, 20))
    check("X9b 22:20 跳过 → 记录 22:00", A._last_net_skip_ts == T(2026, 9, 30, 22, 0).timestamp())
    A._mark_net_skip(T(2026, 9, 30, 22, 30))
    check("X9c 22:30 整跳过 → 记录 22:30", A._last_net_skip_ts == T(2026, 9, 30, 22, 30).timestamp())

    # X10. _send_wechat 未配置 webhook → warning 返回 False 不崩（用真实实现）
    _reset()
    orig_url = A.WECHAT_WEBHOOK_URL
    A.WECHAT_WEBHOOK_URL = ""
    A._send_wechat = _ORIG_SEND_WECHAT
    check("X10 未配置webhook → False", A._send_wechat("test") is False)
    A.WECHAT_WEBHOOK_URL = orig_url

    # X11. _send_wechat 网络失败（urlopen 异常）→ 返回 False 不崩
    _reset()
    A.WECHAT_WEBHOOK_URL = "https://example.invalid/hook"
    A._send_wechat = _ORIG_SEND_WECHAT
    with mock.patch("announce.urllib.request.urlopen", side_effect=Exception("timeout")):
        check("X11 urlopen失败 → False", A._send_wechat("test") is False)
    A.WECHAT_WEBHOOK_URL = orig_url

    # X12. schedule_watchdog 判定异常（cfg_get 抛错）→ 记日志不崩
    _reset()
    A._last_announce_ok = T(2026, 9, 30, 10, 0).timestamp()
    with mock.patch("announce.cfg_get", side_effect=Exception("bad cfg")):
        A.schedule_watchdog(now=T(2026, 9, 30, 10, 50))   # 应打印"播报看门狗异常"且不崩
    check("X12 判定异常 → 不崩", True)


if __name__ == "__main__":
    test_decision_pure()
    test_integration()
    test_decision_extra()
    test_execution_extra()
    print(f"\n结果: PASS={_OK} FAIL={_FAIL}")
    sys.exit(1 if _FAIL else 0)
