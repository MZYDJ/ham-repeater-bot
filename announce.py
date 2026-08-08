import sys
import time
import hashlib
import logging
import logging.handlers
import datetime
import asyncio
import threading
import queue
import json
import urllib.request
import urllib.error
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from playwright.sync_api import sync_playwright
import edge_tts
import base64

# ====================== 用户必填配置 ======================
# 以下配置项必须根据你的实际情况修改，否则服务无法正常运行

TALK_USERNAME = "YOUR_TALK_USERNAME"            # 滔滔链路登录账号
TALK_PASSWORD = "YOUR_TALK_PASSWORD"            # 滔滔链路登录密码

# 播报内容模板，根据你的中继台信息修改（呼号、频率、亚音等）
# 可用变量：{year} {month} {day} {weekday} {hour} {minute_text}
ANNOUNCE_TEMPLATE = "CQ CQ CQ，现在是{year}年{month}月{day}日，{weekday}，{hour}点{minute_text}。这里是YOUR_CALLSIGN，本中继下行频率 XXX.XXX 兆赫，上行频率 XXX.XXX 兆赫，叉频 负 X.XX 兆赫。单上行接入亚音为模拟 XXX.X 赫兹。请规范用频，保持信道畅通。完毕"

# 企业微信群机器人 Webhook（可选，不需要告警可留空）
WECHAT_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_WEBHOOK_KEY"

# ==========================================================


# ====================== 播报调度参数 ======================
# 控制播报的时间范围和频率，按需调整

ANNOUNCE_START_HOUR = 6          # 每日起始播报时（24h 制），例如 6 表示早 6 点开始
ANNOUNCE_END_HOUR = 22           # 每日结束播报时（24h 制），例如 22 表示晚 10 点后停止
                                 # 播报频率：每半小时一次（XX:00 和 XX:30）

# ==========================================================


# ====================== 高级配置（一般无需修改） ======================
# 以下参数已有合理默认值，仅在需要微调时修改

# --- 滔滔链路页面 ---
TALK_URL = "https://totalkd.allptt.com:1443/"  # 滔滔链路 Web 端地址（平台更新时可能变更）
PTT_SELECTOR = "#imagePtt_div"                  # PTT 按钮 CSS 选择器（页面改版时需更新）
LOGIN_BTN_SELECTOR = "#loginUI > div.container > div > div > div > form > div:nth-child(7) > div > div.col-xs-12 > input"  # 登录按钮 CSS 选择器

# --- TTS 语音合成 ---
TTS_VOICE = "zh-CN-XiaoxiaoNeural"             # Edge-TTS 语音角色（可选：zh-CN-YunxiNeural 等）
TTS_SYNTH_TIMEOUT = 30                          # 单次合成超时（秒）
TTS_MAX_RETRIES = 3                             # 合成失败最大重试次数
TTS_RETRY_DELAY = 5.0                           # 重试间隔（秒）
CACHE_EXPIRE_DAYS = 7                           # TTS 缓存文件过期天数，过期自动清理

# --- 音频链路 ---
AUDIO_SAMPLE_RATE = 24000       # 虚拟麦克风采样率（Hz），需与滔滔链路音频参数匹配
MIC_GAIN = 1.5                  # 虚拟麦克风播报音量增益，>1 放大，<1 缩小
LEVEL_CHECK_THRESHOLD = 10      # 电平检测通过阈值（0-255），低于此值视为无音频输出
LEVEL_CHECK_ROUNDS = 5          # 电平检测最大轮数，连续检测均低于阈值则告警
PTT_BUFFER_OFFSET = 0.6         # PTT 提前释放时间（秒），抵消 WebRTC 音频缓冲延迟

# --- PTT 与时序 ---
PTT_PRESS_DELAY = 800           # PTT 按下后等待时间（ms），确保中继台已响应
REFRESH_WAIT_SEC = 6            # 页面刷新后等待时间（秒），让音频链路和 WebSocket 稳定
PAGE_LOAD_TIMEOUT = 15000       # 页面加载超时（ms）

# --- 浏览器 ---
HEADLESS_MODE = True            # 浏览器无头模式，True=无界面运行，False=显示浏览器窗口（调试用）
USER_DATA_DIR = r"/app/edge_user_data"  # 浏览器数据目录（保存登录态，勿手动清理）

