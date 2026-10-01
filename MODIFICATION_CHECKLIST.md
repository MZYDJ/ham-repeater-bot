# 修改检查清单（SOP）— ham-repeater-bot

> **用途**：程序跨文件耦合（代码默认值 / 示例配置 / README 文档 / 测试 / 部署脚本 / 版本标记），
> 任何修改都可能需要同步多个地方。做任何修改前先定位本清单对应场景，逐项检查，
> **防止"改了一处、漏了另一处"**。本文件自身也是仓库产物，随每次发布同步更新。

---

## 0. 总原则

**一处修改，五处确认**：
1. **代码**（announce.py / direct_announce.py / net_control.py / 部署脚本）——改动的本体
2. **示例配置**（`config.example.json`）——仅当新增/删除**必填项或常用项**时才需要动；
   改默认值时示例文件**通常不动**（缺省项由代码默认值接管）
3. **README**（配置说明表 / 各功能段）——字段、默认值、语义描述必须与代码一致
4. **测试**（tests/ 四套件）——新增逻辑必须补用例；改默认值需检查是否有锁定断言的用例
5. **版本标记**（BUILD_TIME）——见 §1-P3

---

## 1. 发布固定流程（任何修改完成后必须走完）

| 步骤 | 动作 | 命令/位置 | 失败后果 |
|---|---|---|---|
| P1 | 全量四套件测试 | `python3 tests/test_announce.py`、`python3 tests/test_watchdog.py`、`timeout 110 python3 tests/run_tests.py`、`timeout 60 python3 tests/test_session.py` | 有回归即停止，先修 |
| P2 | 语法编译 | `python3 -m py_compile announce.py direct_announce.py net_control.py`（及改动的 py 文件） | 语法错误 |
| P3 | 版本标记 | ①**重建镜像场景**：Dockerfile 已自动生成 `/opt/build_time`，无需手动；②**zip 覆盖场景**：打包前现场生成时间戳文件 `BUILD_TIME`（内容=`date '+%Y-%m-%d %H:%M'`，放入 zip 根，即容器 `/app/BUILD_TIME`） | 线上 BUILD_TIME 陈旧，无法判断跑的是哪版 |
| P4 | 双分支提交+推送 | `feat/net-control`（点名版主线）+ `main`（纯播报版）各 commit + push（PAT 走 Bash+git，GitHub MCP 仅读不写） | 双分支不同步 |
| P5 | 交付通道选择 | **push 成功 → 主交付 = GitHub 链接/commit hash，不出 zip**；用户服务器 `git pull` 同步（docker-compose 挂载宿主目录，pull 即覆盖 /app）。**仅当 GitHub push 失败（不可达/鉴权失败）→ 兜底打包 zip**：`zip -r <包>.zip $(git ls-files)` + `BUILD_TIME` 时间戳文件入根 | 用户拿不到新代码 |
| P6 | present_files 交付 | push 成功：交付 commit hash + GitHub 链接（zip 可省）；push 失败：交付 zip 链接（https://aka.doubaocdn.com/...） | 用户拿不到新代码 |
| P7 | 服务器更新重启 | push 通道：用户 `cd <宿主部署目录> && git pull && docker restart announce`；zip 通道：覆盖 `/app` 后重启 | 旧代码继续运行 |
| P8 | 线上日志验证 | `docker logs announce --since "30s"` 检查：`[ENV] BUILD_TIME: /app=... \| /opt=... \| env=...`（应为本次时间戳）、`[ENV] config.example.json: OK`、Scheduler started、首个准点播报完成 | 没验证=没交付完 |

---

## 2. 修改类型 → 检查点矩阵

### A. 改播报文案 / 模板（整点播报内容）
- [ ] `announce.py`：默认模板常量（若改默认值）；`get_announce_text` 占位符逻辑（若新增占位符）
- [ ] `config.example.json`：`announce.template`（必填项，改模板必须同步）
- [ ] README：配置说明表 `announce.template` 行；若新增占位符，更新模板占位符说明
- [ ] 测试：`test_announce` 文案渲染用例（新增占位符时必须补）
- [ ] 服务器：`/app/config.json` 的 `announce.template`（用户模板在服务器，代码默认模板改了不影响线上，但需提示用户是否同步）
- ⚠️ 易漏：README "播报时间"段的 jsonc 示例若含模板片段

