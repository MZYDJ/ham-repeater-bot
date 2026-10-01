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

# ========== 5. 安装Python业务依赖（固定版本） ==========
# dashscope>=1.18 提供 tts_v2 SpeechSynthesizer（CosyVoice 点名 TTS 官方 SDK，
# WebSocket 流式；net_control 已做旧版 timeout 参数兼容 + SDK 缺失自动回退 HTTP）
RUN pip install --break-system-packages --no-cache-dir \
    edge-tts==7.2.8 \
    APScheduler==3.11.3 \
    "dashscope>=1.18.0"

# ========== 6. 构建版本标记（构建时自动生成当天日期，无需手动同步） ==========
# 说明：镜像构建时间随构建内容同处本文件，改 Dockerfile 重建镜像即自动更新，
# 不再依赖 docker-compose build.args 手动注入（旧设计：改 Dockerfile 装包后
# 常忘记同步 compose 里的 BUILD_TIME，导致版本标记陈旧）。
# start.sh 读取优先级：/app/BUILD_TIME（zip 覆盖部署，发布时现场生成）
#                   > /opt/build_time（镜像构建时生成）
#                   > 环境变量 BUILD_TIME（旧镜像兼容兜底）
RUN date -u +'%Y-%m-%d %H:%M' > /opt/build_time

# ========== 7. 工作目录 ==========
WORKDIR /app

# ========== 8. 入口脚本 ==========
ENTRYPOINT ["/app/start.sh"]
