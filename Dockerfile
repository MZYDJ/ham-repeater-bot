FROM ubuntu:26.04

# ========== 1. 替换为阿里云APT国内源 ==========
RUN sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/ubuntu.sources && \
    sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/ubuntu.sources

# ========== 2. 安装系统依赖 ==========
# 直连客户端仅需音频编解码系统库（libopus0=Opus编码，libmpg123-0=mp3解码，均 ctypes 直调）
RUN apt update && apt install -y --no-install-recommends \
    libopus0 libmpg123-0 \
    python3 python3-pip \
    ca-certificates curl procps vim \
    && rm -rf /var/lib/apt/lists/*

# ========== 3. 创建运行用户 ==========
RUN useradd -m audiouser

# ========== 4. 配置PIP全局阿里源 ==========
RUN mkdir -p /etc/pip && \
    cat > /etc/pip.conf << 'EOF'
[global]
index-url = https://mirrors.aliyun.com/pypi/simple/
trusted-host = mirrors.aliyun.com
EOF

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# ========== 5. 安装Python业务依赖 ==========
# scripts/update_edge_tts_version.py 每次构建前查询阿里云 PyPI 镜像的最新稳定版，
# 并写入被 .gitignore 忽略的 .env；版本号作为 build arg，未变化时可复用本层缓存。
# 使用 BuildKit pip 缓存：即使其他依赖变化需要重装，也可复用已下载的 wheel。
# 不提供版本号时 fail-fast，避免 GUI 直接构建时静默沿用旧版。
ARG EDGE_TTS_VERSION=unset
# dashscope>=1.18 提供 tts_v2 SpeechSynthesizer（CosyVoice 点名 TTS 官方 SDK，
# WebSocket 流式；net_control 已做旧版 timeout 参数兼容 + SDK 缺失自动回退 HTTP）
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$EDGE_TTS_VERSION" = "unset" ] || [ -z "$EDGE_TTS_VERSION" ]; then \
      echo >&2 "ERROR: EDGE_TTS_VERSION 未设置。请先运行 python3 scripts/update_edge_tts_version.py"; \
      exit 1; \
    fi && \
    pip install --break-system-packages \
      "edge-tts==${EDGE_TTS_VERSION}" \
      APScheduler==3.11.3 \
      "dashscope>=1.18.0"

# ========== 6. 构建版本标记 ==========
# 镜像构建时间仍由本文件生成；提交号由构建前脚本从 Git 写入 .env，再经 Compose build args 注入。
# 元数据放在依赖安装层之后，修改版本标识不会触发 edge-tts / APScheduler / dashscope 重新安装。
ARG GIT_COMMIT=unknown
ARG DOCKERFILE_VERSION=2
ENV GIT_COMMIT=${GIT_COMMIT} \\
    DOCKERFILE_VERSION=${DOCKERFILE_VERSION}
RUN date -u +'%Y-%m-%d %H:%M' > /opt/build_time

# ========== 7. 工作目录 ==========
WORKDIR /app

# ========== 8. 入口脚本 ==========
ENTRYPOINT ["/app/start.sh"]
