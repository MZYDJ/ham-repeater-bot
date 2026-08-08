#!/bin/bash
set -e

# ========== 浏览器迁移到 SSD ==========
# /opt/playwright-browsers 在机械硬盘，复制到 /app/playwright-browsers（SSD）
# 减少机械硬盘读取压力，加快浏览器启动速度
HDD_BROWSER_DIR="/opt/playwright-browsers"
SSD_BROWSER_DIR="/app/playwright-browsers"
CHROMIUM_SUBDIR=$(basename "$(ls -d ${HDD_BROWSER_DIR}/chromium_headless_shell-* 2>/dev/null | head -1)")

if [ -n "${CHROMIUM_SUBDIR}" ]; then
    if [ ! -d "${SSD_BROWSER_DIR}/${CHROMIUM_SUBDIR}" ]; then
        echo "[ENV] 首次启动：复制浏览器到 SSD（${HDD_BROWSER_DIR} → ${SSD_BROWSER_DIR}）..."
        mkdir -p "${SSD_BROWSER_DIR}"
        cp -a "${HDD_BROWSER_DIR}/${CHROMIUM_SUBDIR}" "${SSD_BROWSER_DIR}/${CHROMIUM_SUBDIR}"
        # 同时复制 chromium 目录（Playwright 可能需要）
        CHROMIUM_FULL=$(basename "$(ls -d ${HDD_BROWSER_DIR}/chromium-[0-9]* 2>/dev/null | head -1)")
        if [ -n "${CHROMIUM_FULL}" ] && [ ! -d "${SSD_BROWSER_DIR}/${CHROMIUM_FULL}" ]; then
            cp -a "${HDD_BROWSER_DIR}/${CHROMIUM_FULL}" "${SSD_BROWSER_DIR}/${CHROMIUM_FULL}"
        fi
        echo "[ENV] 浏览器复制完成"
    else
        echo "[ENV] 浏览器已在 SSD，跳过复制"
    fi
    export PLAYWRIGHT_BROWSERS_PATH="${SSD_BROWSER_DIR}"
    CHROME_BIN="${SSD_BROWSER_DIR}/${CHROMIUM_SUBDIR}/chrome-headless-shell-linux64/chrome-headless-shell"
else
    echo "[ENV] 未找到 Chromium，使用原始路径"
    export PLAYWRIGHT_BROWSERS_PATH="${HDD_BROWSER_DIR}"
    CHROME_BIN="${HDD_BROWSER_DIR}/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell"
fi

# ========== 环境版本信息 ==========
echo "========== 环境信息 =========="
echo "[ENV] 启动时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "[ENV] OS: $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"')"
echo "[ENV] Python: $(python3 --version 2>&1)"
echo "[ENV] Playwright: $(pip show playwright 2>/dev/null | grep ^Version | awk '{print $2}' || echo '未安装')"
echo "[ENV] Chromium: $(${CHROME_BIN} --version 2>/dev/null || echo '未找到')"
echo "[ENV] 浏览器路径: ${PLAYWRIGHT_BROWSERS_PATH}"
echo "[ENV] edge-tts: $(python3 -c 'import edge_tts; print(edge_tts.__version__)' 2>&1 || echo '未安装')"
echo "[ENV] APScheduler: $(python3 -c 'import apscheduler; print(apscheduler.__version__)' 2>&1 || echo '未安装')"
echo "[ENV] BUILD_TIME: ${BUILD_TIME:-unknown}"

# ========== Chromium 依赖检查 ==========
echo "========== Chromium 依赖检查 =========="
if [ -f "${CHROME_BIN}" ]; then
    MISSING=$(ldd "${CHROME_BIN}" 2>/dev/null | grep "not found" || true)
    if [ -z "$MISSING" ]; then
        echo "[CHECK] 所有动态库依赖满足"
    else
        echo "[CHECK] 以下动态库缺失："
        echo "$MISSING"
    fi
else
    echo "[CHECK] Chromium 二进制未找到: ${CHROME_BIN}"
fi
echo "=============================="

# 持久目录授权
chown -R audiouser:audiouser /app/edge_user_data /app/tts_cache /app/logs || true

# 清理 Chromium 残留锁
rm -f /app/edge_user_data/SingletonLock
rm -f /app/edge_user_data/SingletonSocket
rm -f /app/edge_user_data/SingletonCookie

# 切换普通用户执行
exec su audiouser -s /bin/bash -c "
export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}
cd /app
exec python3 announce.py -t 
"
