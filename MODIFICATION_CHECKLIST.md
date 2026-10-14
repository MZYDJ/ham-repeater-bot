# 修改检查清单（SOP）— ham-repeater-bot

> **用途**：程序跨文件耦合（代码默认值 / 示例配置 / README 文档 / 测试 / 部署脚本 / 版本标记），
> 任何修改都可能需要同步多个地方。做任何修改前先定位本清单对应场景，逐项检查，
> **防止"改了一处、漏了另一处"**。本文件自身也是仓库产物，随每次发布同步更新。
>
> **分支定位（重要）**：`feat/net-control` = 点名版主线（开发/部署主力）；
> `main` = **纯播报版定格线**（已恢复至最后一个纯播报 commit `7d3432b`，不再随 feat 演进，
> 见下"分支同步规则"）。

---

## 0. 总原则

**一处修改，五处确认**：
1. **代码**（announce.py / direct_announce.py / net_control.py / 部署脚本）——改动的本体
2. **示例配置**（`config.example.json`）——仅当新增/删除**必填项或常用项**时才需要动；
   改默认值时示例文件**通常不动**（缺省项由代码默认值接管）
3. **README**（配置说明表 / 各功能段）——字段、默认值、语义描述必须与代码一致
4. **测试**（tests/ 四套件）——新增逻辑必须补用例；改默认值需检查是否有锁定断言的用例
5. **版本标记**（BUILD_TIME）——见 §2-P3

---

## 0.1 标准代码修改 SOP（默认工作流程）

**强制入口**：任何 AI 辅助开发任务开始前，必须先阅读目标分支上的 `AGENTS.md` 与本文件；每次后续修改也必须重新对照本 SOP，不依赖对话记忆。PR 创建者应填写 `.github/pull_request_template.md` 中的检查清单。GitHub Actions 会检查 SOP 文件是否存在并运行回归测试，但不能替代人工判断分支同步、生产验收和最终 diff。

本 SOP 是 `feat/net-control` 后续所有代码修改任务的默认执行流程。需求明确时应连续推进，不必在每个小步骤停下来等待用户确认；涉及不可逆操作、生产风险、权限不足或需要用户作业务决策时，必须先说明风险并取得必要确认。

1. **需求与状态确认**
   - 确认仓库 `MZYDJ/ham-repeater-bot`、目标分支（默认 `feat/net-control`）、当前远端 HEAD、工作区/暂存区状态和本次验收标准。
   - 查看相关代码、配置、测试、文档及调用关系；确认 GitHub 读取、写入与提交通道可用，避免完成修改后才发现无法交付。
   - 若任务明确指定其他分支或环境，以该任务的明确要求为准；不得假定分支或工作区状态未变化。

2. **方案与风险分析**
   - 修改前列出受影响文件、兼容性要求、失败处理、测试策略、部署影响及回滚方式。
   - 分析是否影响 `main` 纯播报定格线，并严格按 §1 的分支同步规则处理。
   - 分支重建、定格、force push 等不可逆操作，必须先向用户说明方案与代价并获得明确授权。

3. **一次性实施**
   - 尽可能在同一交付批次完成代码、回归测试、README、`config.example.json`、部署/CI 配置及本清单的同步更新。
   - 遵循本文件各修改类型矩阵，避免只改实现而遗漏默认值、文档、测试或部署自检。
   - 不提交密钥、生产配置、运行时文件或与任务无关的改动。

4. **分层验证**
   - 先执行语法/格式等快速检查和受影响模块测试，再执行本文件 §5 要求的完整回归测试。
   - 互不依赖的检查可并行执行；存在依赖关系的步骤按顺序执行。
   - 模拟测试与真实服务验证必须区分：mock 通过不能代替真实 API、网络、音频或生产配置验证。

5. **审查并提交**
   - 提交前检查最终 diff、文件清单、敏感信息、测试覆盖和目标分支，确认没有无关修改或意外删除。
   - 默认只提交并推送 `feat/net-control`；是否同步 `main` 依 §1 判定。优先使用已验证可写的 GitHub Connector/API，失败时再使用具备授权的备用通道。
   - 记录实际提交 SHA 与链接；不得把未成功的写入描述为已提交。

6. **核验最终 CI**
   - 必须核验与**最新提交 SHA** 对应的 GitHub Actions/CI 运行，而不是只引用较早一次成功的运行。
   - 等待所有必需工作流完成；检查失败步骤并在安全范围内修复、重跑，再次核验最终提交的结果。
   - CI 尚在运行、被跳过或无法查询时，明确标注状态，不得称为“CI 全绿”。

