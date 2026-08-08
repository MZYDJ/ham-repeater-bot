# Ham Repeater Bot - 中继报时钟

业余无线电中继台自动播报服务，通过滔滔链路（ALLPTT）Web 端实现定时语音播报，支持 Docker 一键部署。

## 功能特性

- **定时播报**：每半小时自动播报一次（默认 6:00 ~ 22:00），内容包含日期、星期、时间
- **TTS 语音合成**：使用 Edge-TTS 生成中文语音，支持缓存和自动过期清理
- **虚拟麦克风注入**：通过 Playwright 注入 JS 操控 Web Audio API，实现无物理声卡的音频输入
- **PTT 自动控制**：自动按下/释放滔滔链路的 PTT 按键，播报前预留缓冲时间抵消 WebRTC 延迟
- **企业微信告警**：日志达到指定级别时自动推送到企业微信群机器人
- **资源优化**：播报间隔期间自动关闭浏览器释放 CPU，播报前自动重新初始化

## 架构

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  APScheduler │────▶│  Playwright      │────▶│  滔滔链路     │
│  (定时触发)   │     │  (Chromium Headless)│    │  (Web PTT)   │
└─────────────┘     └──────────────────┘     └──────────────┘
       │                     │
       │                     ▼
       │              ┌──────────────┐
       └─────────────▶│  Edge-TTS    │
                      │  (语音合成)   │
                      └──────────────┘
```

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/YOUR_USERNAME/ham-repeater-bot.git
cd ham-repeater-bot
```

### 2. 编辑配置

编辑 `announce.py` 顶部的配置项，修改为你的实际信息：

```python
# 滔滔链路账号（必填）
TALK_USERNAME = "YOUR_TALK_USERNAME"
TALK_PASSWORD = "YOUR_TALK_PASSWORD"

# 播报内容（修改为你的中继台呼号、频率等信息）
ANNOUNCE_TEMPLATE = "CQ CQ CQ，现在是{year}年{month}月{day}日，{weekday}，{hour}点{minute_text}。这里是YOUR_CALLSIGN，本中继下行频率 XXX.XXX 兆赫，上行频率 XXX.XXX 兆赫，叉频 负 X.XX 兆赫。单上行接入亚音为模拟 XXX.X 赫兹。请规范用频，保持信道畅通。完毕"

# 企业微信 Webhook（可选，不需要可留空）
WECHAT_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_WEBHOOK_KEY"
```

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
   - 将本项目的所有文件（`announce.py`、`Dockerfile`、`docker-compose.yml`、`start.sh`）上传到飞牛NAS 的共享文件夹中，例如 `/vol1/docker/ham-repeater-bot/`

2. **打开 Docker 管理**
   - 登录飞牛NAS 管理后台
   - 在桌面或应用中心找到 **「Docker」**，点击进入

3. **进入容器编排**
   - 在 Docker 管理页面左侧菜单中，点击 **「Compose」**（容器编排）

4. **创建 Compose 项目**
   - 点击 **「创建」** 或 **「新建项目」**
   - 项目名称填写：`ham-repeater-bot`（或自定义名称）

5. **导入 docker-compose.yml**
   - **方式 A - 粘贴内容**：用文本编辑器打开 `docker-compose.yml`，将内容复制粘贴到编辑框中
   - **方式 B - 上传文件**：点击「上传」或「导入」按钮，选择本地的 `docker-compose.yml` 文件

6. **设置 Compose 路径**
   - 将 Compose 文件路径设置为步骤 1 中上传的目录，例如 `/vol1/docker/ham-repeater-bot`
   - 确保「构建镜像」选项已开启（因为本项目需要本地构建）

7. **部署启动**
   - 点击 **「部署」** 或 **「启动」** 按钮
   - 系统会自动构建镜像并启动容器
   - 在「容器」页面可以查看运行状态和日志

> **提示**：首次部署需要构建镜像，可能需要几分钟时间，取决于 NAS 性能和网络速度。

### 4. 测试与调试

修改 `start.sh` 最后一行的启动参数，可以切换运行模式：

```bash
# 测试模式：播报指定次数后进入正常调度（默认 1 次）
exec python3 announce.py -t [次数]

# 交互模式：按回车播报一次（适合调试）
exec python3 announce.py -i
```

## 配置说明

### 播报时间

```python
ANNOUNCE_START_HOUR = 6    # 每日起始播报时间（整点）
ANNOUNCE_END_HOUR = 22     # 每日结束播报时间（整点）
```

播报规则：每半小时一次，XX:00 和 XX:30 各播报一次。XX:29 和 XX:59 会自动预刷新页面和预合成 TTS。

### TTS 合成

```python
TTS_VOICE = "zh-CN-XiaoxiaoNeural"   # Edge-TTS 语音角色
TTS_SYNTH_TIMEOUT = 30        # TTS 单次合成超时（秒）
TTS_MAX_RETRIES = 3           # TTS 合成最大重试次数
TTS_RETRY_DELAY = 5.0         # TTS 合成重试间隔（秒）
CACHE_EXPIRE_DAYS = 7         # TTS 缓存过期天数
```

### 音频与播报

```python
AUDIO_SAMPLE_RATE = 24000     # 虚拟麦克风采样率（Hz）
MIC_GAIN = 1.5                # 虚拟麦克风播报音量增益
LEVEL_CHECK_THRESHOLD = 10    # 电平检测通过阈值（0-255）
LEVEL_CHECK_ROUNDS = 5        # 电平检测最大轮数
PTT_BUFFER_OFFSET = 0.6       # PTT 提前释放缓冲时间（秒），抵消 WebRTC 音频缓冲
```

### 超时与延迟

```python
PTT_PRESS_DELAY = 800        # PTT 按下后等待时间（ms）
PAGE_LOAD_TIMEOUT = 15000    # 页面加载超时（ms）
REFRESH_WAIT_SEC = 6         # 刷新后等待音频链路稳定时间（s）
```

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
| `edge_user_data/` | 浏览器持久化数据（登录态等） |
| `tts_cache/` | TTS 合成音频缓存 |
| `logs/` | 运行日志（自动轮转） |

## 运行要求

- Docker 与 Docker Compose
- 宿主机需要支持 Chromium headless 运行（需共享 `/dev/shm`，compose 中已配置 `shm_size: 256m`）
- 网络需能访问滔滔链路（allptt.com）和 Edge-TTS 服务

## 依赖

- Python 3.x
- [Playwright](https://playwright.dev/) (Chromium)
- [edge-tts](https://github.com/rany2/edge-tts)
- [APScheduler](https://github.com/agronholm/apscheduler)

## 许可证

本项目采用 [MIT License](LICENSE) 开源。

使用前请确保：
1. 持有有效的业余无线电操作证书
2. 中继台已依法取得电台执照
3. 播报内容符合当地无线电管理规定
