import os
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

# ====================== 配置项 ======================
# 滔滔链路账号：优先读环境变量（推荐 compose 的 environment 段配置），
# 也可直接改为你的账号密码常量
TALK_USERNAME = os.environ.get("TALK_USERNAME", "")
TALK_PASSWORD = os.environ.get("TALK_PASSWORD", "")
TTS_VOICE = "zh-CN-XiaoxiaoNeural"

# 播报内容配置
ANNOUNCE_TEMPLATE = "CQ CQ CQ，现在是{year}年{month}月{day}日，{weekday}，{hour}点{minute_text}。这里是咸阳市业余无线电中继台B阿九AB，本中继下行频率 4三9.625 兆赫，上行频率 4三2.575 兆赫，叉频 负 7.05 兆赫。单上行接入亚音为模拟 88.5 赫兹。请规范用频，保持信道畅通。完毕"
ANNOUNCE_TEMPLATE = "CQ CQ CQ，现在是{year}年{month}月{day}日，{weekday}，{hour}点{minute_text}。这里是咸阳市业余无线电中继台B阿九AB，本中继下行频率 4三9.6875 兆赫，上行频率 4三4.4875 兆赫，叉频 负 5.2 兆赫。单上行接入亚音为模拟 88.5 赫兹。请规范用频，保持信道畅通。完毕"
ANNOUNCE_START_HOUR = 7    # 每日首次播报 x:00
ANNOUNCE_END_HOUR = 22     # 每日最后一次播报 x:30

# 缓存与时序配置
CACHE_DIR = r"/app/tts_cache"
LOG_MAX_BYTES = 0.25 * 1024 * 1024  # 日志轮转：单文件最大 0.25MB
LOG_BACKUP_COUNT = 40              # 保留最近 40 个备份
CACHE_EXPIRE_DAYS = 2             # TTS 缓存过期天数
TTS_PREFILL_HOURS = 48            # TTS 蓄水池提前量：预合成未来该时长内所有播报时段的音频
NATIVE_PREP_LEAD = 2.5            # 直连预建链提前量：准点前该秒数建链+登录
                                  # （实测建链~1s，空闲仅~1s 无需保活）
NATIVE_MIC_LEAD = 0.5             # 抢麦提前量：准点前该秒数才发起抢麦（不提前占麦，
                                  # 避免频道长时间"说话中"静默）；抢麦响应~1.1s，
                                  # 首包≈准点+1.1s，UserTalking→首包 0.5s 官方时序由 play() 保证

# 企业微信 Webhook 推送配置
# Webhook 地址（企业微信群机器人）；为空时自动禁用告警推送
WECHAT_WEBHOOK_URL = os.environ.get("WECHAT_WEBHOOK_URL", "")
# 日志推送级别：WARNING=仅告警+错误, ERROR=仅错误
# 可选值：logging.INFO / logging.WARNING / logging.ERROR
WEBHOOK_LOG_LEVEL = logging.WARNING
# ======================================================

