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
# 每次构建前通过 scripts/update_edge_tts_version.py 查询阿里云 PyPI 镜像的最新稳定版，
# 并写入被 .gitignore 忽略的 .env；版本号作为 build arg，未变化时可复用本层缓存。
# BuildKit pip 缓存可减少依赖层失效时的重复下载；未提供版本号时 fail-fast。
ARG EDGE_TTS_VERSION=unset
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$EDGE_TTS_VERSION" = "unset" ] || [ -z "$EDGE_TTS_VERSION" ]; then \
      echo >&2 "ERROR: EDGE_TTS_VERSION 未设置。请先运行 python3 scripts/update_edge_tts_version.py"; \
      exit 1; \
    fi && \
    pip install --break-system-packages \
      "edge-tts==${EDGE_TTS_VERSION}" \
      APScheduler==3.11.3

# ========== 6. 构建版本标记 ==========
# 元数据放在依赖安装层之后，版本变化不会导致 edge-tts/APScheduler 重新安装。
ARG BUILD_TIME=unknown
ARG GIT_COMMIT=unknown
ARG DOCKERFILE_VERSION=1
ENV BUILD_TIME=${BUILD_TIME} \
    GIT_COMMIT=${GIT_COMMIT} \
    DOCKERFILE_VERSION=${DOCKERFILE_VERSION}

# ========== 7. 工作目录 ==========
WORKDIR /app

# ========== 8. 入口脚本 ==========
ENTRYPOINT ["/app/start.sh"]
