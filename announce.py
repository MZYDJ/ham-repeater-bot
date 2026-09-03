import sys
import time
import hashlib
import logging
import logging.handlers
import datetime
import gc
import ctypes
import asyncio
import threading
import queue
import json
import urllib.request
import urllib.error
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
import edge_tts
import contextlib
import direct_announce

# ====================== 配置项（v2：全部迁移至 config.json） ======================
# 配置来源优先级：环境变量 HAM_BOT_CONFIG 指定路径 > 脚本同目录 config.json
# direct_announce.load_config() 已在模块加载时解析；此处复用其 CFG 与 cfg_get。
CFG = direct_announce.CFG
CFG_PATH = direct_announce.CFG_PATH
def cfg_get(*path, default=None):
    return direct_announce.cfg_get(*path, default=default)

# 滔滔链路账号（来自配置 talk 段；与 direct_announce 同源）
TALK_USERNAME = cfg_get("talk", "username", default="")
TALK_PASSWORD = cfg_get("talk", "password", default="")

# TTS 配置
TTS_VOICE = cfg_get("tts", "voice", default="zh-CN-XiaoxiaoNeural")
TTS_TIMEOUT_INNER = cfg_get("tts", "timeout_inner", default=28)   # 内层协程超时（秒）
TTS_TIMEOUT_JOIN = cfg_get("tts", "timeout_join", default=30)     # 外层线程 join 超时（秒）
TTS_MAX_RETRIES = cfg_get("tts", "max_retries", default=3)
TTS_RETRY_DELAY = cfg_get("tts", "retry_delay", default=5.0)

# 播报内容配置
ANNOUNCE_TEMPLATE = cfg_get("announce", "template", default="")
ANNOUNCE_START_HOUR = cfg_get("announce", "start_hour", default=7)
ANNOUNCE_END_HOUR = cfg_get("announce", "end_hour", default=22)

# 缓存与时序配置
CACHE_DIR = cfg_get("paths", "cache_dir", default=r"/app/tts_cache")
LOG_DIR = cfg_get("paths", "log_dir", default=r"/app/logs")
LOG_MAX_BYTES = cfg_get("logging", "max_bytes", default=0.25 * 1024 * 1024)
LOG_BACKUP_COUNT = cfg_get("logging", "backup_count", default=40)
CACHE_EXPIRE_DAYS = cfg_get("tts", "cache_expire_days", default=2)
TTS_PREFILL_HOURS = cfg_get("tts", "prefill_hours", default=48)
NATIVE_PREP_LEAD = cfg_get("timing", "native_prep_lead", default=2.5)
NATIVE_MIC_LEAD = cfg_get("timing", "native_mic_lead", default=0.5)

# 企业微信 Webhook 推送配置（为空时自动禁用告警推送）
WECHAT_WEBHOOK_URL = cfg_get("notify", "webhook_url", default="")
_LEVEL_STR = cfg_get("notify", "webhook_log_level", default="WARNING")
WEBHOOK_LOG_LEVEL = getattr(logging, _LEVEL_STR.upper(), logging.WARNING)
# ======================================================