# 日志初始化（轮转日志，防止单文件撑满磁盘）
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
Path(r"/app/logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            r"/app/logs/daemon.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ====================== 企业微信 Webhook 日志推送 ======================
# 功能：当日志达到指定级别时，自动推送到企业微信群机器人
# 推送级别由配置项 WEBHOOK_LOG_LEVEL 控制（默认 WARNING）
# 发送在后台线程异步执行，不阻塞主流程，超时 5 秒
class WeChatWebhookHandler(logging.Handler):
    """将指定级别及以上的日志推送到企业微信群机器人"""

    def __init__(self, webhook_url: str, level=logging.ERROR):
        super().__init__(level=level)
        self.webhook_url = webhook_url

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            # 企业微信纯文本消息体（text 类型，直接显示在聊天中）
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
            # 非阻塞：在后台线程发送，避免卡住主流程
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
                # 记录推送结果到文件日志（不再触发 webhook，避免死循环）
                logging.getLogger(__name__).debug(f"Webhook推送结果: {result}")
        except Exception as e:
            logging.getLogger(__name__).debug(f"Webhook推送失败: {e}")


# 注册 Webhook Handler，推送级别由 WEBHOOK_LOG_LEVEL 控制
# 如需调整推送级别，修改上方 WEBHOOK_LOG_LEVEL 即可，无需改动以下代码
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


def get_tts_file(text: str, max_retries: int = 3, retry_delay: float = 5.0) -> str:
    """TTS合成，子线程隔离事件循环。含文件完整性校验 + 超时重试"""
    cache_path = tts_cache_path(text)

    # 缓存命中时校验文件大小，空文件视为损坏，删除后重新合成
    if cache_path.exists():
        if cache_path.stat().st_size > 0:
            return str(cache_path)
        logger.warning(f"TTS缓存文件为空（损坏），将重新合成: {cache_path.name}")
        cache_path.unlink()

    for attempt in range(1, max_retries + 1):
        logger.info(f"合成语音 (第{attempt}/{max_retries}次): {text}")
        try:
            # 捕获子线程内的真实异常（threading 不向外传播异常），
            # 否则失败原因被吞掉，无从判断是超时/403/DNS等问题
            syn_error = []

            def _syn_thread():
                async def _syn():
                    await edge_tts.Communicate(text, TTS_VOICE).save(str(cache_path))
                try:
                    # 内层超时比外层 join 超时短 2 秒，让取消先落地、线程必定退出：
                    # 否则 websocket 挂死时线程会带着事件循环和连接永久滞留进程内（内存泄漏）
                    asyncio.run(asyncio.wait_for(_syn(), timeout=28))
                except Exception as e:
                    syn_error.append(f"{type(e).__name__}: {e}")

            t = threading.Thread(target=_syn_thread, daemon=True)
            t.start()
            t.join(timeout=30)  # 最多等30秒（兜底安全网）

            if t.is_alive():
                logger.warning(f"TTS合成超时 (第{attempt}次)")
                # 清理可能写了一半的文件
                if cache_path.exists() and cache_path.stat().st_size == 0:
                    cache_path.unlink()
            elif cache_path.exists() and cache_path.stat().st_size > 0:
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
    # 按调度规则取下一场真实播报：播报时段外/跨天时自动指向次日首场，
    # 不再为不存在的时段（如 22:30 之后算出的 23:00）白做合成
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

    # 清理过期 TTS 缓存
    expire_threshold = now - datetime.timedelta(days=CACHE_EXPIRE_DAYS)
    cache_dir = Path(CACHE_DIR)
    removed = 0
    for f in cache_dir.glob("*.mp3"):
        if f.stat().st_mtime < expire_threshold.timestamp():
            f.unlink()
            removed += 1
    if removed:
        logger.info(f"清理过期TTS缓存: 删除 {removed} 个超过 {CACHE_EXPIRE_DAYS} 天的文件")


def get_upcoming_announce_times(now: datetime.datetime, hours: int = TTS_PREFILL_HOURS):
    """枚举 now 之后 hours 小时内的全部准点播报时刻"""
    end = now + datetime.timedelta(hours=hours)
    t = now.replace(minute=0, second=0, microsecond=0)
    slots = []
    while t <= end:
        if t > now and ANNOUNCE_START_HOUR <= t.hour <= ANNOUNCE_END_HOUR and t.minute in (0, 30):
            slots.append(t)
        t += datetime.timedelta(minutes=30)
    return slots


def tts_prefill_task():
    """TTS 蓄水池：预合成未来播报时段缺失的音频。

    播报文本完全由日期+时间决定，可提前计算。edge-tts 网络故障具有
    突发性（常持续数分钟到数小时），把合成提前到更早的时间窗口并
    每小时补充一次，准点播报就不再依赖播报时刻的网络状态；
    只有连续超过一天的网络中断才可能影响播报。
    """
    now = datetime.datetime.now()
    slots = get_upcoming_announce_times(now)
    synthesized, still_missing = 0, []
    for slot in slots:
        # 预热/播报任务入队时立即让位，绝不阻塞准点流程
        if not task_queue.empty():
            logger.info("TTS蓄水池：检测到待执行任务，本轮提前结束，剩余时段下次继续补充")
            break
        text = get_announce_text(slot)
        cache_path = tts_cache_path(text)
        if cache_path.exists() and cache_path.stat().st_size > 0:
            continue  # 已备好
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
    """主动归还内存给操作系统：回收循环引用 + glibc 碎片整理。

    每个播报周期都会创建/销毁大量临时对象（音频包缓冲、
    合成/webhook线程等），glibc 各线程 arena 的空闲内存默认不归还
    操作系统，长跑容器表现为 RSS 只涨不降的"疑似内存泄漏"。
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # 非 glibc 平台（如 musl）无此函数，忽略


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


# 预建链状态（预热任务写入，准点播报消费）
_native_session = None     # direct_announce.DirectAnnouncer：已登录+已抢麦的会话
_native_prebuilt = None    # (mp3路径, packets)：预构建的音频包
_native_session_temp = False  # 预建会话是否临时短链（播完 close；常驻的只 release）
_native_link = None        # direct_announce.PersistentAnnouncer：常驻直连链路


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
    2. 临近准点（<70s）：常驻链路健康检查（不健康自动重建）→ 准点前
       NATIVE_MIC_LEAD 才发起抢麦（不提前占麦）。常驻不可用时退化为临时短链
       （准点前 NATIVE_PREP_LEAD 建链），再不行留给准点现场流程兜底。
    启动场景（距准点尚远）只做 1。"""
    global _native_session, _native_prebuilt, _native_session_temp
    if _native_session:                      # 上次预热残留（播报未消费等异常），先清理
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
    if wait > 70:                            # 距准点尚远（服务刚启动），无需预建链
        return
    # 睡到准点前2.5s再动链路：期间常驻连接由守护线程正常 Ping 保活
    # （若提前独占，60s 无 Ping 可能被服务器当空闲连接踢掉）
    time.sleep(max(0, wait - NATIVE_PREP_LEAD))
    s = None
    if _native_link:
        s = _native_link.ensure_session()    # 健康检查/重建（常驻活着则瞬时完成）
    if s is not None:
        _native_session_temp = False
        logger.info("常驻链路就绪（已健康检查）")
    else:
        try:                                  # 二级兜底：立即建临时短链
            s = direct_announce.DirectAnnouncer(
                username=TALK_USERNAME, password=TALK_PASSWORD)
            with contextlib.redirect_stdout(_StdoutToLogger()):
                s.connect()                            # 连接+登录 ~1s
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
            time.sleep(mic_wait)                       # 睡到准点前0.5s才抢麦
        with contextlib.redirect_stdout(_StdoutToLogger()):
            s.take_mic()                               # 抢麦+UserTalking（响应~1.1s）
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
    """直连播报（现场完整流程，容错兜底）：编码→登录→抢麦→匀速发包→放麦→断开。
    抛出的异常由 announce_task 外层 except 统一记录并触发企业微信告警。"""
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
        # 预建会话+预构建音频均就绪（预热任务准点前0.5s已发起抢麦，此刻刚完成）
        # —— play() 内部再留 0.5s 建链间隔后发包，官方时序完整保留
        if _native_session and _native_prebuilt and _native_prebuilt[0] == tts_file:
            s, packets = _native_session, _native_prebuilt[1]
            temp = _native_session_temp
            _native_session = _native_prebuilt = None   # 先取走，防重入
            try:
                _native_play(s, packets)
            finally:
                if temp:                       # 临时短链：用完即弃
                    s.close()
                elif _native_link:             # 常驻链路：还给守护线程继续保活
                    _native_link.release()
            return
        _announce_native(tts_file)   # 容错兜底：现场完整流程（含 LEAD_DELAY）

    except Exception as e:
        logger.error(f"播报流程异常: {str(e)}", exc_info=True)


# ========== 调度任务分发函数 ==========
def schedule_announce():
    task_queue.put("announce")

def schedule_tts_prefill():
    task_queue.put("tts_prefill")

if __name__ == "__main__":
    try:
        # 交互模式：阻塞式，按回车播报，不进入调度
        if len(sys.argv) > 1 and sys.argv[1] in ("--interactive", "-i"):
            logger.info("=== 交互测试模式：按回车播报一次 ===")
            while True:
                input()
                announce_task()

        else:
            # 测试模式：连续播报指定次数后进入调度循环
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
                # 启动时执行一次预热（TTS 预合成 + 常驻链路启动）
                prewarm_task()

            # 后台调度器
            scheduler = BackgroundScheduler(timezone="Asia/Shanghai", daemon=True)
            # 预热任务一：XX:30 播报前一分钟（XX:29）预热，覆盖时段内每个半点
            scheduler.add_job(
                schedule_prewarm,
                "cron",
                hour=f"{ANNOUNCE_START_HOUR}-{ANNOUNCE_END_HOUR}",
                minute="29",
                second=0
            )
            # 预热任务二：XX:00 播报前一分钟（前一小时 XX:59）预热，覆盖时段内每个整点。
            # (START-1):59 保证每天首次播报前 TTS/音频/常驻链路都是新鲜就绪的；
            # END:59 不再预热（其后已无播报），最后一次播报结束后整夜只留常驻链路保活。
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
            # TTS 蓄水池：每小时补充预合成未来时段缺失的音频，
            # 让准点播报不依赖播报时刻的 edge-tts 网络状态
            scheduler.add_job(
                schedule_tts_prefill,
                "cron",
                minute="5",
                second=0
            )
            scheduler.start()

            # 启动即补充一次蓄水池（首次部署/缓存清空后尽快备好音频）
            task_queue.put("tts_prefill")

            # 常驻直连链路：后台 Ping 保活，播报前健康检查复用
            _native_link_start()

            logger.info(f"自动播报服务启动成功，每日 {ANNOUNCE_START_HOUR}:00 ~ {ANNOUNCE_END_HOUR}:30 每半小时播报一次")
            logger.info("客户端：协议直连 totalkd.allptt.com:59638（常驻链路）")
            logger.info(f"预热规则：XX:00 / XX:30 准时播报；每次播报前一分钟（XX:29 / XX:59）预合成TTS+预构建音频+预建链，每天首次播报由 {ANNOUNCE_START_HOUR - 1}:59 预热")

            # 主线程循环：队列串行执行所有任务
            while True:
                task = task_queue.get()
                try:
                    if task == "announce":
                        announce_task()
                        trim_python_memory()  # 回收播报产生的音频缓冲对象
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
