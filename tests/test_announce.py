#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""announce.py 调度/播报执行层 + direct_announce 纯函数回归测试（无网络依赖）。

运行：python3 tests/test_announce.py

覆盖（对应历次生产故障的修复点）：
  - 文案/时间纯函数：format_minute_text / get_announce_text / _next_announce_time /
    get_upcoming_announce_times（准点边界/跨天/时段外）
  - TTS 链路：tts_cache_path / _tts_min_seconds / get_tts_file（缓存命中/损坏重合成/最终失败）
  - 蓄水池：tts_prefill_task（队列让位/已备好跳过/缺口补/失败记录）、prepare_next_tts（含过期缓存清理）
  - 调度：schedule_prewarm / schedule_announce / schedule_tts_prefill / schedule_net_control
  - native 链路：_native_link_start（幂等/失败兜底）、_native_link_suspend/resume（异常吞掉）、
    _native_prewarm（点名跳过/残留清理/距离远只构建/常驻健康检查预建/临时短链兜底/抢麦失败清理）、
    _native_play、_announce_native（成功打点/失败不打点）
  - 播报核心：announce_task（点名中跳过+打点/TTS失败快速跳场+打点/预建命中播报+释放/无预建兜底/异常不崩）
  - 其他：net_active / _net_start / _net_stop / trim_python_memory / _StdoutToLogger /
    WeChatWebhookHandler（emit 起线程/payload 构造/_send 成功失败）
  - direct_announce 纯函数：dry_validate_mp3（长度阈值/异常）、trim_silence（头尾裁剪/全静音/余量）、
    parse_udp_voice_multi（多包串联/回声过滤/截断/非语音）、parse_udp_voice、build_audio（补零/185字节包）
