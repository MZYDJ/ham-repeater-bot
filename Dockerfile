FROM ubuntu:26.04

# ========== 1. 替换为阿里云APT国内源 ==========
RUN sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/ubuntu.sources && \
    sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/ubuntu.sources

# ========== 2. 安装系统依赖 ==========
# Chromium headless 运行所需的最小依赖集（无 Xvfb 服务端、无 PulseAudio）
# X11 基础库（libxfixes/libx11/libxext/libxrender/libxi）Chromium 运行时仍需要
RUN apt update && apt install -y --no-install-recommends \
    libpulse0 libasound2t64 \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libxshmfence1 libglib2.0-0 libfontconfig1 \
    libxfixes3 libx11-6 libxext6 libxrender1 libxi6 libxkbcommon0 \
    python3 python3-pip \
    ca-certificates curl procps vim \
    && rm -rf /var/lib/apt/lists/*

# ========== 3. 创建运行用户 ==========
RUN useradd -m audiouser

# ========== 4. 配置PIP全局清华源 ==========
RUN mkdir -p /etc/pip && \
    cat > /etc/pip.conf << 'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
EOF

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# ========== 5. 安装Python业务依赖（固定版本） ==========
RUN pip install --break-system-packages --no-cache-dir \
    playwright==1.61.0 \
    edge-tts==7.2.8 \
    APScheduler==3.11.3

# ========== 6. 安装Playwright浏览器 ==========
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

RUN playwright install chromium && \
    chmod -R 755 /opt/playwright-browsers

# ========== 7. 构建版本标记 ==========
ARG BUILD_TIME=unknown
ENV BUILD_TIME=${BUILD_TIME}

# ========== 8. 工作目录 ==========
WORKDIR /app

# ========== 9. 入口脚本 ==========
ENTRYPOINT ["/app/start.sh"]