7. **部署与生产验收**
   - 部署前确认配置、密钥来源、挂载目录、版本标记和回滚路径；生产部署或可能实际发射无线电语音的操作要控制风险。
   - 优先执行不产生实际无线电发射的独立冒烟测试（例如只合成并校验音频），再检查服务启动、调度器、业务日志和关键功能。
   - 未实际访问目标环境或没有真实运行结果时，只能报告“待生产验收”；不能用 CI 结果推断生产已正常。

8. **最终交付报告**
   - 明确列出修改内容、文件、目标分支、提交 SHA/链接、测试结果、最新 CI 结果、生产验收结果和未验证事项。
   - 分别使用以下状态，不能混为一谈：
     - **代码完成**：修改完成且最终 diff 已审查。
     - **CI 通过**：最新提交对应的必需 CI 全部成功。
     - **生产验收通过**：目标运行环境已实际验证关键业务功能。
     - **待验证/受阻**：尚未执行、无法访问或缺少证据的项目及其下一步。
   - 只要求用户执行不可由当前工具完成的必要操作；给出可直接复制的最少命令和需要回传的结果。

## 1. 分支同步规则（feat/net-control ↔ main）

### 1.1 分支定位
- **feat/net-control**：点名版主线，日常开发、测试、部署全部在此分支。
- **main**：纯播报版**定格线**。已恢复至最后一个纯播报 commit `7d3432b`（v12 配置独立版，
  无 net_control 点名代码、无点名测试），此后**默认不再随 feat 演进**，仅当出现"真正影响
  播报功能"的修复时才按本规则同步。

### 1.2 同步判定（唯一标准：改动是否**真正影响播报功能**）
"真正影响播报" = 改动作用于播报执行链且**不是点名驱动的**：
`TTS 合成/缓存/蓄水池 → 调度入队 → 预热/预建链 → 抢麦/发包 → 看门狗 → 播报文案/配置加载`。

**需要同步 main（播报核心/通用且非点名驱动）**：
- 播报执行链 bug 修复（如 `Client.send` 超时、`pump` 超时、看门狗纯播报逻辑、抢麦时序）
- 通用部署改进（BUILD_TIME 机制、示例配置校验）——**前提是点名无关**
- 通用文档（README 播报/时序/TTS/部署段、config.example 的 talk/tts/announce/notify 段）

**不同步 main（点名驱动，即使改动落在共享文件里）**：
- `net_control.py` 的任何改动
- `announce.py` 里点名集成（点名调度/会话/点名中看门狗豁免/点名跳过打点）
- `start.sh` / `Dockerfile` 里点名专属自检与依赖（cosyvoice 自检段、dashscope 若仅为点名 TTS）
- README / config.example 里点名配置段（net_control 字段表、点名文案占位符、点名功能说明）
- 点名测试（tests/test_session.py、test_watchdog 点名分支、test_announce 点名相关断言）

### 1.3 操作流程
1. 开发/修复默认只在 **feat** 完成并全量测试。
2. 按 1.2 判定改动类别：点名相关 → **不同步**，结束；播报核心修复 → 进入 3。
3. **混合 commit 一律禁止整体 cherry-pick**（历史教训：`9197bed` 一个 commit 同时改
   announce/direct_announce/net_control，整体同步会把点名部分倒进 main）。混合 commit
   按以下命令序列只摘**播报相关文件级改动**：
   ```bash
   git cherry-pick -n <sha>            # 应用全部改动到暂存区（不提交）
   git checkout main -- <点名文件>     # 还原点名专属文件（如 net_control.py）
   git diff --cached --stat            # 核对剩余暂存内容全部播报相关
   git commit -m "fix: <播报部分>（来自 <sha> 混合 commit 拆分）"
   ```
   共享文件（announce.py / direct_announce.py / start.sh）按 §1.2 逐文件判定：播报部分保留、
   点名段还原。