# --- 存储路径 ---
CACHE_DIR = r"/app/tts_cache"   # TTS 缓存目录
LOG_DIR = r"/app/logs"          # 日志目录

# --- 日志 ---
LOG_MAX_BYTES = 0.25 * 1024 * 1024  # 单个日志文件最大大小（字节），达到后自动轮转
LOG_BACKUP_COUNT = 40               # 保留的历史日志文件数量

# --- 企业微信告警级别 ---
# 可选值：logging.INFO / logging.WARNING / logging.ERROR
WEBHOOK_LOG_LEVEL = logging.WARNING  # WARNING=仅告警和错误推送，ERROR=仅错误推送

# ==================================================================

# 日志初始化（轮转日志，防止单文件撑满磁盘）
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            str(Path(LOG_DIR) / "daemon.log"),
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
_wechat_handler = WeChatWebhookHandler(WECHAT_WEBHOOK_URL, level=WEBHOOK_LOG_LEVEL)
_wechat_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
logger.addHandler(_wechat_handler)
# ======================================================================

# 全局对象
page = None
playwright_instance = None
context = None
task_queue = queue.Queue()
WEEKDAY_MAP = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

def setup_virtual_mic(context):
    """
    稳定运行版：永久保活上下文 + 真实电平检测
    解决长时间闲置后首次播报无声的问题
    """
    js_template = """
    (() => {
        // ========== 播报期间主线程开销抑制 ==========
        window.__playbackActive = false;
        const _origLog = console.log.bind(console);
        const _origWarn = console.warn.bind(console);
        const _origDebug = console.debug.bind(console);
        console.log = function(...args) {
            if (!window.__playbackActive) _origLog(...args);
        };
        console.warn = function(...args) {
            if (!window.__playbackActive) _origWarn(...args);
        };
        console.debug = function(...args) {
            if (!window.__playbackActive) _origDebug(...args);
        };

        // ========== 全局 AudioContext 接管与唤醒 ==========
        const _OriginalAudioContext = window.AudioContext || window.webkitAudioContext;
        const allAudioContexts = new Set();

        window.AudioContext = function(...args) {
            const ctx = new _OriginalAudioContext(...args);
            allAudioContexts.add(ctx);
            return ctx;
        };
        window.AudioContext.prototype = _OriginalAudioContext.prototype;
        if (window.webkitAudioContext) {
            window.webkitAudioContext = window.AudioContext;
        }

        function resumeAllContexts() {
            if (window.__playbackActive) return; // 播报期间跳过，减少主线程开销
            allAudioContexts.forEach(ctx => {
                if (ctx.state === 'suspended') ctx.resume();
            });
        }

        document.addEventListener('mousedown', () => { try { resumeAllContexts(); } catch(e) {} }, true);
        setInterval(resumeAllContexts, 1000);

        // ========== 全局虚拟麦克风（保活版） ==========
        let virtualCtx = null;
        let virtualStream = null;
        let gainNode = null;       // 主音量节点
        let analyser = null;       // 真实电平检测
        let sourceNode = null;     // 播报音频源
        let audioBuffer = null;
        const mainGain = __MIC_GAIN__;  // 播报音量（由配置项 MIC_GAIN 控制）

        function ensureVirtualMic() {
            if (virtualStream) return virtualStream;

            virtualCtx = new _OriginalAudioContext({ sampleRate: __AUDIO_SAMPLE_RATE__ });
            allAudioContexts.add(virtualCtx);

            // 输出流目标
            const dest = virtualCtx.createMediaStreamDestination();
            virtualStream = dest.stream;

            // 主增益节点
            gainNode = virtualCtx.createGain();
            gainNode.gain.value = mainGain;
            gainNode.connect(dest);

            // 真实电平分析器
            analyser = virtualCtx.createAnalyser();
            analyser.fftSize = 256;
            gainNode.connect(analyser);

            // 启用音频轨道（track 一旦启用不会被浏览器自动关闭，无需定时重复设置）
            virtualStream.getAudioTracks().forEach(track => {
                track.enabled = true;
            });
            
            console.log('[虚拟麦] 初始化完成');
            return virtualStream;
        }

        // ========== 播放控制接口 ==========
        async function loadAudio(base64Str) {
            ensureVirtualMic();
            
            const binaryStr = atob(base64Str);
            const bytes = new Uint8Array(binaryStr.length);
            for (let i = 0; i < binaryStr.length; i++) {
                bytes[i] = binaryStr.charCodeAt(i);
            }
            audioBuffer = await virtualCtx.decodeAudioData(bytes.buffer);
            console.log(`[虚拟麦] 音频加载完成：时长${audioBuffer.duration.toFixed(2)}s`);
            return audioBuffer.duration;
        }

        function play() {
            if (!audioBuffer) throw new Error('音频未加载');
            ensureVirtualMic();
            resumeAllContexts();

            // 停止上一次播报
            stop();

            // 创建播报源，接入主增益链路
            sourceNode = virtualCtx.createBufferSource();
            sourceNode.buffer = audioBuffer;
            sourceNode.connect(gainNode);
            sourceNode.start(0);

            sourceNode.onended = () => {
                console.log('[虚拟麦] 播报音频播放结束');
                sourceNode = null;
            };

            console.log('[虚拟麦] 开始播报');
            return audioBuffer.duration;
        }

        function stop() {
            if (sourceNode) {
                try { sourceNode.stop(); } catch(e) {}
                try { sourceNode.disconnect(); } catch(e) {}
                sourceNode = null;
            }
        }

        // 真实电平检测（0-255）
        function getLevel() {
            if (!analyser) return 0;
            const data = new Uint8Array(analyser.frequencyBinCount);
            analyser.getByteFrequencyData(data);
            return data.reduce((a,b) => a+b, 0) / data.length;
        }

        // ========== 劫持 ScriptProcessorNode，强制加大 buffer 减少回调频率 ==========
        // 网站用 960 采样点（40ms/次@24kHz=25次/秒），改为 8192（341ms/次=3次/秒）
        function _patchScriptProcessor(proto) {
            // 劫持 createScriptProcessor（标准 API）
            const _orig = proto.createScriptProcessor;
            if (_orig) {
                proto.createScriptProcessor = function(bufferSize, numberOfInputChannels, numberOfOutputChannels) {
                    const origBuf = bufferSize;
                    bufferSize = Math.max(bufferSize, 16384);
                    if (bufferSize !== origBuf) {
                        _origLog(`[优化] ScriptProcessorNode buffer: ${origBuf} → ${bufferSize}`);
                    }
                    return _orig.call(this, bufferSize, numberOfInputChannels, numberOfOutputChannels);
                };
            }
            // 劫持 createJavaScriptNode（旧版 API，部分网站仍在用）
            const _origJ = proto.createJavaScriptNode;
            if (_origJ) {
                proto.createJavaScriptNode = function(bufferSize, numberOfInputChannels, numberOfOutputChannels) {
                    const origBuf = bufferSize;
                    bufferSize = Math.max(bufferSize, 16384);
                    if (bufferSize !== origBuf) {
                        _origLog(`[优化] JavaScriptNode buffer: ${origBuf} → ${bufferSize}`);
                    }
                    return _origJ.call(this, bufferSize, numberOfInputChannels, numberOfOutputChannels);
                };
            }
        }
        _patchScriptProcessor(_OriginalAudioContext.prototype);
        _patchScriptProcessor(window.AudioContext.prototype);
        _origLog(`[优化] ScriptProcessorNode buffer 劫持已注入 (min=16384)`);

        // ========== 劫持麦克风 API ==========
        const _origGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        const _origEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);

        navigator.mediaDevices.getUserMedia = async function(constraints) {
            if (constraints?.audio) {
                const stream = ensureVirtualMic();
                resumeAllContexts();
                return stream;
            }
            return _origGetUserMedia(constraints);
        };

        navigator.mediaDevices.enumerateDevices = async function() {
            const list = await _origEnumerate();
            const filtered = list.filter(d => d.kind !== 'audioinput');
            filtered.push(
                { deviceId: 'default', label: 'Virtual Microphone', kind: 'audioinput', groupId: 'virtual' },
                { deviceId: 'virtual-mic-0', label: 'Virtual Microphone', kind: 'audioinput', groupId: 'virtual' }
            );
            return filtered;
        };

        // 强制确保 MediaStream track 启用（浏览器重启后 track 可能被自动 disable）
        function ensureTrackEnabled() {
            if (!virtualStream) return false;
            const tracks = virtualStream.getAudioTracks();
            tracks.forEach(t => { t.enabled = true; });
            return tracks.length > 0 && tracks.every(t => t.enabled);
        }

        window.__virtualMic = { loadAudio, play, stop, getLevel, ensureTrackEnabled };
    })();
    """
    # 将 Python 配置注入 JS 模板
    js_code = js_template.replace("__MIC_GAIN__", str(MIC_GAIN)).replace("__AUDIO_SAMPLE_RATE__", str(AUDIO_SAMPLE_RATE))
    context.add_init_script(js_code)

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


