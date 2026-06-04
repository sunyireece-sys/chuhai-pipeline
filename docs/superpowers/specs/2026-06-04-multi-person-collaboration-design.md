# 多人协作设计 — Chuhai Pipeline / Redvia

日期：2026-06-04
状态：Spec draft（brainstorming → spec），待用户审核进入 writing-plans 阶段

## 1. 目标与边界

把目前**单人 + Codex/Claude 自动化**的开发模式，扩展到能容纳：

- **intern**：补功能、修 bug、写测试，但不动核心爬虫/LLM/发件路径
- **资深技术同事**：偶尔 review、能动核心
- **sales（运营）**：通过 webui 提反馈，不直接进 GitHub
- **coding agent（Codex / Claude）**：默认协作者，跑大头任务

**核心约束（不变量）**：

- `xiaoman_playwright.py` 的执行只能在用户 Mac 上（持久 Chromium profile + 微信扫码登录态在 `~/.xiaoman_playwright_profile/`）。这条不能进任何容器、CI、远端机
- 当前 webui / cloudflare worker / redvia-site 三块都已稳定，本次设计**不重写它们**，只是给它们加协作脚手架
- 阶段性协作模式：**现阶段（pipeline + webui + redvia 旧项目稳定期）用户独占核心 review；未来新项目（Apollo/Clay 替换 xiaoman 后的新链路）改为分布式 ownership**。这条会写进 CONTRIBUTING.md 作为阶段声明

## 2. 整体方案 — 方案 B'（本地 pipeline + 云端协作）

```
┌──────────────────────────────────────────┐
│ 用户 Mac（不变）                            │
│  pipeline.py 全链路、xiaoman step3 登录态  │
│  CLAUDE.md / 工作日志/ / 会议/ 等本地资产    │
└──────────────────────┬───────────────────┘
                       │ make push-runs
                       ▼
┌──────────────────────────────────────────┐
│ 阿里云 ECS（webui + vaultwarden）          │
│  docker compose 起 webui + Vaultwarden    │
│  SQLite 不切 Postgres                     │
│  手动 deploy.sh，main 合并后用户 ssh 触发   │
└──────────────────────┬───────────────────┘
                       │ 1-min Cloudflare cron sync
                       ▼
┌──────────────────────────────────────────┐
│ Cloudflare（不变）                         │
│  Worker + D1 + redvia-site 静态资产        │
└──────────────────────────────────────────┘

GitHub（协作中枢）
  · PR + CI + CODEOWNERS
  · Issues + Projects 任务流
  · Devcontainer 让 intern/agent 入场
```

## 3. 仓库结构与文档

### 3.1 顶层文档调整

| 文件 | 动作 | 说明 |
|---|---|---|
| `README.md` | 保留 | 双语作品集风格，已重写 |
| `ARCHITECTURE.md` | 保留 | 系统地图 |
| `CONTRIBUTING.md` | **新增** | PR 流程、reviewer 约定、阶段性协作模式声明、key 怎么拿 |
| `AGENTS.md` | **新增** | Claude/Codex 入场约定（从前删的 agent.md 重写为公共版） |
| `CLAUDE.md` | 不变 | 仍在 `.gitignore`，本地工具用 |
| `docs/architecture/0001-*.md` | **新增** | ADR 目录 |

### 3.2 ADR 首批回写 4 个

| 编号 | 决策 |
|---|---|
| 0001 | Xiaoman step3 路径选 Playwright 持久 profile，不走 AppleScript / 不走 Node 旧爬虫 |
| 0002 | 追踪埋点用 Cloudflare Worker + D1，不上阿里云独立服务 |
| 0003 | WebUI 技术栈 FastAPI + Jinja + HTMX + SQLite（不上 React / 不上 Postgres） |
| 0004 | LLM provider 抽象集中在 `llm_judge.py`，pipeline 主链路不直调 LLM API |

### 3.3 reece 简短版日志机制废除

- 旧机制：`工作日志/` 写双份，详细自用 + 短版 `reeceYYYY-MM-DD.md` 给"上司 reece"看
- 修正：reece = 用户本人，没有上司视角；详细版已够用
- 落地：CLAUDE.md "工作日志写双份"那段清理；旧的 `reece*.md` 保留作历史不删除；不再新增

## 4. Secret 公司化迁移 + 1Password 替代方案

### 4.1 迁移 4 阶段

| 阶段 | 范围 | 状态 |
|---|---|---|
| A | 个人 → 公司 GLM / Serper key | **已启动**（公司已有 LLM 中转站 API） |
| B | webui fly.io → 阿里云 ECS（独立 workstream） | 未启动 |
| C | Cloudflare 个人账号 → 公司账号 | 未启动 |
| D | Vaultwarden 建立 + 共享分发 | 阶段 B 完成后 |

### 4.2 Vault 选型：Vaultwarden 自部署