4. 同步前先评估冲突面（**注意：`git diff main feat` 是全量累积差异——72 个 commit、announce.py
   单文件 586 行，不能用来评估单个修复的冲突**）。正确做法：
   ```bash
   git show <sha> --stat               # ① 看该改动本身改了什么
   git cherry-pick -n <sha>            # ② 试应用，实际冲突一目了然（冲突即停）
   git cherry-pick --abort             # ③ 试完回滚，再决定怎么走
   ```
   main 定格线很老（7d3432b 无 tests/config.example），feat 演进大，**cherry-pick 几乎必然
   冲突**（实测首个播报 commit 即 announce.py 自动合并失败）。处理路径二选一：
   - 冲突小 → cherry-pick 后手工解冲突；
   - 冲突大 → **手工移植**（按 1.2 判定逐文件摘播报改动），或**明确"不同步"**——
     "不同步"是合法决策，不为了同步而同步。
5. 同步后强制复核（**单向检查：main 侧必须无点名内容**；"差异仅限点名文件"是双向对称表述，
   在 main 定格未同步播报修复时永远不成立，弃用）：
   ```bash
   git ls-tree main --name-only | grep -E "net_control|capture"   # 应无输出（无点名文件）
   git show main:announce.py | grep -cE "net_control|点名"        # 应为 0（无点名集成）
   ```
6. main 无独立测试套件（定格线 7d3432b 不含 tests/ 目录）——同步播报修复时必须**将对应
   测试一并带入**，否则 main 无回归保护；带入后跑 main 侧测试子集（不含点名用例）。

> ⚠️ 共享文件备注：`start.sh` 的 `engine` 默认值为 `cosyvoice`（点名 TTS 默认落在共享文件里）。
> 同步 start.sh 播报相关部分时，cosyvoice 自检段（engine/asr_key/cosyvoice_voice 检查）属点名
> 驱动，按 §1.2 不同步；main 定格线的 start.sh 无此段，不受影响。

### 1.4 分支级操作纪律（恢复/定格/force push）
分支恢复、定格、force push 均为**不可逆**动作，动工前必须：
- [ ] **方案前置确认**：先向用户列出候选方案（如 A 重建 / B 定格 / C 不同步）及各自代价，
      用户明确拍板后再动手，**禁止按自己理解直接开工**（教训：先跑方案 A 半程被纠正，浪费工作量）
- [ ] `git status` 确认工作区/暂存区干净（或先 stash/提交，避免丢失未提交工作）
- [ ] 明确目标 commit 与当前 HEAD 的差异预期（`git log --oneline -5`）
- [ ] force push 属不可逆操作，必须**用户明确授权**，push 后立即
      `git ls-remote` 核对远端 ref（main/feat HEAD 与预期一致）
- [ ] 完成后在交付说明中写明：恢复到的 commit、是否 force push、验证结果

---

## 2. 发布固定流程（任何修改完成后必须走完）

| 步骤 | 动作 | 命令/位置 | 失败后果 |
|---|---|---|---|
| P1 | 全量四套件测试 | `python3 tests/test_announce.py`、`python3 tests/test_watchdog.py`、`timeout 110 python3 tests/run_tests.py`、`timeout 60 python3 tests/test_session.py` | 有回归即停止，先修 |
| P2 | 语法编译 | `python3 -m py_compile announce.py direct_announce.py net_control.py`（及改动的 py 文件） | 语法错误 |
| P3 | 版本标记 | ①**重建镜像场景**：Dockerfile 已自动生成 `/opt/build_time`；②**zip 覆盖场景**：打包前生成 `BUILD_TIME`（内容=`date '+%Y-%m-%d %H:%M'`，放入 zip 根，即 `/app/BUILD_TIME`）；③**git pull + 宿主目录挂载场景**：在宿主部署目录执行 `date '+%Y-%m-%d %H:%M' > BUILD_TIME`，该文件被 `.gitignore` 忽略，不会提交，但会通过挂载映射到 `/app/BUILD_TIME` | 线上 BUILD_TIME 陈旧，无法判断跑的是哪版 |
| P4 | 提交+推送（按 §1 判定分支） | 默认只提交并 push `feat/net-control`；仅当本次改动按 §1.2 判定为**真正影响播报**的修复时，才 cherry-pick 同步 `main`（纯播报定格线）并 push。优先使用当前已验证可写的 GitHub Connector/API；若写入失败，再按授权情况使用 PAT + Bash/git | main 该同步的没同步 / 点名改动误进 main |
| P5 | 交付通道选择 | **push 成功 → 主交付 = GitHub 链接/commit hash，不出 zip**；用户服务器 `git pull` 同步（docker-compose 挂载宿主目录，pull 即覆盖 /app）。**仅当 GitHub push 失败（不可达/鉴权失败）→ 兜底打包 zip**：`zip -r <包>.zip $(git ls-files)` + `BUILD_TIME` 时间戳文件入根 | 用户拿不到新代码 |
| P6 | present_files 交付 | push 成功：交付 commit hash + GitHub 链接（zip 可省）；push 失败：交付 zip 链接（https://aka.doubaocdn.com/...） | 用户拿不到新代码 |
| P7 | 服务器更新重启 | push 通道：用户 `cd <宿主部署目录> && git pull && docker restart announce`；zip 通道：覆盖 `/app` 后重启 | 旧代码继续运行 |
| P8 | 线上日志验证 | `docker logs announce --since "30s"` 检查：`[ENV] BUILD_TIME: /app=... \| /opt=... \| env=...`（应为本次时间戳）、自检无 `[ERROR]`（fail-fast 未触发）、Scheduler started、首个准点播报完成 | 没验证=没交付完 |

