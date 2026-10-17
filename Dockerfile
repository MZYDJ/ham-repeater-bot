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
# GUI Compose 构建无需预先生成 .env：未显式传版本时，在依赖层构建时自动查询阿里云 PyPI 最新稳定版。
# 命令行 scripts/build-image.sh 会先查询版本并显式传入 build arg；版本未变化时可复用本层缓存。
# BuildKit pip 缓存可减少依赖层失效时的重复下载。
ARG EDGE_TTS_VERSION=auto
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -z "$EDGE_TTS_VERSION" ] || [ "$EDGE_TTS_VERSION" = "unset" ] || [ "$EDGE_TTS_VERSION" = "auto" ]; then \
      echo "[INFO] 未指定 EDGE_TTS_VERSION，自动查询阿里云 PyPI 最新稳定版..." && \
      VERSION_OUTPUT="$(python3 -m pip index versions --index-url https://mirrors.aliyun.com/pypi/simple/ edge-tts 2>&1)" || { \
        echo >&2 "[ERROR] 查询 edge-tts 最新版本失败"; echo >&2 "$VERSION_OUTPUT"; exit 1; \
      } && \
      EDGE_TTS_VERSION="$(printf '%s\\n' "$VERSION_OUTPUT" | sed -n 's/^edge-tts (\\([^)]*\\)).*/\\1/p' | head -n 1)" && \
      if [ -z "$EDGE_TTS_VERSION" ]; then \
        echo >&2 "[ERROR] 无法从 PyPI 响应中解析 edge-tts 版本"; echo >&2 "$VERSION_OUTPUT"; exit 1; \
      fi; \
    fi && \
    echo "[INFO] 安装 edge-tts==${EDGE_TTS_VERSION}" && \
    pip install --break-system-packages \
      "edge-tts==${EDGE_TTS_VERSION}" \
      APScheduler==3.11.3

# ========== 6. 构建版本标记 ==========
ARG BUILD_TIME=unknown
ENV BUILD_TIME=${BUILD_TIME}

# ========== 7. 工作目录 ==========
WORKDIR /app

# ========== 8. 入口脚本 ==========
ENTRYPOINT ["/app/start.sh"]