`Vaultwarden` 是 Bitwarden 协议兼容的轻量服务端实现（Rust 写的），跟 webui 一起部署在阿里云 ECS 上，docker compose 起。

```yaml
# 阶段 B 的 docker-compose.yml 片段
services:
  webui:
    image: ...
    ports: ["8080:8000"]
  vaultwarden:
    image: vaultwarden/server:latest
    volumes: ["./vw-data:/data"]
    ports: ["8443:80"]
    environment:
      SIGNUPS_ALLOWED: "false"  # 只能管理员邀请
```

理由：

- 0 美元，对人民币入账场景友好（1Password Business 需 visa 国际信用卡）
- Bitwarden 客户端（macOS / iOS / Android / browser plugin / `bw` CLI）开箱即用
- 跟 webui 共用 ECS 节省一份资源

### 4.3 分发流程（阶段 D 后）

```
新成员入场 → 用户加他到 Vaultwarden（邀请邮件）
           → git clone + cp .env.example .env
           → bw login + bw unlock
           → 用脚本（scripts/bootstrap-env.sh）从 vault 拉值写入 .env
           → python pipeline.py --dry-run 验证
```

过渡期（阶段 D 之前）：CONTRIBUTING.md 写明"找用户单独要 key"，不走 GitHub Issue 不走 Slack 文本贴密钥。

## 5. Devcontainer（一个大容器装下所有）

### 5.1 文件结构

```
.devcontainer/
  devcontainer.json          # VSCode / Cursor / Claude Code 入口
  Dockerfile                 # base = python:3.11-slim + node 20
  docker-compose.yml         # python 容器 + vaultwarden client + sqlite volume
  postCreate.sh              # 启动后自动跑
```

### 5.2 容器内能跑 / 不能跑

| 能跑 | 不能跑 |
|---|---|
| `pipeline.py --skip-step3` 全链路演练 | step3 xiaoman（无 GUI、无登录态） |
| step4/5 LLM 判断 | — |
| webui FastAPI + pytest | — |
| `npx wrangler dev` Worker 本地预览 | — |
| 任何 Python/Node 改动 | — |

### 5.3 postCreate.sh 关键 4 步

```bash
pip install -r requirements.txt
playwright install chromium       # 容器内 Playwright 给 website_verify 用
bw login                          # 提示用户 Bitwarden 登录公司 vault
bash scripts/bootstrap-env.sh     # 从 vault 拉 secret 写本地 .env
```

### 5.4 选型理由

- Devcontainer 而不是裸 docker-compose → IDE 原生 "Reopen in Container"，intern 装好插件就能进
- compose 里只加 `bw` CLI，不跑 server → server 在 ECS 独立部署
- sqlite volume → webui dev 时保留 feedback.db，跨 rebuild 不丢

## 6. webui fly.io → 阿里云 ECS 迁移（B' 阶段 #4）

详细可逆方案在 `工作日志/2026-05-28_fly_webui_备份与下线计划.md`。多人协作设计中需要加的部分：

### 6.1 ECS 上 webui 部署：docker compose

ECS 跑 `docker compose up -d`，镜像跟 Devcontainer 用同一份 Dockerfile（区别只在 ENV：dev vs prod）。

理由：本地发现的问题 = ECS 能复现；intern review 时本地起容器就能验证修改在 prod 容器里也对。

### 6.2 数据库：继续 SQLite

不切 Postgres。当前并发 = sales 几人 + Cloudflare cron 1 分钟一次写 send_tracking / pixel_event，远未到 SQLite 单写者上限。

CONTRIBUTING 里预埋一句："DAO 层抽象稳定后，未来如出现真实并发问题再切 PG + 上 Alembic"。

### 6.3 部署触发：手动 deploy.sh

PR 合并到 main 后，用户 ssh ECS 跑 `deploy.sh`：

```bash
# deploy.sh 大致内容
cd /srv/redvia
git pull origin main
docker compose build webui
docker compose up -d webui
docker compose exec webui pytest -q   # smoke
```

不上 GitHub Actions 自动 deploy 的理由：要在 Actions secrets 里放 ECS 私钥，后期暴露面太大；intern 起步阶段手动够用。

## 7. CI 基线 + CODEOWNERS

### 7.1 GitHub Actions 最小集

`.github/workflows/ci.yml`，每 PR 触发，跑 3 个 step：

| Step | 工具 | 范围 |
|---|---|---|
| 1 | `ruff check .` | Python lint，全仓 |
| 2 | `pytest webui/` | webui 单元测试（in-memory SQLite） |
| 3 | `cd cloudflare && npx wrangler deploy --dry-run` | Worker bundle 检查：esbuild 跑 .mjs 语法 + 解决 imports，发现语法错就失败（不真 deploy） |

**不上**：

- mypy（现代码没 type hint，先补 hint 再开 mypy）
- step3 xiaoman 测试（CI 无 GUI、无登录态）
- 自动 deploy（按 §6.3）