def get_tts_file(text: str) -> str:
    """TTS合成，子线程隔离事件循环。含文件完整性校验 + 超时重试"""
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    cache_path = Path(CACHE_DIR) / f"{text_hash}.mp3"

    # 缓存命中时校验文件大小，空文件视为损坏，删除后重新合成
    if cache_path.exists():
        if cache_path.stat().st_size > 0:
            return str(cache_path)
        logger.warning(f"TTS缓存文件为空（损坏），将重新合成: {cache_path.name}")
        cache_path.unlink()

    for attempt in range(1, TTS_MAX_RETRIES + 1):
        logger.info(f"合成语音 (第{attempt}/{TTS_MAX_RETRIES}次): {text}")
        try:
            def _syn_thread():
                async def _syn():
                    await edge_tts.Communicate(text, TTS_VOICE).save(str(cache_path))
                asyncio.run(_syn())

            t = threading.Thread(target=_syn_thread)
            t.start()
            t.join(timeout=TTS_SYNTH_TIMEOUT)

            if t.is_alive():
                logger.warning(f"TTS合成超时 (第{attempt}次)")
                # 清理可能写了一半的文件
                if cache_path.exists() and cache_path.stat().st_size == 0:
                    cache_path.unlink()
            elif cache_path.exists() and cache_path.stat().st_size > 0:
                return str(cache_path)
            else:
                logger.warning(f"TTS合成失败或文件为空 (第{attempt}次)")
        except Exception as e:
            logger.warning(f"TTS合成异常 (第{attempt}次): {e}")

        if attempt < TTS_MAX_RETRIES:
            logger.info(f"等待 {TTS_RETRY_DELAY} 秒后重试...")
            time.sleep(TTS_RETRY_DELAY)

    logger.error(f"TTS合成最终失败，已重试 {TTS_MAX_RETRIES} 次")
    return ""