---

## 3. 修改类型 → 检查点矩阵

### TTS 引擎配置隔离（定时播报与点名）
- [ ] `tts.engine` 仅控制点名系统；定时播报独立读取 `announce.tts_engine`，缺省必须为 `edge`。
- [ ] 定时播报切换引擎时，不得改变点名引擎；两套系统缓存前缀隔离（点名 `nc_`，定时播报 CosyVoice `announce_`）。
- [ ] `start.sh` 分别校验两个引擎的取值；只有任一系统启用 CosyVoice 时才要求百炼密钥与音色。
- [ ] `tests/test_announce.py` 覆盖两个引擎配置互相冲突时定时播报仍遵循 `announce.tts_engine`。



> 分支归属：A/B/C/E/F/G/H/I 中**影响播报执行链**的部分 → 按 §1 同步 main（cherry-pick）；
> **D（点名）→ 仅 feat，任何情况下不同步 main**；点名驱动落在共享文件里的改动也按 §1.2 不同步。

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
- [ ] `announce.py`：合成函数 / 蓄水池 `tts_prefill_task` / 缓存校验；`PREFILL_ERROR_THRESHOLD_SECONDS` 缺省为 86400 秒（24 小时），远期最终失败 `WARNING`、进入阈值窗口后 `ERROR`
- [ ] 若定时播报复用点名 TTS：核对 `tts_cache_path` 与 `net_control.synth_text` 的缓存键/目录完全一致；测试共享缓存、合成失败、短音频删除与重试
- [ ] README：配置说明表 tts 行；"TTS 合成与缓存校验"段；快速开始中写清 CosyVoice API Key 与音色要求；同步记录 `announce.prefill_error_threshold_seconds` 默认值与级别语义
- [ ] 测试：`test_announce` 的 TTS 缓存/合成/蓄水池用例（包括默认阈值 86400 秒、远期 WARNING、临近 ERROR）；所有 mock Edge-TTS 行为的测试（包括 `test_watchdog`）应显式设 `tts.engine=edge`，避免依赖生产默认值
- [ ] `start.sh`：若改引擎/依赖，同步 cosyvoice/edge 自检逻辑；CosyVoice 成为默认引擎后，即使 `net_control.enabled=false` 也必须检查 API Key 与音色配置
- ⚠️ 易漏：`start.sh` 的依赖自检与 Dockerfile 依赖安装是两处；CosyVoice SDK 缺失时可走 HTTP 回退，不应错误地把 SDK 当成硬依赖

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
- [ ] `Dockerfile`：依赖安装（改包后必须确认 start.sh 自检段同步）；`edge-tts` 通过 `EDGE_TTS_VERSION` 精确固定到本次查询的版本
- [ ] `scripts/update_edge_tts_version.py`：每次构建前查询阿里云 PyPI 镜像的最新稳定版并更新本地 `.env`；查询失败必须 fail-fast，禁止静默沿用旧版本
- [ ] `scripts/build-image.sh`：命令行构建入口，先更新版本再构建
- [ ] `docker-compose.yml`：将 `.env` 中的 `EDGE_TTS_VERSION` 作为 build arg；`.env` 必须加入 `.gitignore`
- [ ] 缓存：`EDGE_TTS_VERSION` 不变时复用依赖安装层；版本变化时仅使依赖安装层失效；BuildKit pip cache 用于减少重复下载
- [ ] 飞牛 OS 图形界面：每次点“重建/部署”前先在项目目录运行 `python3 scripts/update_edge_tts_version.py`，图形界面不会自动执行该查询脚本
- [ ] `start.sh`：环境自检 / 配置文件校验 / BUILD_TIME 读取优先级
- [ ] 测试：`bash -n start.sh`、`bash -n scripts/build-image.sh`、`python3 tests/test_build_image.py`
- [ ] README：依赖更新与 GUI 构建步骤同步
- ⚠️ 易漏：改 Dockerfile 装包后忘记同步版本标记——现已由 `/opt/build_time` 自动生成解决；改 compose 后忘记更新 `image:` 标签

