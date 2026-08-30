# Ham Repeater Bot - 中继报时钟

业余无线电中继台自动播报服务：通过**协议直连**滔滔链路（ALLPTT）服务器实现定时语音播报，不依赖浏览器与任何图形环境，支持 Docker 一键部署。

> v2 架构升级：以 Python 直连平台 TCP/TLS 协议端口（与手机 App 同级链路）替代原 Playwright 浏览器方案——无需 Chromium、无需虚拟声卡、容器镜像体积从 ~1.5GB 精简到 ~300MB，且不再受平台网页网关故障影响。

## 功能特性

- **协议直连**：原生对接平台服务器（改造版 Mumble 协议），链路可靠性与手机 App 同级
- **定时播报**：每半小时自动播报一次（默认 7:00 ~ 22:30），内容包含日期、星期、时间
- **常驻链路**：后台线程 Ping 保活 + 下行流量泵，服务器视角与长在线的真实用户一致
- **准点对齐**：播报前一分钟预热（TTS 预合成 + 音频包预构建 + 预建链），准点前 0.5s 才抢麦（不提前占用信道），首包紧贴整点发出
- **三级兜底**：常驻链路 → 临时短链 → 准点现场完整流程，任一环节故障自动降级，播报不中断
- **TTS 蓄水池**：每小时预合成未来 48 小时所有播报时段的音频，准点播报不依赖播报时刻的 Edge-TTS 网络状态
- **企业微信告警**：日志达到指定级别时自动推送到企业微信群机器人
- **轻量运行**：仅依赖 libopus0/libmpg123-0 两个系统库（ctypes 直调），无 pip 重型依赖

## 架构

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ APScheduler │────▶│ direct_announce  │────▶│ 滔滔链路服务器     │
│ (定时触发)   │     │ (协议直连客户端)  │     │ (TLS :59638)     │
└─────────────┘     └──────────────────┘     └──────────────────┘
       │                     ▲
       │                     │ mp3 → Opus 12kbps → 185B/包
       ▼                     │
┌──────────────┐             │
│  Edge-TTS    │─────────────┘
│  (语音合成)   │
└──────────────┘
```

- **常驻链路（PersistentAnnouncer）**：守护线程每 2.5s Ping 保活并消费下行流量；播报前健康检查（Ping 回显），不健康自动重建
- **播报流程**：抢麦（ApplyMic）→ 上报开始说话（UserTalking）→ 0.5s 建链间隔 → 匀速 120ms/包发包（官方实时编码节奏）→ 放麦 → 收服务器回执（录音 URL = 端到端确认）

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/MZYDJ/ham-repeater-bot.git
cd ham-repeater-bot
```

### 2. 配置账号

滔滔链路账号通过环境变量注入（也可直接改 `announce.py` 顶部常量）：

```yaml
# docker-compose.yml 的 environment 段
environment:
  - TZ=Asia/Shanghai
  - TALK_USERNAME=你的滔滔链路账号      # 必填
  - TALK_PASSWORD=你的滔滔链路密码      # 必填
  - WECHAT_WEBHOOK_URL=                # 可选：企业微信机器人 Webhook，留空禁用告警
```

播报内容（中继台呼号、频率、亚音频等）编辑 `announce.py` 顶部的 `ANNOUNCE_TEMPLATE`。

### 3. 启动服务

#### 方式一：命令行启动（Linux / macOS）

```bash
# 构建镜像
docker compose build

# 启动服务（后台运行）
docker compose up -d

# 查看实时日志
docker compose logs -f

# 停止服务
docker compose down
```

#### 方式二：飞牛NAS（fnOS）图形界面部署

飞牛NAS 内置 Docker 管理功能，可通过 Compose 图形界面一键部署：

1. **上传项目文件**
   - 将本项目的所有文件（`announce.py`、`direct_announce.py`、`Dockerfile`、`docker-compose.yml`、`start.sh`）上传到飞牛NAS 的共享文件夹中，例如 `/vol1/docker/ham-repeater-bot/`

2. **打开 Docker 管理**
   - 登录飞牛NAS 管理后台
   - 在桌面或应用中心找到 **「Docker」**，点击进入

3. **进入容器编排**
   - 在 Docker 管理页面左侧菜单中，点击 **「Compose」**（容器编排）

4. **创建 Compose 项目**
   - 点击 **「创建」** 或 **「新建项目」**
   - 项目名称填写：`ham-repeater-bot`（或自定义名称）
   - 路径选择步骤 1 中上传的目录，例如 `/vol1/docker/ham-repeater-bot`

