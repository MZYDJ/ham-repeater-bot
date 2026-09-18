# Ham Repeater Bot - 中继报时钟
业余无线电中继台自动播报服务：通过**协议直连**滔滔链路（ALLPTT）服务器实现定时语音播报，不依赖浏览器与任何图形环境，支持 Docker 一键部署。
> v2 架构升级：以 Python 直连平台 TCP/TLS 协议端口（与手机 App 同级链路）替代原 Playwright 浏览器方案——无需 Chromium、无需虚拟声卡、容器镜像体积从 ~1.5GB 精简到 ~300MB，且不再受平台网页网关故障影响。
>
> **v12 配置独立版**：所有环境相关参数（账号、播报模板、时段、时序、音频、告警等）全部迁移至 `config.json`，代码零硬编码。生产/开源共用同一份代码，仅配置文件不同即可切换部署环境；升级代码只需同步一次，不再需要单独同步参数。
## 功能特性
- **协议直连**：原生对接平台服务器（改造版 Mumble 协议），链路可靠性与手机 App 同级
- **定时播报**：每半小时自动播报一次（默认 7:00 ~ 22:30，可在配置中调整），内容包含日期、星期、时间
- **常驻链路**：后台线程 Ping 保活 + 下行流量泵，服务器视角与长在线的真实用户一致
- **准点对齐**：播报前一分钟预热（TTS 预合成 + 音频包预构建 + 预建链），准点前 0.5s 才抢麦（不提前占用信道），首包紧贴整点发出
- **三级兜底**：常驻链路 → 临时短链 → 准点现场完整流程，任一环节故障自动降级，播报不中断
- **TTS 蓄水池**：每小时预合成未来 48 小时所有播报时段的音频，准点播报不依赖播报时刻的 Edge-TTS 网络状态
- **音频质量保障**：TTS 缓存经 libmpg123 完整解码 dry-run 校验（拦截头部损坏/尾部截断），播报前头尾静音自动裁切
- **企业微信告警**：日志达到指定级别时自动推送到企业微信群机器人（配置中留空即禁用）
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
### 2. 配置文件（唯一需要改的地方）
**所有参数都在 `config.json` 中，代码无需任何修改。**
```bash
cp config.example.json config.json   # 复制示例配置为实际配置
vi config.json                       # 编辑你的参数
```
最少需要修改三处：
```jsonc
{
  "talk": {
    "username": "你的滔滔链路账号",   // 必填
    "password": "你的滔滔链路密码"    // 必填
  },
  "announce": {
    "template": "CQ CQ CQ，现在是{year}年{month}月{day}日……",  // 播报内容（中继台呼号/频率/亚音）
    "start_hour": 7,  // 每日首次播报时刻
    "end_hour": 22    // 每日最后一次播报所在小时
  },
  "notify": {
    "webhook_url": ""  // 可选：企业微信机器人 Webhook，留空禁用告警
  }
}
```
> **生产/开源同步**：代码仓库不提交任何真实配置。生产部署时将实际参数写入本地 `config.json`（已被 `.gitignore` 忽略，不会误提交）。代码升级只需 `git pull` 同步代码，配置文件保持不变，两侧参数互不影响。
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
   - 将本项目的所有文件（`announce.py`、`direct_announce.py`、`Dockerfile`、`docker-compose.yml`、`start.sh`、`config.json`）上传到飞牛NAS 的共享文件夹中，例如 `/vol1/docker/ham-repeater-bot/`
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
`config.json` 完整结构（所有字段均带默认值，缺省项自动回退）：
| 配置段 | 字段 | 说明 |
|--------|------|------|
| `talk` | `host` / `port` | 滔滔链路服务器地址与端口（默认 totalkd.allptt.com:59638） |
| `talk` | `username` / `password` | 滔滔链路账号（必填） |
| `talk.client_profile` | `release` / `os_name` / `os_version` / `model` | 客户端画像（对齐真实手机 App，UserState 广播用） |
| `tts` | `voice` | Edge-TTS 语音角色（默认 zh-CN-XiaoxiaoNeural） |
| `tts` | `timeout_inner` / `timeout_join` | TTS 协程/线程超时（秒） |
| `tts` | `max_retries` / `retry_delay` | 合成重试次数与间隔 |
| `tts` | `cache_expire_days` | TTS 缓存过期天数（默认 2） |
| `tts` | `prefill_hours` | TTS 蓄水池提前量（默认 48 小时） |
| `announce` | `template` | 播报模板（支持 {year}/{month}/{day}/{weekday}/{hour}/{minute_text} 占位符） |
| `announce` | `start_hour` / `end_hour` | 每日播报时段（每半小时一次） |
| `timing` | `native_prep_lead` | 准点前建链提前量（默认 2.5s） |
| `timing` | `native_mic_lead` | 准点前抢麦提前量（默认 0.5s） |
| `timing` | `lead_delay` | UserTalking→首包建链间隔（默认 0.5s） |
| `timing` | `packet_period` | 语音包发送间隔（默认 0.12s，即 120ms） |
| `timing` | `tail_flush` | 发包后尾巴冲刷时长（默认 0.3s，调小可缩短结尾空白） |
| `audio` | `opus_bitrate` | Opus 编码码率（默认 12000bps CBR） |
| `audio` | `trim_silence` / `trim_threshold` / `trim_min_ms` | 头尾静音裁切开关与参数 |
| `paths` | `cache_dir` / `log_dir` | TTS 缓存与日志目录 |
| `logging` | `max_bytes` / `backup_count` | 日志轮转大小与备份数 |
| `notify` | `webhook_url` | 企业微信机器人 Webhook（留空禁用） |
| `notify` | `webhook_log_level` | 推送级别（INFO/WARNING/ERROR） |
### 配置加载优先级
1. 环境变量 `HAM_BOT_CONFIG` 指定配置文件路径
2. 未设置时默认读取脚本同目录 `config.json`
3. 缺失/非法时回退到代码内置默认值（并打印告警）
```bash
# 显式指定配置文件启动（Docker 内可改 docker-compose.yml 的 environment）
HAM_BOT_CONFIG=/app/config.json python3 announce.py
```
### 播报时间
```jsonc
"announce": {
  "start_hour": 7,   // 每日首次播报时刻（整点）
  "end_hour": 22     // 每日最后一次播报所在小时（22 表示最后一次为 22:30）
}
```
播报规则：每半小时一次，XX:00 和 XX:30 各播报一次。每次播报前一分钟（XX:29 / XX:59）自动预热（预合成 TTS + 预构建音频包 + 预建链）；每天首次播报由 (START-1):59 预热；收盘播报后仅保留常驻链路保活，不空转。
### 直连时序
```jsonc
"timing": {
  "native_prep_lead": 2.5,   // 准点前该秒数建链+登录（实测建链 ~1s）
  "native_mic_lead": 0.5,    // 准点前该秒数才发起抢麦（不提前占麦）
  "tail_flush": 0.3          // 发包后尾巴冲刷（调小可缩短播报结尾空白）
}
```
抢麦提前量 0.5s 是刻意设计：提前抢麦会让频道内其他用户看到"说话中"状态长时间静默。抢麦服务器响应约 1.1s，实际首包落在准点后 ~1.1s，其中 UserTalking 与首包保持 0.5s 官方间隔（中继台链路设备靠该间隔建立转发）。
### TTS 合成与缓存校验
```jsonc
"tts": {
  "voice": "zh-CN-XiaoxiaoNeural",   // Edge-TTS 语音角色
  "cache_expire_days": 2,            // TTS 缓存过期天数
  "prefill_hours": 48                // TTS 蓄水池提前量（小时）
}
```
- **完整性校验**：缓存命中/合成完成时，使用 libmpg123 完整解码 dry-run 校验（识别头部损坏 + 尾部截断不完整），损坏文件自动删除重合成——不依赖文件大小判断。
- **静音裁切**：音频编码前自动裁切头尾静音（默认各留 50ms 余量），缩短播报时长、避免空白。
TTS 蓄水池机制：播报文本完全由日期+时间决定，可提前计算。服务每小时检查并预合成未来 `prefill_hours` 内所有播报时段缺失的音频，使准点播报不依赖播报时刻的 Edge-TTS 网络状态——网络突发故障（分钟到小时级）不再导致播报失败，只有连续中断超过一天才可能缺音。
### 日志与 Webhook
```jsonc
"logging": {
  "max_bytes": 262144,    // 单日志文件最大 0.25MB
  "backup_count": 40      // 保留最近 40 个备份
},
"notify": {
  "webhook_url": "",      // 企业微信机器人 Webhook，留空禁用
  "webhook_log_level": "WARNING"   // 推送级别：WARNING=仅告警+错误
}
```
## 点名主播（net_control，可选）
在中继台上自动主持业余无线电点名活动：纯 ASR + 本地状态机 + LLM 信息提取（可选）+ 独立 TTS。

