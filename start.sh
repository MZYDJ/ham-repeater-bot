#!/bin/bash
set -e

# ========== 环境版本信息 ==========
echo "========== 环境信息 =========="
echo "[ENV] 启动时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "[ENV] OS: $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"')"
echo "[ENV] Python: $(python3 --version 2>&1)"
echo "[ENV] edge-tts: $(python3 -c 'import edge_tts; print(edge_tts.__version__)' 2>&1 || echo '未安装')"
echo "[ENV] APScheduler: $(python3 -c 'import apscheduler; print(apscheduler.__version__)' 2>&1 || echo '未安装')"
echo "[ENV] BUILD_TIME: ${BUILD_TIME:-unknown}"
# 直连链路音频编解码库自检（缺失则起服务也必然播报失败，fail-fast）
echo "[ENV] libopus: $(python3 -c 'import ctypes.util; print("OK" if ctypes.util.find_library("opus") else "缺失!")' 2>&1)"
echo "[ENV] libmpg123: $(python3 -c 'import ctypes.util; print("OK" if ctypes.util.find_library("mpg123") else "缺失!")' 2>&1)"
echo "=============================="

# 持久目录授权
chown -R audiouser:audiouser /app/tts_cache /app/logs || true

# 切换普通用户执行
exec su audiouser -s /bin/bash -c "
cd /app
exec python3 announce.py
"
