#!/bin/bash
set -e

# ========== 环境版本信息 ==========
echo "========== 环境信息 =========="
echo "[ENV] 启动时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "[ENV] OS: $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"')"
echo "[ENV] Python: $(python3 --version 2>&1)"
# 播报硬依赖：edge-tts（TTS 合成）/ apscheduler（定时调度）缺失则服务必然不可用，fail-fast
if ! python3 -c 'import edge_tts' 2>/dev/null; then
    echo "[ERROR] edge-tts 未安装——TTS 合成必然失败，播报无法进行。请 pip install edge-tts 或重建镜像。"
    exit 1
fi
if ! python3 -c 'import apscheduler' 2>/dev/null; then
    echo "[ERROR] apscheduler 未安装——定时调度无法启动，播报不会触发。请 pip install apscheduler 或重建镜像。"
    exit 1
fi
echo "[ENV] edge-tts: $(python3 -c 'import edge_tts; print(edge_tts.__version__)' 2>&1)"
echo "[ENV] APScheduler: $(python3 -c 'import apscheduler; print(apscheduler.__version__)' 2>&1)"
echo "[ENV] dashscope: $(python3 -c 'import dashscope; print(dashscope.__version__)' 2>&1 || echo '未安装！仅当点名启用且 tts.engine=cosyvoice 时必需（start.sh 会条件性 fail-fast）')"
# 构建/发布时间标记（三个来源全部列出，便于诊断线上跑的是哪版）：
#   /app/BUILD_TIME   zip 覆盖部署时由发布动作写入（发布时间）
#   /opt/build_time   Dockerfile 构建时自动生成（镜像构建时间）
#   环境变量 BUILD_TIME  旧镜像兼容（docker-compose build.args 历史注入）
BT_FILE="$(cat /app/BUILD_TIME 2>/dev/null || echo -)"
BT_IMG="$(cat /opt/build_time 2>/dev/null || echo -)"
BT_ENV="${BUILD_TIME:--}"
echo "[ENV] BUILD_TIME: /app=${BT_FILE} | /opt=${BT_IMG} | env=${BT_ENV}"
# 直连链路音频编解码库自检（缺失则起服务也必然播报失败，fail-fast 直接退出）
if ! python3 -c 'import ctypes.util; raise SystemExit(0 if ctypes.util.find_library("opus") else 1)' 2>/dev/null; then
    echo "[ERROR] libopus 缺失——Opus 编解码必然失败，播报无法进行。请安装：apt-get install -y libopus0 后重建/重启容器。"
    exit 1
fi
if ! python3 -c 'import ctypes.util; raise SystemExit(0 if ctypes.util.find_library("mpg123") else 1)' 2>/dev/null; then
    echo "[ERROR] libmpg123 缺失——mp3 解码必然失败，播报无法进行。请安装：apt-get install -y libmpg123-0 后重建/重启容器。"
    exit 1
fi
echo "[ENV] libopus: OK"
echo "[ENV] libmpg123: OK"
# 配置文件定位（默认同目录 config.json，可用环境变量 HAM_BOT_CONFIG 覆盖）
echo "[ENV] HAM_BOT_CONFIG: ${HAM_BOT_CONFIG:-（未设置，默认使用 /app/config.json）}"
if [ -z "${HAM_BOT_CONFIG}" ] && [ ! -f /app/config.json ]; then
    echo "[WARN] 未找到 /app/config.json！请将 config.example.json 复制为 config.json 并填写参数。"
fi
# 关键配置字段自检（启动即 fail-fast：下列检查失败后继续执行没有意义——无账号无法登录、
# 无模板播报为空、点名启用但 TTS 依赖缺失则点名必然失败，直接报错退出）
python3 - <<'PYEOF'
import json, os, sys
p = os.environ.get("HAM_BOT_CONFIG", "/app/config.json")
def fail(msg):
    print(f"[ERROR] {msg}")
    print("[HINT] 若此配置由 config.example.json 复制而来，请一并检查模板是否完整（git pull 可恢复最新示例）。")
    sys.exit(1)
try:
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    fail(f"配置文件读取失败（{p}）: {e}——无配置=无账号/无模板，播报无意义，请从 config.example.json 复制并填写。")
def has(*path):
    n = cfg
    for k in path:
        if not isinstance(n, dict) or k not in n:
            return False
        n = n[k]
    return bool(n)
# 必填①：滔滔账号（直连登录前置，缺失则任何播报都无法发射）
if not has("talk", "username") or not has("talk", "password"):
    fail("talk.username / talk.password 未配置——直连链路无法登录，播报无法进行。")
# 必填②：播报模板（空模板=播报内容为空）
if not has("announce", "template"):
    fail("announce.template 未配置——播报文案为空，播报无意义。")
# 条件必填：点名启用且走 CosyVoice 引擎时，其依赖缺失=点名必然失败
engine = (cfg.get("tts") or {}).get("engine", "cosyvoice")
net_enabled = has("net_control", "enabled")
if net_enabled and engine == "cosyvoice":
    asr_key = (cfg.get("net_control", {}).get("asr", {}).get("api_key")
               or (cfg.get("asr") or {}).get("api_key"))
    if not asr_key:
        fail("net_control.enabled=true 且 tts.engine=cosyvoice，但 asr.api_key 缺失——点名 TTS 必然失败。")
    if not has("tts", "cosyvoice_voice"):
        fail("net_control.enabled=true 且 tts.engine=cosyvoice，但 tts.cosyvoice_voice 缺失——点名 TTS 必然失败。")
    if not has("tts", "cosyvoice_model"):
        print("[INFO] tts.cosyvoice_model 未配置，将使用默认 cosyvoice-v3.5-flash。")
    try:
        import dashscope
    except ImportError:
        fail("net_control.enabled=true 且 tts.engine=cosyvoice，但 dashscope 未安装——点名 TTS 必然失败，请重建镜像或 pip install dashscope>=1.18。")
else:
    if not has("tts", "voice"):
        print("[WARN] tts.engine=edge 但 tts.voice 未配置，将使用默认 zh-CN-XiaoxiaoNeural。")
# 正式配置 config.json 已在上方 fail-fast（读取/账号/模板/点名条件全覆盖），
# 不再单独检查示例文件 config.example.json（模板完整性由仓库测试 test_announce 兜底）
print("==============================")
PYEOF

# 持久目录授权（含点名录音/摘要目录 net_records；容器以非 root 的 audiouser 运行，
# /app 为宿主挂载目录，须显式建目录并授权，否则 net_records 落盘会 Permission denied）
mkdir -p /app/tts_cache /app/logs /app/net_records
chown -R audiouser:audiouser /app/tts_cache /app/logs /app/net_records || true

# 切换普通用户执行
exec su audiouser -s /bin/bash -c "
cd /app
exec python3 announce.py
"