**与播报的关系**：复用同一条常驻直连链路（`direct_announce.PersistentAnnouncer`）作为接收侧与发射侧；点名期间跳过整点播报与预热抢麦（`busy` 锁互斥，同一时刻只有一个发射者）。启用点名**不影响**现有定时播报——`net_control.enabled=false`（默认）时完全不走点名路径。

**工作原理**：
1. 接收：链路下行泵 → Opus 解码（`OpusDecoder`，ctypes 直调镜像内 libopus0）→ 话音检测（VAD）切段 → WAV 落盘（16k 单声道，供复盘）
2. 识别：阿里云百炼 **qwen3-asr-flash**（OpenAI 兼容非流式接口，System Message 传实体词表提升呼号识别，`enable_itn` 归一化信号报告数字）
3. 提取：**字母解释法词表确定性解码**（ITU 26 词 + 中文音译变体，config 可扩展）→ 呼号格式校验（快路径原文直读 + 慢路径词表装配）→ 已抄收去重 → 低置信度播报"请重复一遍呼号"（限次）→ 仍失败记未抄收
4. 可选 LLM 兜底：智谱 **GLM-4.5-Flash**（免费）低置信度修复/备注提取（默认关闭，主路径为确定性解码）
5. 播报：Edge-TTS 合成（全文 md5 缓存 + libmpg123 校验，与播报同款）→ 复用链路抢麦发包