# 日志初始化（轮转日志，防止单文件撑满磁盘）
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            str(Path(LOG_DIR) / "daemon.log"),
            maxBytes=int(LOG_MAX_BYTES),
            backupCount=int(LOG_BACKUP_COUNT),
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ====================== 企业微信 Webhook 日志推送 ======================
# 功能：当日志达到指定级别时，自动推送到企业微信群机器人
# 推送级别由配置项 notify.webhook_log_level 控制（默认 WARNING）
# 发送在后台线程异步执行，不阻塞主流程，超时 5 秒
class WeChatWebhookHandler(logging.Handler):
    """将指定级别及以上的日志推送到企业微信群机器人"""

    def __init__(self, webhook_url: str, level=logging.ERROR):
        super().__init__(level=level)
        self.webhook_url = webhook_url

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            payload = {
                "msgtype": "text",
                "text": {
                    "content": f"[播报服务 {record.levelname}] {msg}"
                }
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            threading.Thread(
                target=self._send, args=(req,), daemon=True
            ).start()
        except Exception:
            self.handleError(record)

    @staticmethod
    def _send(req):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = resp.read().decode("utf-8")
                logging.getLogger(__name__).debug(f"Webhook推送结果: {result}")
        except Exception as e:
            logging.getLogger(__name__).debug(f"Webhook推送失败: {e}")


if WECHAT_WEBHOOK_URL:
    _wechat_handler = WeChatWebhookHandler(WECHAT_WEBHOOK_URL, level=WEBHOOK_LOG_LEVEL)
    _wechat_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(_wechat_handler)
# ======================================================================

task_queue = queue.Queue()
WEEKDAY_MAP = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

def format_minute_text(minute: int) -> str:
    if minute == 0:
        return "整"
    elif minute == 30:
        return "三十"
    else:
        return f"{minute}分"


def get_announce_text(now: datetime.datetime) -> str:
    return ANNOUNCE_TEMPLATE.format(
        year=now.year,
        month=now.month,
        day=now.day,
        weekday=WEEKDAY_MAP[now.weekday()],
        hour=now.hour,
        minute_text=format_minute_text(now.minute)
    )


def tts_cache_path(text: str) -> Path:
    """TTS 缓存文件路径（以播报文本 md5 为键）"""
    return Path(CACHE_DIR) / f"{hashlib.md5(text.encode('utf-8')).hexdigest()}.mp3"


def get_tts_file(text: str, max_retries: int = None, retry_delay: float = None) -> str:
    """TTS合成，子线程隔离事件循环。含文件完整性校验（libmpg123 dry-run）+ 超时重试"""
    if max_retries is None:
        max_retries = TTS_MAX_RETRIES
    if retry_delay is None:
        retry_delay = TTS_RETRY_DELAY
    cache_path = tts_cache_path(text)

    if cache_path.exists():
        if direct_announce.dry_validate_mp3(cache_path):
            return str(cache_path)
        logger.warning(f"TTS缓存文件损坏（dry-validate失败），将重新合成: {cache_path.name}")
        cache_path.unlink()

    for attempt in range(1, max_retries + 1):
        logger.info(f"合成语音 (第{attempt}/{max_retries}次): {text}")
        try:
            syn_error = []

            def _syn_thread():
                async def _syn():
                    await edge_tts.Communicate(text, TTS_VOICE).save(str(cache_path))
                try:
                    asyncio.run(asyncio.wait_for(_syn(), timeout=TTS_TIMEOUT_INNER))
                except Exception as e:
                    syn_error.append(f"{type(e).__name__}: {e}")

            t = threading.Thread(target=_syn_thread, daemon=True)
            t.start()
            t.join(timeout=TTS_TIMEOUT_JOIN)

            if t.is_alive():
                logger.warning(f"TTS合成超时 (第{attempt}次)")
                if cache_path.exists() and cache_path.stat().st_size == 0:
                    cache_path.unlink()
            elif cache_path.exists() and direct_announce.dry_validate_mp3(cache_path):
                return str(cache_path)
            else:
                reason = syn_error[0] if syn_error else "无异常抛出但文件未生成"
                logger.warning(f"TTS合成失败或文件为空 (第{attempt}次): {reason}")
        except Exception as e:
            logger.warning(f"TTS合成异常 (第{attempt}次): {e}")

        if attempt < max_retries:
            logger.info(f"等待 {retry_delay} 秒后重试...")
            time.sleep(retry_delay)

    logger.error(f"TTS合成最终失败，已重试 {max_retries} 次")
    return ""


def prepare_next_tts():
    """预合成下一次准点播报的TTS音频，供播报时直接使用。顺便清理过期缓存。"""
    now = datetime.datetime.now()
    slots = get_upcoming_announce_times(now)
    if not slots:
        return
    next_time = slots[0]

    announce_text = get_announce_text(next_time)
    logger.info(f"预合成下一次播报音频: {next_time.strftime('%H:%M')}")
    tts_file = get_tts_file(announce_text)
    if tts_file:
        logger.info(f"预合成完成: {Path(tts_file).name}")
    else:
        logger.warning("预合成失败，播报时将重试")

    expire_threshold = now - datetime.timedelta(days=CACHE_EXPIRE_DAYS)
    cache_dir = Path(CACHE_DIR)
    removed = 0
    for f in cache_dir.glob("*.mp3"):
        if f.stat().st_mtime < expire_threshold.timestamp():
            f.unlink()
            removed += 1
    if removed:
        logger.info(f"清理过期TTS缓存: 删除 {removed} 个超过 {CACHE_EXPIRE_DAYS} 天的文件")


def get_upcoming_announce_times(now: datetime.datetime, hours: int = None):
    """枚举 now 之后 hours 小时内的全部准点播报时刻"""
    if hours is None:
        hours = TTS_PREFILL_HOURS
    end = now + datetime.timedelta(hours=hours)
    t = now.replace(minute=0, second=0, microsecond=0)
    slots = []
    while t <= end:
        if t > now and ANNOUNCE_START_HOUR <= t.hour <= ANNOUNCE_END_HOUR and t.minute in (0, 30):
            slots.append(t)
        t += datetime.timedelta(minutes=30)
    return slots


def tts_prefill_task():
    """TTS 蓄水池：预合成未来播报时段缺失的音频。"""
    now = datetime.datetime.now()
    slots = get_upcoming_announce_times(now)
    synthesized, still_missing = 0, []
    for slot in slots:
        if not task_queue.empty():
            logger.info("TTS蓄水池：检测到待执行任务，本轮提前结束，剩余时段下次继续补充")
            break
        text = get_announce_text(slot)
        cache_path = tts_cache_path(text)
        if cache_path.exists() and cache_path.stat().st_size > 0:
            continue
        if get_tts_file(text):
            synthesized += 1
        else:
            still_missing.append(slot.strftime("%m-%d %H:%M"))

    if synthesized or still_missing:
        summary = f"TTS蓄水池：本轮新合成 {synthesized} 个"
        if still_missing:
            preview = "、".join(still_missing[:5]) + ("..." if len(still_missing) > 5 else "")
            summary += f"，剩余缺口 {len(still_missing)} 个（{preview}），将持续重试"
        logger.info(summary)
        if still_missing:
            logger.warning(f"TTS蓄水池仍有 {len(still_missing)} 个时段未备好，若到播报时仍未成功将现场重试")


def trim_python_memory():
    """主动归还内存给操作系统：回收循环引用 + glibc 碎片整理。"""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def prewarm_task():
    """播报前一分钟预热：预合成TTS + 预构建音频包 + 临近准点预建链抢麦"""
    logger.info("预热：预合成TTS+预构建音频+预建链")
    prepare_next_tts()
    _native_prewarm()


def schedule_prewarm():
    """定时入队：播报前一分钟预热"""
    task_queue.put("prewarm")


# ====================== 直连模式播报（native） ======================
class _StdoutToLogger:
    """把 direct_announce 的 print 诊断重定向进 logger（进 daemon.log + 企业微信告警链路）"""
    def write(self, s):
        s = s.rstrip()
        if s:
            logger.info("[直连] " + s)

    def flush(self):
        pass


_native_session = None
_native_prebuilt = None
_native_session_temp = False
_native_link = None


def _native_link_event(kind, detail):
    """常驻链路事件回调（企业微信告警链路复用 logger 级别）"""
    if kind == "removed":
        logger.error(f"[常驻链路] {detail}，退避后自动重连（若正在使用手机属正常互踢）")
    elif kind == "reconnected":
        logger.info(f"[常驻链路] {detail}")
    else:
        logger.warning(f"[常驻链路] {detail}")


def _native_link_start():
    """启动常驻直连链路（后台 Ping 保活+下行泵）。失败仅告警，播报走兜底。"""
    global _native_link
    if _native_link:
        return
    try:
        link = direct_announce.PersistentAnnouncer(
            username=TALK_USERNAME, password=TALK_PASSWORD,
            on_event=_native_link_event)
        link.start()
        _native_link = link
        logger.info("常驻直连链路已建立（后台 Ping 保活，服务器画像=长在线用户）")
    except Exception as e:
        logger.warning(f"常驻链路启动失败（播报时现场建链兜底）: {e}")


def _next_announce_time(now: datetime.datetime) -> datetime.datetime:
    """下一个准点播报时刻（minute ∈ {0,30}）"""
    t = now.replace(second=0, microsecond=0)
    if t.minute < 30:
        return t.replace(minute=30)
    return (t + datetime.timedelta(hours=1)).replace(minute=0)


def _native_prewarm():
    """预热阶段（XX:29/XX:59 触发）：
    1. 预构建下一准点的音频包（省去准点后 ~2.3s 解码编码）
    2. 临近准点（<70s）：常驻链路健康检查 → 准点前 NATIVE_MIC_LEAD 才发起抢麦。"""
    global _native_session, _native_prebuilt, _native_session_temp
    if _native_session:
        if _native_session_temp:
            _native_session.close()
        elif _native_link:
            _native_link.release()
        _native_session = None
    nxt = _next_announce_time(datetime.datetime.now())
    try:
        mp3 = get_tts_file(get_announce_text(nxt))
        if mp3:
            packets, n = direct_announce.build_audio(mp3)
            _native_prebuilt = (mp3, packets)
            logger.info(f"音频预构建完成: {n}帧 {len(packets)}包（{nxt:%H:%M} 播报用）")
    except Exception as e:
        logger.warning(f"音频预构建失败（准点现场构建兜底）: {e}")
        _native_prebuilt = None

    wait = (nxt - datetime.datetime.now()).total_seconds()
    if wait > 70:
        return
    time.sleep(max(0, wait - NATIVE_PREP_LEAD))
    s = None
    if _native_link:
        s = _native_link.ensure_session()
    if s is not None:
        _native_session_temp = False
        logger.info("常驻链路就绪（已健康检查）")
    else:
        try:
            s = direct_announce.DirectAnnouncer(
                username=TALK_USERNAME, password=TALK_PASSWORD)
            with contextlib.redirect_stdout(_StdoutToLogger()):
                s.connect()
            _native_session_temp = True
            logger.info("临时短链预建完成（常驻链路不可用）")
        except Exception as e:
            logger.warning(f"临时短链预建失败（准点现场流程兜底）: {e}")
            s = None
    if s is None:
        return
    _native_session = s
    try:
        mic_wait = (nxt - datetime.datetime.now()).total_seconds() - NATIVE_MIC_LEAD
        if mic_wait > 0:
            time.sleep(mic_wait)
        with contextlib.redirect_stdout(_StdoutToLogger()):
            s.take_mic()
        logger.info(f"已抢麦，待 {nxt:%H:%M} 准点发包（play 再留 0.5s 建链间隔）")
    except Exception as e:
        logger.warning(f"抢麦失败（准点现场流程兜底）: {e}")
        if _native_session_temp:
            s.close()
        elif _native_link:
            _native_link.release()
        _native_session = None


def _native_play(session, packets):
    """消费预建会话发包（此刻即准点整）。异常抛给 announce_task 外层统一告警。"""
    t0 = time.time()
    with contextlib.redirect_stdout(_StdoutToLogger()):
        ok = session.play(packets)
    if ok:
        logger.info(f"直连播报完成（预建链），耗时 {time.time() - t0:.1f} 秒")
    else:
        logger.error("直连播报返回失败")


def _announce_native(tts_file: str):
    """直连播报（现场完整流程，容错兜底）。"""
    t0 = time.time()
    with contextlib.redirect_stdout(_StdoutToLogger()):
        ok = direct_announce.announce_once(
            tts_file, username=TALK_USERNAME, password=TALK_PASSWORD)
    if ok:
        logger.info(f"直连播报完成，耗时 {time.time() - t0:.1f} 秒")
    else:
        logger.error("直连播报返回失败")


def announce_task():
    """播报核心逻辑，仅主线程执行"""
    now = datetime.datetime.now()
    announce_text = get_announce_text(now)
    logger.info(f"触发定时播报: {announce_text}")

    try:
        global _native_session, _native_prebuilt, _native_session_temp
        tts_file = get_tts_file(announce_text)
        if not tts_file:
            logger.error("TTS音频文件无效，跳过本次播报")
            return
        if _native_session and _native_prebuilt and _native_prebuilt[0] == tts_file:
            s, packets = _native_session, _native_prebuilt[1]
            temp = _native_session_temp
            _native_session = _native_prebuilt = None
            try:
                _native_play(s, packets)
            finally:
                if temp:
                    s.close()
                elif _native_link:
                    _native_link.release()
            return
        _announce_native(tts_file)

    except Exception as e:
        logger.error(f"播报流程异常: {str(e)}", exc_info=True)


# ========== 调度任务分发函数 ==========
def schedule_announce():
    task_queue.put("announce")

def schedule_tts_prefill():
    task_queue.put("tts_prefill")

if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] in ("--interactive", "-i"):
            logger.info("=== 交互测试模式：按回车播报一次 ===")
            while True:
                input()
                announce_task()

        else:
            if len(sys.argv) > 1 and sys.argv[1] in ("--test", "-t"):
                repeat = 1
                if len(sys.argv) > 2:
                    try:
                        repeat = int(sys.argv[2])
                        if repeat < 1:
                            raise ValueError
                    except ValueError:
                        logger.error(f"无效的次数参数: {sys.argv[2]}，应为正整数")
                        sys.exit(1)
                logger.info(f"=== 测试模式：连续播报 {repeat} 次 ===")
                for i in range(1, repeat + 1):
                    logger.info(f"--- 第 {i}/{repeat} 次播报 ---")
                    announce_task()
                    if i < repeat:
                        time.sleep(2)
                logger.info(f"测试完成，共播报 {repeat} 次，进入正常调度模式")
                prepare_next_tts()
            else:
                prewarm_task()

            scheduler = BackgroundScheduler(timezone="Asia/Shanghai", daemon=True)
            scheduler.add_job(
                schedule_prewarm,
                "cron",
                hour=f"{ANNOUNCE_START_HOUR}-{ANNOUNCE_END_HOUR}",
                minute="29",
                second=0
            )
            scheduler.add_job(
                schedule_prewarm,
                "cron",
                hour=f"{ANNOUNCE_START_HOUR - 1}-{ANNOUNCE_END_HOUR - 1}",
                minute="59",
                second=0
            )
            scheduler.add_job(
                schedule_announce,
                "cron",
                hour=f"{ANNOUNCE_START_HOUR}-{ANNOUNCE_END_HOUR}",
                minute="0,30",
                second=0
            )
            scheduler.add_job(
                schedule_tts_prefill,
                "cron",
                minute="5",
                second=0
            )
            scheduler.start()

            task_queue.put("tts_prefill")
            _native_link_start()

            logger.info(f"自动播报服务启动成功，每日 {ANNOUNCE_START_HOUR}:00 ~ {ANNOUNCE_END_HOUR}:30 每半小时播报一次")
            logger.info(f"配置文件: {CFG_PATH}")
            logger.info("客户端：协议直连 totalkd.allptt.com:59638（常驻链路）")
            logger.info(f"预热规则：XX:00 / XX:30 准时播报；每次播报前一分钟（XX:29 / XX:59）预合成TTS+预构建音频+预建链，每天首次播报由 {ANNOUNCE_START_HOUR - 1}:59 预热")

            while True:
                task = task_queue.get()
                try:
                    if task == "announce":
                        announce_task()
                        trim_python_memory()
                    elif task == "prewarm":
                        prewarm_task()
                    elif task == "tts_prefill":
                        tts_prefill_task()
                except Exception as e:
                    logger.error(f"队列任务执行异常 task={task}: {str(e)}", exc_info=True)
                finally:
                    task_queue.task_done()

    except KeyboardInterrupt:
        logger.info("接收到停止信号，正在清理资源...")
    finally:
        if _native_link:
            _native_link.stop()
        logger.info("服务已停止")