### 7.2 CODEOWNERS

`.github/CODEOWNERS`：

```
pipeline.py            @sunyireece-sys
xiaoman_playwright.py  @sunyireece-sys
llm_judge.py           @sunyireece-sys
send_outreach.py       @sunyireece-sys
cloudflare/            @sunyireece-sys
.env.example           @sunyireece-sys
.github/workflows/     @sunyireece-sys
```

其他文件（webui/ / redvia-site/ / docs/ / requirements.txt / 工具脚本）任何 reviewer 可批。

CONTRIBUTING.md 里要写明这是"当前阶段（旧项目稳定期）的协作模式"，未来新项目可改。

## 8. GitHub Issues + Projects + sales 反馈通路

### 8.1 Issue 模板（3 个）

`.github/ISSUE_TEMPLATE/`：

| 文件 | 用途 |
|---|---|
| `bug.md` | sales / intern 报 bug：哪页 / 什么操作 / 期望 vs 实际 |
| `feature.md` | 新功能请求：用例 / 验收标准 / 优先级 |
| `agent-task.md` | 给 Codex/Claude 的任务单：上下文 / 改哪些文件 / 验收命令 |

### 8.2 Projects 看板

5 列：`Backlog → Ready → In progress → In review → Done`

3 维 label：

| 维度 | 取值 |
|---|---|
| area | `area:pipeline` / `area:webui` / `area:redvia` / `area:cloudflare` / `area:docs` |
| owner | `owner:user` / `owner:intern` / `owner:agent` / `owner:sales` |
| priority | `priority:p0` / `priority:p1` / `priority:p2` |

### 8.3 Sales 反馈 → GitHub issue 同步（B' 阶段 #7）

```
webui 反馈表单提交 → 写 feedback 表
                  → 触发 sync_feedback_to_github.py（cron 每 10min）
                  → POST /repos/.../issues
                  → 自动加 label: area:webui + owner:user + priority:p1
                  → 回写 feedback.github_issue_url
```

何时做：**webui 迁 ECS 之后**（B' 阶段 #4 完成）。fly 阶段不上，避免中间表又迁。

理由：sales 没 GitHub 账号、嫌技术工具门槛高、习惯在 webui 里就地反馈。同步脚本把入口收口在 webui，技术侧统一在 GitHub 看。

## 9. B' 工程拆解（落 issue 的源头）

8 个 issue，按依赖顺序：

```
#1 Secret 公司化阶段 A：GLM/Serper 公司 key 替换         ── 已部分启动
#2 Devcontainer + compose（一个大容器）                   ── AI agent + intern 入场前置
#3 顶层文档：CONTRIBUTING.md / AGENTS.md / ADR 0001-0004  ── 跟 #2 并行可
#4 webui fly.io → 阿里云 ECS（独立 workstream，B 阶段）   ── 阻塞 #5 #7
#5 Vaultwarden + 1Password 替代 vault（D 阶段）           ── 依赖 #4
#6 CODEOWNERS + CI 基线（ruff / pytest webui / wrangler） ── 跟 #2 并行可
#7 GitHub Issues 模板 + Projects 看板                     ── 跟 #6 并行可
#8 sales 反馈 → GitHub issue 同步                         ── 依赖 #4 + #7
```

并行波次：

- 波 1（立刻可起）：#1 / #2 / #3 / #6 / #7
- 波 2（#4 完成后）：#5 / #8

## 10. 不做的事（YAGNI 清单）

- 不上 mypy（type hint 没补全时强上等于堵 PR）
- 不上 Postgres（提前优化）
- 不上 K8s（小团队过重）
- 不上 GitHub Actions 自动 deploy（私钥暴露风险）
- 不写 SECURITY.md（仓库 public 后再加，现在加是 cargo cult）
- 不拆 webui / cloudflare / pipeline 三容器（Devcontainer 一个大容器够）
- 不立即切 1Password Business（Vaultwarden 自部署够用）

## 11. 验收（这份 spec 完成时的可观察信号）

- [ ] `docs/` 树下有这个文件
- [ ] 用户在 review pass 后批准
- [ ] 后续 writing-plans skill 接管，把 B' 8 issue 转成 implementation plan
- [ ] B' 8 issue 进入 GitHub Projects "Backlog" 列

## 12. 与现有 memory 的连接

- 解除 [[project_github_publish_progress]] 对此 spec 的阻塞（A 已完）
- 替换 [[project_multi_person_collab_brainstorm_progress]] 中"4 个开放问题待答"的状态
- 与 [[project_accounts_registry]] 互引：迁移阶段 A-D 的密钥同步细则照那份
- 与 [[project_webui_deployment]] 互引：webui 迁 ECS 用 docker compose 复用 Devcontainer image
- 与 [[project_llm_usage_distribution]] 互引：ADR 0004 的 LLM 抽象边界
