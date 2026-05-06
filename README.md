# 出海 B2B Pipeline

把 `01_keywords.md` 串到 `02_buyers.xlsx`、`03_xiaoman.xlsx` 和 `03_xiaoman_summary.md` 的半自动出海买家发现流水线。

## 工作流

```text
step1 关键词生成         手工 / Claude web，不在本仓
   ↓
step2 Serper 搜索        本仓覆盖：serper_search.py + buyer_extract.py
   ↓
step3 Xiaoman 匹配+联系人 本仓覆盖：xiaoman_playwright.py + pipeline.py
   ↓
step4 官网核验+评级       本仓覆盖：website_verify.py + llm_judge.py
   ↓
step5 枸杞分析           已并入 step4 输出字段
```

本仓的责任边界是：

- 读 `runs/<run_name>/01_keywords.md`
- 跑 step2 生成 `02_buyers.xlsx`
- 跑 step3 生成 `03_xiaoman.xlsx`
- 跑 step4 生成 `04_verified.xlsx`
- 生成或重算 `03_xiaoman_summary.md`

## 环境要求

- macOS
- Python 3.9+
- 一个可用的 Serper API key
- 可登录的小满账号

## 安装

建议直接用项目自带脚本：

```bash
cd ~/Documents/chuhai_pipeline
bash setup.sh
cp .env.example .env
```

如果手动安装，等价命令如下：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

然后把 `.env` 里的 `SERPER_API_KEY` 填好。

如果要用 `send_outreach.py` 通过 Outlook SMTP 做邮件 dry-run，先在 Outlook 账号开启 2FA（account.microsoft.com → Security），生成 App Password（不是登录密码），然后在 `.env` 里填 `SMTP_HOST=smtp-mail.outlook.com`、`SMTP_PORT=587`、`SMTP_USER=<your outlook>`、`SMTP_PASS=<app password>`。默认 dry-run 命令：`python send_outreach.py runs/2026-04-30/05_profiles/ --test-recipient your-other@email.com`；如需清空历史发送状态后重跑，加 `--reset`；真发必须显式加 `--live --i-confirm-live-send`。

## 首次跑 step3：扫码登录说明

step3 不读用户名密码文件，而是直接用 Playwright 打开真实 Chromium。

首次执行 step3 时会弹出小满登录页，需要你手动扫码一次。登录成功后，浏览器 profile 会持久保存在：

```bash
~/.xiaoman_playwright_profile/
```

后续再次跑 step3 会复用这个 profile，通常不需要重复登录。若登录态失效，重新扫码即可；若要彻底清空登录态，再删除这个目录。

## 常用命令

激活环境：

```bash
source .venv/bin/activate
```

完整跑 step2 + step3（默认抓公司 + top-1 联系人）：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md
```

step3 只抓公司，不抓联系人：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md --skip-contacts
```

只跑 step2，不进小满：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md --skip-step3
```

限制 Serper 查询数，适合冒烟：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md --max-queries 30
```

增加 step3 翻页数：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md --xiaoman-max-pages 2
```

放慢 step3 节奏，降低 captcha 风险：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md --xiaoman-search-interval 8
```

跳过官网核验和 LLM 判断：

```bash
python pipeline.py runs/2026-04-14_goji/01_keywords.md --skip-step4
```

不重跑 step3，只重算 summary：

```bash
python pipeline.py --summary-only runs/test_smoke_rerun_2026-04-10
```

## 运行产物

每次运行建议落在单独的 `runs/<run_name>/` 目录下，关键产物如下：

- `01_keywords.md`
  step1 产出的关键词输入文件，本仓只消费，不负责生成。
- `02_buyers.xlsx`
  step2 产出的买家候选列表。step3 读取这里的 `Company Name`、`Country`、`Lead Type`。
- `02_serper_raw.json`
  Serper 原始返回，主要用于审计和排查抽取问题。
- `03_xiaoman.xlsx`
  step3 产出的公司匹配结果；top-1 若抓到多个联系人，会展开成多行，重复公司字段并填联系人列。
- `03_xiaoman_summary.md`
  对 `02_buyers.xlsx` + `03_xiaoman.xlsx` 的摘要汇总，适合 demo 或快速复盘。
- `03_xiaoman_progress.json`
  step3 断点续跑状态。记录每个 buyer 的 `completed`、`no_matches`、`interrupted` 等状态。
- `04_verified.xlsx`
  step4 官网核验结果，只保留每个 buyer 的 top-1 公司，包含 B2B/B2C、目标客户、竞争对手、枸杞、评级和外联角度。
- `pipeline.log`
  本次运行的日志。

## 断点续跑

`pipeline.py` 会按文件和进度状态决定是否跳过步骤：

- `02_buyers.xlsx` 已存在：跳过 step2
- `03_xiaoman.xlsx` 已存在：读取已有行和 `03_xiaoman_progress.json`，自动跳过已完成 buyer，只继续未完成 buyer
- `04_verified.xlsx` 已存在：跳过 step4

这意味着：

- 要重跑 step2，删掉 `02_buyers.xlsx`
- 要完整重跑 step3，删掉 `03_xiaoman.xlsx` 和 `03_xiaoman_progress.json`
- 要从中断处继续 step3，直接重跑同一个 `01_keywords.md`；例如第 21 个 buyer 触发验证码，下次会从第 21 个未完成 buyer 自动继续
- 要重跑 step4，删掉 `04_verified.xlsx`
- 只想更新汇总，不要重跑 step3，用 `--summary-only`

## 已知限制

- 小满会触发 rate-limit / captcha；脚本不会绕过验证码，出现后需要人工在弹出的 Chromium 里处理。
- step3 的 top-1 不等于 100% 正确命中，尤其在名字短、歧义大或跨国实体场景下，仍需要人工复核。
- `03_xiaoman_summary.md` 里的国家类指标是汇总信号，不是最终 truth。
- 联系人抓取只覆盖 top-1 公司；rank 2、3... 仍然只有公司信息，没有联系人正文。
- step4 依赖官网可访问性和 LLM 判断，结果适合作为优先级信号，不应替代人工最终复核。
- 现在的冒烟规模适合 demo 和验证链路，不等于生产跑量配置；生产前要单独评估 query cap、sleep、captcha 频率和复核成本。