def prepare_next_tts():
    """预合成下一次准点播报的TTS音频，供播报时直接使用。顺便清理过期缓存。"""
    now = datetime.datetime.now()
    if now.minute < 30:
        # XX:29 预刷新 → 下一次播报是 XX:30
        next_time = now.replace(minute=30, second=0, microsecond=0)
    else:
        # XX:59 预刷新 → 下一次播报是 XX+1:00
        next_hour = now + datetime.timedelta(hours=1)
        next_time = next_hour.replace(minute=0, second=0, microsecond=0)

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


def is_logged_in() -> bool:
    try:
        return page.locator(PTT_SELECTOR).is_visible(timeout=2000)
    except:
        return False


def auto_login():
    """自动登录，仅主线程调用"""
    logger.info("检测到未登录，执行自动登录")
    page.wait_for_selector("#username", timeout=5000)
    page.locator("#username").first.fill(TALK_USERNAME)
    page.locator("#pwd").first.fill(TALK_PASSWORD)
    page.locator(LOGIN_BTN_SELECTOR).first.click()
    page.locator(PTT_SELECTOR).wait_for(state="visible", timeout=30000)
    logger.info("自动登录成功")
    time.sleep(2)

def close_browser():
    """关闭浏览器释放资源，播报间歇期间不占用 CPU"""
    global page, context, playwright_instance
    try:
        if context:
            context.close()
    except:
        pass
    try:
        if playwright_instance:
            playwright_instance.stop()
    except:
        pass
    page = None
    context = None
    playwright_instance = None
    logger.info("浏览器已关闭，释放资源")