### B. 改时序参数（timing 段：native_prep_lead / native_mic_lead / lead_delay / packet_period / tail_flush）
- [ ] `announce.py`：对应常量（如 `NATIVE_MIC_LEAD = cfg_get(... default=...)`）
- [ ] README：配置说明表对应行 **默认值必须同步**；"直连时序"段语义描述（历史教训：0.5→1.0 时 README 曾过期）
- [ ] 测试：`test_announce` 是否已有锁定默认值的断言（如 `NATIVE_MIC_LEAD == 1.0`）——改默认值必须同步断言
- [ ] `config.example.json`：**通常不动**（timing 段已精简移除）；若曾显式写入则删除该行
- [ ] 服务器：检查 `/app/config.json` 是否显式写入该项（显式值覆盖默认值！）——提示用户删行或改值
- ⚠️ 易漏：README 有两处（配置表 + 直连时序 jsonc 示例），都可能残留旧值

### C. 改看门狗（watchdog 段）
- [ ] `announce.py`：`_watchdog_decision` 判定顺序、`_notify_then_restart` 冷却、打点收敛
- [ ] README：配置说明表 watchdog 行；看门狗章节描述
- [ ] 测试：**`test_watchdog` 是重点**——判定顺序逐分支补用例（点名中/时段外/容忍窗口/重启前准点/已成功/点名跳过/TTS窗口/重启）
- [ ] `config.example.json`：通常不动（watchdog 段已精简移除）
- ⚠️ 易漏：冷却逻辑在 `_notify_then_restart` 内（不在纯函数里），改它要同步补测试

### D. 改点名（net_control）
- [ ] `net_control.py`：点名状态机 / 文案模板默认值 / ASR-LLM 流程
- [ ] README：配置说明表 net_control 全字段行；点名文案占位符说明
- [ ] 测试：`test_watchdog`（点名中豁免分支）、`test_session`（会话流程）、`test_announce`（若涉及 DA 纯函数）
- [ ] `config.example.json`：`net_control` 段的开关与开场/结束文案（必填/常用项）
- [ ] 服务器：`/app/config.json` 的 net_control 段（用户已配置，提示是否同步新增字段）
- ⚠️ 易漏：net_control 文案占位符（{call}/{call_phonetic}/{report} 等）改模板时 README 占位符表要同步

### E. 改 TTS / 蓄水池
- [ ] `announce.py`：合成函数 / 蓄水池 `tts_prefill_task` / 缓存校验
- [ ] README：配置说明表 tts 行；"TTS 合成与缓存校验"段
- [ ] 测试：`test_announce` 的 TTS 缓存/合成/蓄水池用例
- [ ] `start.sh`：若改引擎/依赖，同步其 cosyvoice/edge 自检逻辑
- ⚠️ 易漏：`start.sh` 的依赖自检（dashscope/edge-tts 版本提示）与 Dockerfile 依赖安装是两处

### F. 新增 / 删除配置项
- [ ] 代码：`cfg_get` 默认值（默认值=唯一权威）
- [ ] `config.example.json`：**仅当必填/常用项**才加（否则不加，靠默认）
- [ ] README：配置说明表加/删对应行（默认值写准）
- [ ] 测试：`test_config_defaults`（run_tests）或新断言
- [ ] 服务器：提示用户其 `/app/config.json` 无需动（除非想覆盖默认值）
- ⚠️ 易漏：README 配置表行与代码默认值不一致（本项目历史最高发错误）

### G. 改直连协议（direct_announce）
- [ ] `direct_announce.py`：协议构造/解析/发包（take_mic / play / pump / build_audio 等）
- [ ] 测试：`test_announce` 的 DA 纯函数用例（dry_validate_mp3 / trim_silence / parse_udp_voice_multi / build_audio）
- [ ] README："直连时序"段（若改时序语义）
- ⚠️ 易漏：`net_control` 通过局部 import 使用 DA 的函数，改 DA 签名要看 net_control 调用点