5. **确认使用现有配置**
   - 选择路径后，系统会弹窗提示：*「所选择的路径已包含 docker-compose 配置文件，确定要使用现有的 docker-compose 配置文件来创建项目吗？」*
   - 点击 **「确定」**，系统会自动识别 `docker-compose.yml` 并创建项目

6. **部署启动**
   - 点击 **「部署」** 或 **「启动」** 按钮
   - 系统会自动构建镜像并启动容器
   - 在「容器」页面可以查看运行状态和日志

> **提示**：首次部署需要构建镜像，可能需要几分钟时间，取决于 NAS 性能和网络速度。

### 4. 测试与调试

```bash
# 容器内测试模式：播报指定次数后进入正常调度（默认 1 次）
docker exec -it announce python3 announce.py -t [次数]

# 交互模式：按回车播报一次（适合调试）
docker exec -it announce python3 announce.py -i

# 直连客户端独立自测（不联网，只跑音频编解码管线）
docker exec -it announce python3 direct_announce.py --mp3 tts_cache/某个缓存文件.mp3 --dry-run
```

> 注意：测试模式与常驻链路同账号并发会互踢（服务器顶掉旧会话），触发一条告警后自动重连，属正常现象。

## 配置说明

### 播报时间

```python
ANNOUNCE_START_HOUR = 7    # 每日首次播报时刻（整点）
ANNOUNCE_END_HOUR = 22     # 每日最后一次播报所在小时（22 表示最后一次为 22:30）
```

播报规则：每半小时一次，XX:00 和 XX:30 各播报一次。每次播报前一分钟（XX:29 / XX:59）自动预热（预合成 TTS + 预构建音频包 + 预建链）；每天首次播报由 (START-1):59 预热；收盘播报后仅保留常驻链路保活，不空转。

### 直连时序

```python
NATIVE_PREP_LEAD = 2.5   # 准点前该秒数建链+登录（实测建链 ~1s）
NATIVE_MIC_LEAD = 0.5    # 准点前该秒数才发起抢麦（不提前占麦）
```

抢麦提前量 0.5s 是刻意设计：提前抢麦会让频道内其他用户看到"说话中"状态长时间静默。抢麦服务器响应约 1.1s，实际首包落在准点后 ~1.1s，其中 UserTalking 与首包保持 0.5s 官方间隔（中继台链路设备靠该间隔建立转发）。

### TTS 合成

```python
TTS_VOICE = "zh-CN-XiaoxiaoNeural"   # Edge-TTS 语音角色
CACHE_EXPIRE_DAYS = 2                # TTS 缓存过期天数
TTS_PREFILL_HOURS = 48               # TTS 蓄水池提前量（小时）
```

TTS 蓄水池机制：播报文本完全由日期+时间决定，可提前计算。服务每小时检查并预合成未来 `TTS_PREFILL_HOURS` 内所有播报时段缺失的音频，使准点播报不依赖播报时刻的 Edge-TTS 网络状态——网络突发故障（分钟到小时级）不再导致播报失败，只有连续中断超过一天才可能缺音。

### 日志与 Webhook

```python
WEBHOOK_LOG_LEVEL = logging.WARNING   # 推送级别：WARNING=仅告警+错误
LOG_MAX_BYTES = 0.25 * 1024 * 1024    # 单日志文件最大 0.25MB
LOG_BACKUP_COUNT = 40                  # 保留最近 40 个备份
```

## 数据目录

`docker-compose.yml` 将项目目录挂载到容器 `/app`，运行时会在项目根目录下自动创建以下子目录：

| 子目录 | 说明 |
|--------|------|
| `tts_cache/` | TTS 合成音频缓存 |
| `logs/` | 运行日志（自动轮转） |

## 运行要求

- Docker 与 Docker Compose
- 网络需能访问滔滔链路（allptt.com:59638，TLS）和 Edge-TTS 服务

## 依赖

- Python 3.x
- [edge-tts](https://github.com/rany2/edge-tts)（语音合成）
- [APScheduler](https://github.com/agronholm/apscheduler)（定时调度）
- 系统库 `libopus0`、`libmpg123-0`（音频编解码，ctypes 直调，Dockerfile 已包含）

## 许可证

本项目采用 [MIT License](LICENSE) 开源。

使用前请确保：
1. 持有有效的业余无线电操作证书
2. 中继台已依法取得电台执照
3. 播报内容符合当地无线电管理规定