def refresh_page_reinit():
    """播报前打开浏览器 + 页面预热 + 预合成TTS"""
    logger.info("===== 执行播报前置页面刷新重置 =====")
    try:
        if page is None or context is None:
            # 浏览器未就绪（首次启动或上次播报后已关闭），重新初始化
            init_browser()
        else:
            # 浏览器仍在运行（非29/59时间点启动的情况），刷新页面即可
            logger.info("浏览器已在运行，刷新页面")
            page.reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            time.sleep(2)
            if not is_logged_in():
                auto_login()

        # 预合成下一次播报TTS音频
        prepare_next_tts()

        # 额外预留时间：给AudioContext、MediaStream完整初始化
        logger.info(f"等待 {REFRESH_WAIT_SEC} 秒，等待音频链路、websocket稳定建立")
        time.sleep(REFRESH_WAIT_SEC)

        logger.info("===== 页面刷新预热全部完成，等待准点播报 =====")
    except Exception as e:
        logger.error(f"页面刷新预热异常: {str(e)}", exc_info=True)
        close_browser()

def pre_refresh_task():
    """定时入队：播报前一分钟刷新预热"""
    task_queue.put("pre_refresh")

def announce_task():
    """播报核心逻辑，仅主线程执行"""
    now = datetime.datetime.now()
    announce_text = get_announce_text(now)
    logger.info(f"触发定时播报: {announce_text}")

    try:
        # 浏览器未就绪时当场初始化（页面崩溃等异常的容错）
        if page is None or context is None:
            logger.warning("播报时浏览器未就绪，紧急初始化")
            init_browser()

        if not is_logged_in():
            auto_login()

        tts_file = get_tts_file(announce_text)
        if not tts_file:
            logger.error("TTS音频文件无效，跳过本次播报")
            return

        ptt_btn = page.locator(PTT_SELECTOR)
        ptt_btn.wait_for(state="visible", timeout=5000)

        # 按下PTT，沿用Windows成熟写法
        ptt_btn.dispatch_event("mousedown")
        logger.info("PTT 已按下")
        time.sleep(PTT_PRESS_DELAY / 1000)

        # 加载音频
        with open(tts_file, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        duration = page.evaluate("b64 => window.__virtualMic.loadAudio(b64)", audio_b64)
        dur = float(duration)
        logger.info(f"音频加载完成，时长 {dur:.2f} 秒")

        # 播放前强制启用 track（防止浏览器重启后 track 被自动 disable）
        track_ok = page.evaluate("() => window.__virtualMic.ensureTrackEnabled()")
        logger.info(f"MediaStream track 状态: {'启用' if track_ok else '异常'}")

        page.evaluate("() => window.__virtualMic.play()")
        logger.info("音频开始播放")

        # 播报期间抑制浏览器控制台日志，减少主线程 CDP 通道开销
        page.evaluate("() => { window.__playbackActive = true; }")

        # 电平检测 + track 状态校验（合并为一次 evaluate 减少主线程开销）
        for i in range(LEVEL_CHECK_ROUNDS):
            time.sleep(0.3)
            status = page.evaluate("() => ({ level: window.__virtualMic.getLevel(), trackOk: window.__virtualMic.ensureTrackEnabled() })")
            level = status['level']
            track_ok = status['trackOk']
            logger.info(f"第 {i+1} 次电平检测: {level:.1f}, track: {'启用' if track_ok else '异常'}")
            if level > LEVEL_CHECK_THRESHOLD and track_ok:
                logger.info("[OK] 音频输出正常，track 启用")
                break

        # ==========关键改动：提前释放PTT，抵消网页WebRTC音频缓冲==========
        wait_play = max(0.5, dur - PTT_BUFFER_OFFSET)
        logger.info(f"等待{wait_play:.2f}s后预释放PTT，预留{PTT_BUFFER_OFFSET}s网页缓冲时间")
        time.sleep(wait_play)

        # 派发松开事件
        ptt_btn.dispatch_event("mouseup")
        logger.info("PTT 已预释放")

        # 等待剩余时长结束，让音频完整播放收尾
        time.sleep(PTT_BUFFER_OFFSET)

        # 播报结束，恢复浏览器控制台日志
        page.evaluate("() => { window.__playbackActive = false; }")
        logger.info("播报完整结束")

    except Exception as e:
        logger.error(f"播报流程异常: {str(e)}", exc_info=True)
        try:
            page.evaluate("() => { window.__playbackActive = false; }")
        except:
            pass
        try:
            page.locator(PTT_SELECTOR).dispatch_event("mouseup")
        except:
            pass


   
def init_browser():
    global page, playwright_instance, context
    logger.info("初始化常驻浏览器...")
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    playwright_instance = sync_playwright().start()
    context = playwright_instance.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        viewport={"width":320, "height":240},
        headless=HEADLESS_MODE,
        permissions=["microphone"],
        ignore_https_errors=True,
        args=[
            # 音频相关
            "--disable-audio-processing",
            "--disable-echo-cancellation",
            "--disable-noise-suppression",
            "--disable-automatic-gain-control",
            "--disable-audio-input-device-sandbox",
            f"--audio-output-sample-rate={AUDIO_SAMPLE_RATE}",
            f"--audio-input-sample-rate={AUDIO_SAMPLE_RATE}",
            # 渲染优化（纯音频场景，不需要视觉输出）
            "--disable-gpu",
            "--disable-images",
            "--disable-remote-fonts",
            "--disable-reading-from-canvas",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-component-extensions-with-background-pages",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-background-networking",
            # 安全沙箱
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
    )

    # 页面加载前注入虚拟麦克风
    setup_virtual_mic(context)

    page = context.pages[0] if context.pages else context.new_page()

    # ========== 新增：监听浏览器所有控制台输出，自动写入日志 ==========
    page.on("console", lambda msg: logger.info(f"[浏览器控制台] {msg.type}: {msg.text}"))
    page.on("pageerror", lambda err: logger.error(f"[页面报错] {err.message}"))

    page.goto(TALK_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

    # 验证注入
    test_devices = page.evaluate("navigator.mediaDevices.enumerateDevices().then(d => d.filter(x => x.kind==='audioinput'))")
    logger.info(f"注入验证：识别到麦克风数量 = {len(test_devices)}")

    if not is_logged_in():
        auto_login()
        time.sleep(1)
    else:
        logger.info("检测到已登录状态，直接进入对讲")
        
    logger.info("浏览器初始化完成")


# ========== 调度任务分发函数 ==========
def schedule_announce():
    task_queue.put("announce")

def schedule_pre_refresh():
    task_queue.put("pre_refresh")

if __name__ == "__main__":
    try:
        # 交互模式：阻塞式，按回车播报，不进入调度
        if len(sys.argv) > 1 and sys.argv[1] in ("--interactive", "-i"):
            init_browser()
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
                init_browser()
                logger.info(f"=== 测试模式：连续播报 {repeat} 次 ===")
                for i in range(1, repeat + 1):
                    logger.info(f"--- 第 {i}/{repeat} 次播报 ---")
                    announce_task()
                    if i < repeat:
                        time.sleep(2)
                logger.info(f"测试完成，共播报 {repeat} 次，进入正常调度模式")
                prepare_next_tts()
            else:
                # 启动时执行一次预刷新（初始化浏览器 + TTS预合成）                                                                                                                 
                refresh_page_reinit()
                
            # 后台调度器
            scheduler = BackgroundScheduler(timezone="Asia/Shanghai", daemon=True)
            scheduler.add_job(
                schedule_pre_refresh,
                "cron",
                hour=f"{ANNOUNCE_START_HOUR}-{ANNOUNCE_END_HOUR}",
                minute="29,59",
                second=0
            )
            scheduler.add_job(
                schedule_announce,
                "cron",
                hour=f"{ANNOUNCE_START_HOUR}-{ANNOUNCE_END_HOUR}",
                minute="0,30",
                second=0
            )
            scheduler.start()

            logger.info(f"自动播报服务启动成功，每日 {ANNOUNCE_START_HOUR}:00 ~ {ANNOUNCE_END_HOUR}:00 每半小时播报一次")
            logger.info("预热规则：XX:29 / XX:59 自动刷新页面重置音频链路；XX:00 / XX:30 准时播报")

            # 主线程循环：队列串行执行所有页面操作（必须单线程操作Playwright）
            while True:
                task = task_queue.get()
                try:
                    if task == "announce":
                        announce_task()
                        close_browser()  # 播报完毕关闭浏览器，空闲期间不占CPU
                    elif task == "pre_refresh":
                        refresh_page_reinit()
                except Exception as e:
                    logger.error(f"队列任务执行异常 task={task}: {str(e)}", exc_info=True)
                finally:
                    task_queue.task_done()
        
    except KeyboardInterrupt:
        logger.info("接收到停止信号，正在清理资源...")
    finally:
        if context:
            context.close()
        if playwright_instance:
            playwright_instance.stop()
        logger.info("服务已停止，登录态已保存")