### I. 改文档（README）
- [ ] 自查引用一致性：`grep -rn "config.example\|BUILD_TIME\|native_mic_lead\|0.5s\|1.1s" README.md` 确保无过期数字/死引用
- [ ] 配置说明表与代码默认值逐行核对
- ⚠️ 易漏：README 被三处引用（部署文档/配置表/功能段），改一处忘另一处（历史实例：配置表改 1.0 但"直连时序"段还写 0.5）

---

## 4. 历史易漏点固化清单（每次发布前过一遍）

- [ ] README 配置说明表 / 功能段 / jsonc 示例，与代码默认值**全部一致**（数字、单位、语义）
- [ ] `config.example.json` 是**最小可用版**（不含已精简的 timing/watchdog/logging 等段）；若代码新增必填项，确认已补进示例
- [ ] 服务器 `/app/config.json` 无显式旧值覆盖新默认（尤其 timing.native_mic_lead 曾为 0.5）
- [ ] BUILD_TIME 已更新（zip 包内带新时间戳文件；重建镜像则 Dockerfile 自动生成）
- [ ] `.gitignore` 含 `.logs/`、`BUILD_TIME`、`config.json`（防运行时文件误提交）
- [ ] main 定格线检查（按 §1）：本次改动是否点名驱动？是 → 确认**未** push main；播报核心修复 → 已按 §1.3 处理（混合 commit 已拆分、冲突已试应用评估）且**单向复核通过**（`git ls-tree main` 无 net_control/capture，`announce.py` 点名引用为 0）
- [ ] **混合 commit 拆分检查**：本次涉及同步的 commit 若同时含点名改动（如 announce+net_control 同 commit），已确认**未整体 cherry-pick**，播报部分为逐文件摘取（命令序列见 §1.3-3）
- [ ] zip 内不含 `.git/`、`.logs/`、`__pycache__/` 等运行时目录

---

## 5. 测试矩阵

| 套件 | 命令 | 覆盖范围 | 何时必须跑 |
|---|---|---|---|
| test_announce | `python3 tests/test_announce.py` | 文案/时间纯函数、TTS 缓存/合成/蓄水池、调度入队、native 链路与预热六分支、announce_task、DA 纯函数、WebhookHandler、config.example 合法性 | 任何代码/配置/README 改动（feat 全量） |
| test_watchdog | `python3 tests/test_watchdog.py` | 看门狗 `_watchdog_decision` 纯函数全分支、冷却、恢复通知 | 看门狗/点名/调度相关改动 |
| run_tests | `timeout 110 python3 tests/run_tests.py` | 集成级：调度、播报、net_control 场景、配置默认值回退 | 任何改动（全量回归） |
| test_session | `timeout 60 python3 tests/test_session.py` | 点名会话状态机流程 | 点名（net_control）改动 |
| py_compile | `python3 -m py_compile <改动的py>` | 语法 | 每次 |

最近一次 GitHub Actions 实测全绿：**feat 404 项**（`test_announce` 114 + `test_watchdog` 56 + `run_tests` 222 + `test_session` 12），另通过 Python 语法编译与 `bash -n start.sh`。每次新增用例后须重新运行并更新该数字。**main 为纯播报定格线（7d3432b），该 commit 不含
tests/ 目录、无独立测试套件**——main 同步播报修复时按 §1.3-6 将对应测试一并带入（剔除点名用例），
以带入的播报用例集为准。新增用例后 feat 总数应同步上升。

---

## 6. 交付后服务器侧验证命令

```bash
docker logs announce --since "30s" | grep -E "BUILD_TIME|config.example|Scheduler started|自动播报服务启动成功"
docker logs announce --since "30s" | grep "BUILD_TIME: /app="   # 三来源：/app=发布时间 | /opt=镜像构建 | env=旧镜像
docker logs announce -f            # 盯到首个准点播报完成（XX:00 / XX:30）
grep -c native_mic_lead /app/config.json   # 应输出 0（旧显式值已删）或 1.0
```