"""
import sys, types, datetime, json, tempfile, os, array, logging, time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------- import 垫片（与 test_watchdog 同款） ----------
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

# ---------- 临时 config（隔离目录，不入库） ----------
_tmp_root = tempfile.mkdtemp(prefix="announce_test_")
_CFG_TEMPLATE = {
    "paths": {"cache_dir": str(Path(_tmp_root) / "cache"),
              "log_dir": str(Path(_tmp_root) / "logs")},
    "notify": {"webhook_url": ""},
    "announce": {"template": "现在是{year}年{month}月{day}日{weekday}，{hour}点{minute_text}。测试播报",
                 "start_hour": 7, "end_hour": 22},
    "watchdog": {"slot_tolerance_seconds": 900, "tts_fail_window_seconds": 1800},
}
_tmp_cfg = tempfile.mktemp(suffix=".json")
json.dump(_CFG_TEMPLATE, open(_tmp_cfg, "w"))
os.environ["HAM_BOT_CONFIG"] = _tmp_cfg

import announce as A
import direct_announce as DA

D = datetime.datetime
T = lambda y, m, d, hh, mm, ss=0: datetime.datetime(y, m, d, hh, mm, ss)
_OK = 0
_FAIL = 0

def check(name, cond, detail=""):
    global _OK, _FAIL
    if cond:
        _OK += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")

def _reset_announce_state():
    """恢复模块级全局态，避免用例间串扰（与 test_watchdog._reset 同口径）"""
    A._native_session = None
    A._native_prebuilt = None
    A._native_session_temp = False
    A._native_link = None
    A._net_session = None
    A._last_announce_ok = time.time()
    A._announce_ok_seen = False
    A._last_tts_fail_ts = 0.0
    A._last_net_skip_ts = 0.0

# ============ 第一部分：文案/时间纯函数 ============
def test_text_time_pure():
    print("\n=== 文案/时间纯函数 ===")
    check("format_minute_text(0)→整", A.format_minute_text(0) == "整")
    check("format_minute_text(30)→三十", A.format_minute_text(30) == "三十")
    check("format_minute_text(15)→15分", A.format_minute_text(15) == "15分")

    txt = A.get_announce_text(T(2026, 9, 30, 17, 30))
    check("get_announce_text 全字段替换",
          "2026年9月30日星期三" in txt and "17点三十" in txt, txt)

    # get_upcoming_announce_times：准点边界
    slots = A.get_upcoming_announce_times(T(2026, 9, 30, 10, 5), hours=1)
    check("枚举时段内准点(10:05→10:30起)", slots and slots[0].hour == 10 and slots[0].minute == 30,
          str(slots[:2]))
    check("不含 now 本身（恰在准点）",
          A.get_upcoming_announce_times(T(2026, 9, 30, 10, 0), hours=0) == [],
          str(A.get_upcoming_announce_times(T(2026, 9, 30, 10, 0), hours=0)))
    # 跨天：22:45 查 → 次日 07:00 起（23:00 时段外排除）
    slots = A.get_upcoming_announce_times(T(2026, 9, 30, 22, 45), hours=24)
    check("跨天首场为次日07:00", slots[0].hour == 7 and slots[0].day == 1,
          str(slots[0]))
    check("23:00 时段外排除", all(s.hour <= 22 for s in slots))
    # hours=0 但 now 在准点前 → 空
    check("hours=0 准点前无场次", A.get_upcoming_announce_times(T(2026, 9, 30, 22, 45), hours=0) == [])

    check("_next_announce_time 10:00→10:30",
          A._next_announce_time(T(2026, 9, 30, 10, 0)).minute == 30)
    check("_next_announce_time 10:20→10:30",
          A._next_announce_time(T(2026, 9, 30, 10, 20)).minute == 30)
    nxt = A._next_announce_time(T(2026, 9, 30, 10, 31))
    check("_next_announce_time 10:31→11:00", nxt.hour == 11 and nxt.minute == 0)

    # 抢麦提前量设计值：默认 1.0s（发起提前1s，take_mic往返~1.1s，完成≈整点后0.1s；
    # 若回到 0.5 则完成必落整点后 ~0.6s，日志显得"每次都迟到抢麦"）
    check("NATIVE_MIC_LEAD 默认1.0s", A.NATIVE_MIC_LEAD == 1.0, A.NATIVE_MIC_LEAD)

# ============ 第二部分：TTS 缓存/合成 ============
def test_tts_cache():
    print("\n=== TTS 缓存路径/时长估算 ===")
    p = A.tts_cache_path("hello")
    check("tts_cache_path md5 文件名", p.name.endswith(".mp3") and len(p.stem) == 32, str(p))
    check("_tts_min_seconds 按长度估算", A._tts_min_seconds("x" * 100) >= 2.0)
    check("_tts_min_seconds 最短2s", A._tts_min_seconds("短") == 2.0)

def test_get_tts_file():
    print("\n=== get_tts_file（缓存命中/损坏重合成/最终失败）===")
    # 缓存命中：dry_validate 通过 → 直接返回
    _reset_announce_state()
    text = "现在是2026年10月1日星期四，8点整。测试播报"
    cache = A.tts_cache_path(text)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"fake-mp3")
    with mock.patch("announce.direct_announce.dry_validate_mp3", return_value=True):
        r = A.get_tts_file(text, max_retries=1, retry_delay=0)
    check("缓存命中直接返回", r == str(cache), r)

    # 缓存损坏：dry_validate 失败 → 删除 → 合成失败（edge_tts stub 不产文件）→ 返回空
    cache.write_bytes(b"fake-mp3")
    with mock.patch("announce.direct_announce.dry_validate_mp3", return_value=False), \
            mock.patch("announce.time.sleep", return_value=None):
        r = A.get_tts_file(text, max_retries=1, retry_delay=0)
    check("缓存损坏→删除→合成失败→空", r == "" and not cache.exists(), r)

    # 最终失败路径（edge_tts 抛异常，重试 3 次后返回空）
    _edge_err = mock.MagicMock()
    _edge_err.save.side_effect = Exception("conn reset")
    with mock.patch("announce.edge_tts.Communicate", return_value=_edge_err), \
            mock.patch("announce.time.sleep", return_value=None), \
            mock.patch("announce.direct_announce.dry_validate_mp3", return_value=False):
        r = A.get_tts_file("t", max_retries=3, retry_delay=0)
    check("合成重试3次最终失败→空", r == "", r)

# ============ 第三部分：蓄水池/预热 ============
def test_prefill_and_prewarm_sched():
    print("\n=== 蓄水池 tts_prefill_task / prepare_next_tts / schedule_× ===")
    _reset_announce_state()
    # tts_prefill_task：队列非空 → 本轮提前结束（不合成）
    A.task_queue.put("announce")
    called = []
    with mock.patch("announce.get_upcoming_announce_times",
                    return_value=[T(2026, 9, 30, 11, 0)]), \
            mock.patch("announce.get_tts_file", side_effect=lambda *a, **k: called.append(1) or "x"):
        A.tts_prefill_task()
    A.task_queue.get()
    check("蓄水池队列非空→提前让位", not called)
    # tts_prefill_task：已备好（dry_validate 通过）→ 跳过；缺 → 合成；失败 → 记录缺口
    A.task_queue.queue.clear()
    slot = T(2026, 9, 30, 11, 0)
    cache = A.tts_cache_path(A.get_announce_text(slot))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"ok")
    synth = []
    with mock.patch("announce.get_upcoming_announce_times", return_value=[slot]), \
            mock.patch("announce.direct_announce.dry_validate_mp3",
                       side_effect=lambda p, min_seconds=0: str(p) == str(cache)), \
            mock.patch("announce.get_tts_file", side_effect=lambda *a, **k: synth.append(1) or "x"):
        A.tts_prefill_task()
    check("蓄水池已备好跳过", not synth)
    # 缺口：dry_validate 全 False → 合成 1 次；再 mock 合成失败 → 记录缺口
    with mock.patch("announce.get_upcoming_announce_times", return_value=[slot]), \
            mock.patch("announce.direct_announce.dry_validate_mp3", return_value=False), \
            mock.patch("announce.get_tts_file", return_value=""):
        A.tts_prefill_task()
    check("蓄水池失败记录缺口（不抛）", True)

    # prepare_next_tts：无 slots 直接返回；有 slots 合成 + 清理过期缓存
    with mock.patch("announce.get_upcoming_announce_times", return_value=[]), \
            mock.patch("announce.get_tts_file", return_value="") as g:
        A.prepare_next_tts()
    check("prepare_next_tts 无场次不合成", not g.called)
    cache_dir = Path(A.CACHE_DIR)
    old = cache_dir / "old.mp3"
    new = cache_dir / "new.mp3"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    old_ts = time.time() - 3 * 86400
    os.utime(old, (old_ts, old_ts))
    with mock.patch("announce.get_upcoming_announce_times",
                    return_value=[T(2026, 9, 30, 11, 0)]), \
            mock.patch("announce.get_tts_file", return_value="x.mp3"):
        A.prepare_next_tts()
    check("过期缓存清理(>2天删)", not old.exists())
    check("新缓存保留", new.exists())

    # schedule_×：入队内容正确
    A.task_queue.queue.clear()
    A.schedule_prewarm()
    A.schedule_announce()
    A.schedule_tts_prefill()
    got = [A.task_queue.get(), A.task_queue.get(), A.task_queue.get()]
    check("schedule_× 入队正确", got == ["prewarm", "announce", "tts_prefill"], str(got))
    # schedule_net_control → _net_start（不排队）
    with mock.patch.object(A, "_net_start") as ns:
        A.schedule_net_control()
    check("schedule_net_control 直调 _net_start", ns.called)

    # prewarm_task → prepare_next_tts + _native_prewarm
    with mock.patch.object(A, "prepare_next_tts") as pnt, \
            mock.patch.object(A, "_native_prewarm") as npw:
        A.prewarm_task()
    check("prewarm_task 两步串联", pnt.called and npw.called)

# ============ 第四部分：native 链路 ============
def test_native_link():
    print("\n=== native 链路（_native_link_start / suspend/resume / net_active / _net_*）===")
    _reset_announce_state()
    # _native_link_start：启动成功
    link = mock.MagicMock()
    with mock.patch("announce.direct_announce.PersistentAnnouncer", return_value=link):
        A._native_link_start()
    check("常驻链路启动", A._native_link is link and link.start.called)
    # 幂等：已有链路不重复建
    with mock.patch("announce.direct_announce.PersistentAnnouncer") as pa:
        A._native_link_start()
    check("常驻链路幂等", not pa.called)
    # 启动失败 → 告警不抛
    A._native_link = None
    with mock.patch("announce.direct_announce.PersistentAnnouncer",
                    side_effect=Exception("dns")):
        A._native_link_start()
    check("常驻链路启动失败兜底", A._native_link is None)

    # suspend/resume：无链路不抛；有链路调用；异常吞掉
    A._native_link = None
    A._native_link_suspend(); A._native_link_resume()
    check("suspend/resume 无链路不抛", True)
    A._native_link = mock.MagicMock()
    A._native_link_suspend(); A._native_link_resume()
    check("suspend/resume 调链路", A._native_link.suspend.called and A._native_link.resume.called)
    A._native_link.suspend.side_effect = Exception("x")
    A._native_link_suspend()
    check("suspend 异常吞掉", True)

    # net_active
    check("net_active 无会话→False", not A.net_active())
    sess = mock.MagicMock(); sess.active = True
    A._net_session = sess
    check("net_active 会话active→True", A.net_active())
    sess.active = False
    check("net_active 会话非active→False", not A.net_active())
    _reset_announce_state()

    # _net_start：未启用不启动；启用但链路未就绪 → 告警；启用+链路 → 启动会话
    # （net_control 是函数内局部 import，mock 用 sys.modules 替换）
    fake_nc = mock.MagicMock()
    orig_enabled = A.NET_ENABLED
    A.NET_ENABLED = False
    with mock.patch.dict(sys.modules, {"net_control": fake_nc}):
        A._net_start()
    check("_net_start 未启用不启动", not A._net_session)
    A.NET_ENABLED = True
    A._native_link = None
    with mock.patch.dict(sys.modules, {"net_control": fake_nc}):
        A._net_start()
    check("_net_start 链路未就绪→取消", A._net_session is None)
    A._native_link = mock.MagicMock()
    sess = mock.MagicMock()
    fake_nc.NetControlSession.return_value = sess
    with mock.patch.dict(sys.modules, {"net_control": fake_nc}):
        A._net_start()
    check("_net_start 启动会话", A._net_session is sess and sess.start.called)
    A._net_stop()
    check("_net_stop 停会话", A._net_session is None and sess.stop.called)
    A.NET_ENABLED = orig_enabled

# ============ 第五部分：_native_prewarm 预建链 ============
def test_native_prewarm():
    print("\n=== _native_prewarm（点名跳过/残留清理/距离远/常驻预建/临时短链/抢麦失败）===")
    # 点名中 → 跳过整轮
    _reset_announce_state()
    with mock.patch.object(A, "net_active", return_value=True), \
            mock.patch.object(A, "get_tts_file") as g:
        A._native_prewarm()
    check("预热点名中跳过", not g.called)

    # 残留会话清理（临时短链 → close + resume）
    _reset_announce_state()
    s = mock.MagicMock()
    A._native_session = s
    A._native_session_temp = True
    A._native_link = mock.MagicMock()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "_next_announce_time",
                              return_value=datetime.datetime.now() - datetime.timedelta(seconds=1)), \
            mock.patch.object(A, "get_tts_file", return_value="") as g, \
            mock.patch("announce.time.sleep", return_value=None):
        A._native_prewarm()
    check("残留临时链清理(close+resume)", s.close.called and A._native_link.resume.called)

    # 距离远（>70s）：只构建音频，不碰链路
    _reset_announce_state()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "_next_announce_time",
                              return_value=datetime.datetime.now() + datetime.timedelta(hours=2)), \
            mock.patch.object(A, "get_tts_file", return_value="mp3.mp3"), \
            mock.patch("announce.direct_announce.build_audio",
                       return_value=([b"pkt"], 10)) as ba, \
            mock.patch("announce.time.sleep", return_value=None):
        A._native_prewarm()
    check("距准点远→只构建音频", ba.called and A._native_prebuilt and A._native_session is None)

    # 临近 + 常驻链路健康检查 → 预建成功（ensure_session 返回会话）
    _reset_announce_state()
    A._native_link = mock.MagicMock()
    A._native_link.ensure_session.return_value = mock.MagicMock()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "_next_announce_time",
                              return_value=datetime.datetime.now() - datetime.timedelta(seconds=1)), \
            mock.patch.object(A, "get_tts_file", return_value="mp3.mp3"), \
            mock.patch("announce.direct_announce.build_audio",
                       return_value=([b"pkt"], 10)), \
            mock.patch("announce.time.sleep", return_value=None):
        A._native_prewarm()
    check("临近→常驻健康检查预建", A._native_session is not None and not A._native_session_temp)

    # 常驻不可用（ensure_session=None）→ 临时短链兜底
    _reset_announce_state()
    A._native_link = mock.MagicMock()
    A._native_link.ensure_session.return_value = None
    tmp = mock.MagicMock()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "_next_announce_time",
                              return_value=datetime.datetime.now() - datetime.timedelta(seconds=1)), \
            mock.patch.object(A, "get_tts_file", return_value="mp3.mp3"), \
            mock.patch("announce.direct_announce.build_audio", return_value=([b"pkt"], 10)), \
            mock.patch("announce.direct_announce.DirectAnnouncer", return_value=tmp), \
            mock.patch("announce.time.sleep", return_value=None):
        A._native_prewarm()
    check("常驻不可用→临时短链预建", A._native_session_temp and tmp.connect.called and tmp.take_mic.called)

    # 抢麦失败 → 清理会话
    _reset_announce_state()
    A._native_link = mock.MagicMock()
    A._native_link.ensure_session.return_value = None
    tmp = mock.MagicMock()
    tmp.take_mic.side_effect = Exception("mic busy")
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "_next_announce_time",
                              return_value=datetime.datetime.now() - datetime.timedelta(seconds=1)), \
            mock.patch.object(A, "get_tts_file", return_value="mp3.mp3"), \
            mock.patch("announce.direct_announce.build_audio", return_value=([b"pkt"], 10)), \
            mock.patch("announce.direct_announce.DirectAnnouncer", return_value=tmp), \
            mock.patch("announce.time.sleep", return_value=None):
        A._native_prewarm()
    check("抢麦失败→清理会话", A._native_session is None and A._native_link.resume.called)

# ============ 第六部分：播报核心 announce_task ============
def test_announce_task():
    print("\n=== announce_task（点名跳过/TTS失败/预建命中/兜底/异常）===")
    # 点名中 → 跳过 + 打点 _mark_net_skip
    _reset_announce_state()
    with mock.patch.object(A, "net_active", return_value=True):
        A.announce_task()
    check("announce_task 点名中跳过+打点", A._last_net_skip_ts > 0)

    # TTS 失败 → _mark_tts_fail + 快速跳场
    _reset_announce_state()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "get_tts_file", return_value=""):
        A.announce_task()
    check("announce_task TTS失败→打点跳场", A._last_tts_fail_ts > 0)

    # 预建命中 → _native_play + _mark_announce_ok + 常驻 release
    _reset_announce_state()
    link = mock.MagicMock()
    A._native_link = link
    s = mock.MagicMock()
    A._native_session = s
    A._native_prebuilt = ("mp3.mp3", [b"pkt"])
    A._native_session_temp = False
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "get_tts_file", return_value="mp3.mp3"), \
            mock.patch.object(A, "_native_play") as np_, \
            mock.patch.object(A, "_send_wechat", return_value=True):
        A.announce_task()
    check("预建命中→play+打点", np_.called and A._last_announce_ok > 0 and link.release.called)
    check("预建消费后清空", A._native_session is None and A._native_prebuilt is None)

    # 无预建 → _announce_native 兜底
    _reset_announce_state()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "get_tts_file", return_value="mp3.mp3"), \
            mock.patch.object(A, "_announce_native") as an:
        A.announce_task()
    check("无预建→现场兜底", an.called)

    # 播报异常 → 日志不崩
    _reset_announce_state()
    with mock.patch.object(A, "net_active", return_value=False), \
            mock.patch.object(A, "get_tts_file", side_effect=Exception("boom")):
        A.announce_task()
    check("announce_task 异常不崩", True)

# ============ 第七部分：_native_play / _announce_native ============
def test_play_and_native():
    print("\n=== _native_play / _announce_native ===")
    _reset_announce_state()
    s = mock.MagicMock()
    s.play.return_value = True
    A._native_play(s, [b"pkt"])
    check("_native_play 成功", True)
    s.play.return_value = False
    A._native_play(s, [b"pkt"])
    check("_native_play 失败打error（不抛）", True)

    # _announce_native：成功 → _mark_announce_ok；失败 → 不打点
    _reset_announce_state()
    with mock.patch("announce.direct_announce.announce_once", return_value=True):
        A._announce_native("mp3.mp3")
    check("_announce_native 成功→打点", A._last_announce_ok > 0)
    _reset_announce_state()
    _before = A._last_announce_ok
    with mock.patch("announce.direct_announce.announce_once", return_value=False):
        A._announce_native("mp3.mp3")
    check("_announce_native 失败→不打点", A._last_announce_ok == _before)
    # suspend/resume 保证调用
    A._native_link = mock.MagicMock()
    with mock.patch("announce.direct_announce.announce_once", return_value=True):
        A._announce_native("mp3.mp3")
    check("_announce_native 挂起/恢复常驻",
          A._native_link.suspend.called and A._native_link.resume.called)

# ============ 第八部分：杂项 ============
def test_misc():
    print("\n=== 杂项（trim_python_memory / _StdoutToLogger / WebhookHandler）===")
    A.trim_python_memory()
    check("trim_python_memory 正常不抛", True)
    with mock.patch("announce.ctypes.CDLL", side_effect=Exception("no libc")):
        A.trim_python_memory()
    check("trim_python_memory 非glibc兜底", True)

    w = A._StdoutToLogger()
    with mock.patch.object(A.logger, "info") as mi:
        w.write("abc\n"); w.write("\n"); w.flush()
    check("_StdoutToLogger 转发非空行", mi.called and "abc" in mi.call_args[0][0])

    # WeChatWebhookHandler.emit：构造 payload + 后台线程
    h = A.WeChatWebhookHandler("http://hook.invalid", level=logging.ERROR)
    rec = logging.LogRecord("t", logging.ERROR, "f", 1, "boom", None, None)
    with mock.patch("announce.threading.Thread") as mt:
        h.emit(rec)
    check("emit 起后台线程发送", mt.called and mt.call_args.kwargs.get("target") == h._send)
    # _send 成功/失败 → 不抛（urlopen mock）
    req = mock.MagicMock()
    with mock.patch("announce.urllib.request.urlopen") as uo:
        h._send(req)
    check("_send urlopen 成功不抛", uo.called)
    with mock.patch("announce.urllib.request.urlopen", side_effect=Exception("net")):
        h._send(req)
    check("_send 失败不抛", True)
    # emit 异常 → handleError 兜底
    with mock.patch.object(h, "handleError") as he, \
            mock.patch("announce.json.dumps", side_effect=Exception("ser")):
        h.emit(rec)
    check("emit 异常→handleError", he.called)

# ============ 第九部分：direct_announce 纯函数 ============
def test_da_pure():
    print("\n=== direct_announce 纯函数（dry_validate/trim_silence/parse_udp/build_audio）===")
    # dry_validate_mp3：长度阈值/异常
    long_pcm = b"\x00" * (48000 * 2 * 1)   # 1 秒
    with mock.patch("direct_announce.Mpg123Decoder") as md:
        md.return_value.decode.return_value = (long_pcm, 48000)
        check("dry_validate_mp3 足长→True", DA.dry_validate_mp3("x.mp3", min_seconds=0.5))
        md.return_value.decode.return_value = (b"\x00" * 100, 48000)
        check("dry_validate_mp3 不足→False", not DA.dry_validate_mp3("x.mp3", min_seconds=0.5))
        md.return_value.decode.side_effect = Exception("corrupt")
        check("dry_validate_mp3 异常→False", not DA.dry_validate_mp3("x.mp3"))

    # trim_silence：头尾静音裁剪 + 余量
    def _pcm_with_edges(lead=4000, tail=6000, body=8000):
        a = array.array('h')
        a.extend([0] * lead)
        a.extend([300] * body)
        a.extend([0] * tail)
        return a.tobytes()
    pcm = _pcm_with_edges()
    trimmed = DA.trim_silence(pcm, threshold=200, min_ms=50)
    keep = 48000 * 50 // 1000   # 2400
    n_samples = len(pcm) // 2
    check("trim_silence 头裁", len(trimmed) // 2 < n_samples, f"{(len(trimmed)//2)} vs {n_samples}")
    check("trim_silence 保留余量", len(trimmed) // 2 >= 2400 + 8000, len(trimmed) // 2)
    check("trim_silence 全静音→空", DA.trim_silence(b"\x00" * 10000, threshold=200) == b"")
    check("trim_silence 空输入", DA.trim_silence(b"", threshold=200) == b"")

    # parse_udp_voice_multi：多包串联/回声/截断/非语音
    def _vpkt(session, seq, opus_len=10):
        opus = b"\x01" * opus_len
        return b"\x20" + DA.m_varint_enc(session) + DA.m_varint_enc(seq) \
            + DA.m_varint_enc(opus_len) + opus
    p1, p2 = _vpkt(100, 200), _vpkt(100, 206)
    out = DA.parse_udp_voice_multi(p1 + p2)
    check("多包串联解析", len(out) == 2 and out[0]["seq"] == 200 and out[1]["seq"] == 206,
          str(out))
    check("回声过滤(own_session)", DA.parse_udp_voice_multi(p1 + p2, own_session=100) == [])
    check("非语音开头→空", DA.parse_udp_voice_multi(b"\x40\x01\x02") == [])
    trunc = p1[:-3]
    check("截断包→解析停止", len(DA.parse_udp_voice_multi(trunc)) <= 1)
    # parse_udp_voice 单包兼容
    v = DA.parse_udp_voice(p1)
    check("parse_udp_voice 取第一个", v and v["session"] == 100)
    check("parse_udp_voice 空→None", DA.parse_udp_voice(b"") is None)

    # build_audio：帧补零/6帧一包 185 字节
    n_frames = 13
    pcm_body = array.array('h', [300] * (960 * n_frames)).tobytes()   # 13 帧非静音
    with mock.patch("direct_announce.Mpg123Decoder") as md, \
            mock.patch("direct_announce.TRIM_SILENCE", False):
        md.return_value.decode.return_value = (pcm_body, 48000)
        packets, nf = DA.build_audio("x.mp3")
    check("build_audio 帧数正确", nf == n_frames, f"nf={nf}")
    check("build_audio 包数(13帧→3包)", len(packets) == 3, len(packets))
    check("build_audio 每包185字节", all(len(p) == 185 for p in packets),
          {len(p) for p in packets})
    check("build_audio 首包头=0x20", packets[0][0] == 0x20)

if __name__ == "__main__":
    test_text_time_pure()
    test_tts_cache()
    test_get_tts_file()
    test_prefill_and_prewarm_sched()
    test_native_link()
    test_native_prewarm()
    test_announce_task()
    test_play_and_native()
    test_misc()
    test_da_pure()
    print(f"\n结果: PASS={_OK} FAIL={_FAIL}")
    sys.exit(1 if _FAIL else 0)
