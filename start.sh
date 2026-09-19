#!/bin/bash
set -e

# ========== 环境版本信息 ==========
echo "========== 环境信息 =========="
echo "[ENV] 启动时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "[ENV] OS: $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"')"
echo "[ENV] Python: $(python3 --version 2>&1)"
echo "[ENV] edge-tts: $(python3 -c 'import edge_tts; print(edge_tts.__version__)' 2>&1 || echo '未安装')"
echo "[ENV] APScheduler: $(python3 -c 'import apscheduler; print(apscheduler.__version__)' 2>&1 || echo '未安装')"
echo "[ENV] dashscope: $(python3 -c 'import dashscope; print(dashscope.__version__)' 2>&1 || echo '未安装！CosyVoice 点名/播报 TTS 需此依赖，请重建镜像或 pip install dashscope>=1.18')"
echo "[ENV] BUILD_TIME: ${BUILD_TIME:-unknown}"
# 直连链路音频编解码库自检（缺失则起服务也必然播报失败，fail-fast）
echo "[ENV] libopus: $(python3 -c 'import ctypes.util; print("OK" if ctypes.util.find_library("opus") else "缺失!")' 2>&1)"
echo "[ENV] libmpg123: $(python3 -c 'import ctypes.util; print("OK" if ctypes.util.find_library("mpg123") else "缺失!")' 2>&1)"
# 配置文件定位（默认同目录 config.json，可用环境变量 HAM_BOT_CONFIG 覆盖）
echo "[ENV] HAM_BOT_CONFIG: ${HAM_BOT_CONFIG:-（未设置，默认使用 /app/config.json）}"
if [ -z "${HAM_BOT_CONFIG}" ] && [ ! -f /app/config.json ]; then
    echo "[WARN] 未找到 /app/config.json！请将 config.example.json 复制为 config.json 并填写参数。"
fi
# 关键配置字段自检（启动即提示，避免点名/播报运行时才报错）
python3 - <<'PYEOF'
import json, os
p = os.environ.get("HAM_BOT_CONFIG", "/app/config.json")
try:
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    print(f"[WARN] 配置文件读取失败（{p}）: {e}")
    raise SystemExit(0)
def has(*path):
    n = cfg
    for k in path:
        if not isinstance(n, dict) or k not in n:
            return False
        n = n[k]
    return bool(n)
engine = (cfg.get("tts") or {}).get("engine", "cosyvoice")
if not has("talk", "username") or not has("talk", "password"):
    print("[WARN] talk.username / talk.password 未配置！直连链路将无法登录。")
if engine == "cosyvoice":
    if not has("asr", "api_key"):
        print("[WARN] tts.engine=cosyvoice 但 asr.api_key 为空/缺失！点名与播报 TTS 将失败。")
    if not has("tts", "cosyvoice_voice"):
        print("[WARN] tts.engine=cosyvoice 但 tts.cosyvoice_voice 为空/缺失！v3.5 系列需先在百炼控制台创建音色。")
    if not has("tts", "cosyvoice_model"):
        print("[WARN] tts.cosyvoice_model 未配置，将使用默认 cosyvoice-v3.5-flash。")
else:
    if not has("tts", "voice"):
        print("[WARN] tts.engine=edge 但 tts.voice 未配置，将使用默认 zh-CN-XiaoxiaoNeural。")
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