**两种点名模式**：
- 开放点名（默认 `roster_mode=false`）：CQ 开场 → 收听窗口内逐个抄收 → 汇总 → 结束（参与者不可预知，不依赖封闭名单）
- 固定名单（`roster_mode=true`）：逐个呼叫名单成员 → 超时跳过 → 汇总

**启用步骤**：
1. `config.json` 中 `net_control.enabled=true`，填 `asr.api_key`（百炼）、点名时段（`weekday/hour/minute`）；可选填 `llm.api_key`
2. 需要**独立的滔滔测试账号**：与播报共用同一账号会互踢（README 明载）
3. 重启服务，按计划时段自动开始点名；识别摘要与应答录音落盘在 `net_records/`（已 gitignore）

**离线自测**（无需网络/Key）：
```bash
python3 net_control.py --decode "Bravo Hotel Three X-ray X-ray 信号五九"   # 解释法解码
python3 net_control.py --opus-roundtrip                                     # Opus 编解码往返
python3 tests/run_tests.py && python3 tests/test_session.py                 # 全部单元测试
```

**话术模板占位符**（`net_control` 段所有话术字段可用；模板按 TTS 优化编写，可直接当语音念）：

| 占位符 | 含义 | 示例值 |
|---|---|---|
| `{date}` | 今天日期 | 2026年9月17日 |
| `{weekday}` | 星期 | 周四 |
| `{time}` | 北京时间 | 20点00分 |
| `{net_name}` | 点名活动名 | 应急通讯演练台网点名 |
| `{repeater_call}` | 中继台呼号 | BR9AB |
| `{ctrl_call}` / `{ctrl_phonetic}` | 主控呼号/解释法 | BI9BZW / Bravo India Nine... |
| `{main_qth}` `{main_device}` `{main_antenna}` `{main_power}` | 主控 QTH/设备/天线/功率 | 咸阳市渭城区塔尔坡 / 泉盛K6... |
| `{frequency}` `{offset}` `{tone}` | 频率/下差/亚音（兆赫/赫兹，可拼进开场白） | 439.775 / 7 / 88.5 |
| `{call}` / `{call_phonetic}` | 应答方呼号/解释法 | BH3XX / Bravo Hotel Three X-ray X-ray |
| `{report}` | 信号报告 | 59 |
| `{n}` / `{calls}` | 抄收人数/呼号列表 | 9 / BH3XX、BG9ABC |

TTS 优化要点：呼号一律用解释法英文单词（`{call_phonetic}`），中文语音读单词比读字母串稳定；时间写"北京时间{time}"；频率/亚音/功率写中文单位（兆赫/赫兹/瓦）；保留 CQ、Over、73 国际惯例词。铜川变体开场白示例见 `config.example.commented.json`。

完整配置字段见 `config.example.commented.json` 的 `net_control` 段（全部带默认值，缺省即兜底）。

## 数据目录
`docker-compose.yml` 将项目目录挂载到容器 `/app`，运行时会在 `paths.cache_dir` 与 `paths.log_dir` 指定目录下自动创建子目录：
| 子目录 | 说明 |
|--------|------|
| `tts_cache/` | TTS 合成音频缓存 |
| `logs/` | 运行日志（自动轮转） |
| `net_records/` | 点名识别摘要与应答录音（启用 net_control 后生成） |
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