### H. 改构建 / 部署（Dockerfile / docker-compose / start.sh）
- [ ] `Dockerfile`：依赖安装（改包后必须确认 start.sh 自检段同步）
- [ ] `docker-compose.yml`：镜像名 / 挂载 / 环境变量（**BUILD_TIME 已移入 Dockerfile 自动生成，此处不再需要**）
- [ ] `start.sh`：环境自检 / 配置文件校验 / BUILD_TIME 读取优先级
- [ ] 测试：`bash -n start.sh` 语法检查；本地模拟运行验证输出
- [ ] README：依赖安装段（如需）
- ⚠️ 易漏（历史教训）：改 Dockerfile 装包后忘记同步版本标记——现已由 `/opt/build_time` 自动生成解决；改 compose 后忘记更新 `image:` 标签

### I. 改文档（README）
- [ ] 自查引用一致性：`grep -rn "config.example\|BUILD_TIME\|native_mic_lead\|0.5s\|1.1s" README.md` 确保无过期数字/死引用
- [ ] 配置说明表与代码默认值逐行核对
- ⚠️ 易漏：README 被三处引用（部署文档/配置表/功能段），改一处忘另一处（历史实例：配置表改 1.0 但"直连时序"段还写 0.5）

---

## 3. 历史易漏点固化清单（每次发布前过一遍）

- [ ] README 配置说明表 / 功能段 / jsonc 示例，与代码默认值**全部一致**（数字、单位、语义）
- [ ] `config.example.json` 是**最小可用版**（不含已精简的 timing/watchdog/logging 等段）；若代码新增必填项，确认已补进示例
- [ ] 服务器 `/app/config.json` 无显式旧值覆盖新默认（尤其 timing.native_mic_lead 曾为 0.5）
- [ ] BUILD_TIME 已更新（zip 包内带新时间戳文件；重建镜像则 Dockerfile 自动生成）
- [ ] `.gitignore` 含 `.logs/`、`BUILD_TIME`、`config.json`（防运行时文件误提交）
- [ ] 双分支 HEAD 一致（feat/net-control 与 main 的对应改动都已 push）
- [ ] zip 内不含 `.git/`、`.logs/`、`__pycache__/` 等运行时目录

---

## 4. 测试矩阵

| 套件 | 命令 | 覆盖范围 | 何时必须跑 |
|---|---|---|---|
| test_announce | `python3 tests/test_announce.py` | 文案/时间纯函数、TTS 缓存/合成/蓄水池、调度入队、native 链路与预热六分支、announce_task、DA 纯函数、WebhookHandler、config.example 合法性 | 任何代码/配置/README 改动 |
| test_watchdog | `python3 tests/test_watchdog.py` | 看门狗 `_watchdog_decision` 纯函数全分支、冷却、恢复通知 | 看门狗/点名/调度相关改动 |
| run_tests | `timeout 110 python3 tests/run_tests.py` | 集成级：调度、播报、net_control 场景、配置默认值回退 | 任何改动（全量回归） |
| test_session | `timeout 60 python3 tests/test_session.py` | 点名会话状态机流程 | 点名（net_control）改动 |
| py_compile | `python3 -m py_compile <改动的py>` | 语法 | 每次 |

全绿基准：**376 项**（86 + 56 + 222 + 12）。新增用例后总数应同步上升。

---

## 5. 交付后服务器侧验证命令

```bash
docker logs announce --since "30s" | grep -E "BUILD_TIME|config.example|Scheduler started|自动播报服务启动成功"
docker logs announce --since "30s" | grep "BUILD_TIME: /app="   # 三来源：/app=发布时间 | /opt=镜像构建 | env=旧镜像
docker logs announce -f            # 盯到首个准点播报完成（XX:00 / XX:30）
grep -c native_mic_lead /app/config.json   # 应输出 0（旧显式值已删）或 1.0
```
