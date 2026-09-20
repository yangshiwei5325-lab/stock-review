# A股每日复盘一键脚本

一条命令抓取全市场行情、资金、板块、消息面，自动生成一份带"下一交易日展望"的 Markdown 复盘报告。

```
stock-review/
├── 一键复盘.bat            ← Windows 双击运行
├── 一键复盘并推送.bat      ← Windows 双击运行（顺手推到飞书）
├── 每日复盘推送.bat        ← 定时任务调用的入口（生成 + 推送 + 记日志）
├── run.sh                  ← Linux / macOS
├── stock_review.py         ← 主程序（Python 2.7 / 3.x 均可）
├── feishu_push.py          ← 飞书推送模块（也可单独当命令行工具）
├── selftest.py             ← 离线自检（不联网，改代码后先跑它）
├── setup_task.ps1          ← 注册 Windows 定时任务
├── push_config.json        ← 飞书推送配置（需自己填，已被 .gitignore 排除）
├── push_config.example.json← 配置模板（入库的那份，凭据留空）
├── reports/                ← 生成的报告（.md + .summary.json）
└── .cache/<交易日>/        ← 原始数据缓存（可断点续跑，可随时删）
```

---

## 快速开始

**Windows**：双击 `一键复盘.bat`（跑完会自动打开报告目录）。

**首次使用**（配置推送凭据，`push_config.json` 不入库，需要自己建）：

```bash
cp push_config.example.json push_config.json   # 或者直接跑下面的 --set-webhook，会自动创建
```

**命令行**：

```bash
python stock_review.py                  # 全量生成
python stock_review.py --push           # 生成并推送到飞书
python stock_review.py --no-news        # 跳过新闻，更快
python stock_review.py --no-breadth     # 再跳过全市场宽度（最快，约 10 秒）
python stock_review.py --refresh        # 忽略缓存，强制重抓
python stock_review.py --live           # 强制实时（盘中默认已开启）
python stock_review.py --no-live        # 盘中也不吃缓存，走本地快照
python stock_review.py --date 20260911  # 离线复盘：只读本地缓存重算，不联网
python stock_review.py --out D:/x.md    # 指定输出文件
```

跑完控制台会打印一段"盘面速览"，报告默认写到 `reports/A股复盘_<交易日>.md`。

---

## 报告包含什么

| 章节 | 内容 |
|---|---|
| 一、大盘概览 | 11 个主要指数（含中证1000/国证2000/北证50）、两市成交额、与前一日量能对比 |
| 二、市场宽度与情绪 | 涨跌平家数、涨停/跌停/炸板/炸板率、涨跌幅分布直方图、中位数与均值 |
| 三、资金面 | 两市主力/超大单/大单/中单/小单净额、两融余额趋势、行业与概念资金净流入流出榜 |
| 四、板块结构 | 行业与概念板块领涨/领跌排行（含主力净额） |
| 五、个股榜单 | 涨幅榜、跌幅榜、成交额榜各 TOP15~20 |
| 六、市场消息 | 从 7×24 快讯中按关键词自动筛选的市场相关消息 |
| 七、外围市场 | 美股、亚太、欧洲、美元指数、BDI、原油/黄金/白银、人民币汇率 |
| 八、技术位置 | 各指数 MA5/10/20/60 与距 MA20 偏离度 |
| 九、下一交易日展望 | **规则化多空信号**（见下）+ 上证关键压力/支撑位 |
| 十、数据缺口 | 本次抓取失败的接口清单（有则列出） |

### 关于"下一交易日展望"

这一节**不是预测**，而是按固定阈值把当日盘面的多空矛盾自动列出来，避免主观遗漏。当前规则包括：

| 维度 | 阈值 | 输出 |
|---|---|---|
| 均线结构 | 收盘 vs MA5/10/20/60 | 空头排列 / 多头排列 / 中性 |
| 量能 | 沪市成交额环比 ±8% / ±12% | 缩量（利多）/ 放量下跌（利空）/ 放量上涨（利多） |
| 宽度背离 | 上涨家数占比 ≥50% 且指数收跌 | "指数跌但个股普涨，非全面避险" |
| 主力资金 | 净额正负 | 净流入 / 净流出 |
| 情绪 | 炸板率 ≥30% | 提示不宜追高连板 |
| 两融 | 近 3 日趋势 | 回升 / 回落（杠杆退潮） |
| 外围 | 韩国KOSPI ≤-2%、日经 ≤-1%、美股三指数齐涨 | 外围风险偏好提示 |

想按自己的习惯调，直接改 `stock_review.py` 里的 `build_signals()`，阈值都写在明面上。

---

## 飞书推送

跑完复盘后把摘要以**飞书卡片**推到群里或指定的人。推荐用**自定义机器人**——不用认证、2 分钟搞定、能在定时任务里无人值守跑。

### 最快上手（群自定义机器人）

1. 飞书里打开任意群 → 右上角 **设置** → **群机器人** → **添加机器人** → **自定义机器人**
2. 起个名字，复制 **Webhook 地址**
3. （可选）如果创建时勾了「签名校验」，把密钥也复制下来
4. 填进配置——不用手改 JSON，跑一条命令即可：

```bash
python feishu_push.py --set-webhook "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"
python feishu_push.py --show-config        # 确认写入（敏感字段自动打码）
```

如果创建机器人时勾了「签名校验」，把密钥也存进去：

```bash
python feishu_push.py --set-secret "你的密钥"
```

5. 先看效果（不发送）：

```bash
python feishu_push.py --test --dry-run
```

6. 满意了再真发：

```bash
python feishu_push.py --test          # 发一张测试卡片
python stock_review.py --push         # 复盘 + 推送
```

或者直接双击 `一键复盘并推送.bat`。

### 其它渠道

| provider | 推到哪 | 需要什么 |
|---|---|---|
| `feishu` | 飞书群（自定义机器人） | Webhook 地址，可选签名密钥 |
| `feishu_app` | 指定的人 / 群（自建应用） | `app_id` + `app_secret` + `receive_id` |
| `custom` | 任意 webhook | URL，body 里用 `{{title}}` / `{{content}}` |

`feishu_app` 走飞书开放平台：创建企业自建应用 → 开通 `im:message` 权限 → 由管理员发布到企业 → 把 `app_id`/`app_secret` 填进配置。`receive_id_type` 支持 `open_id` / `user_id` / `union_id` / `email` / `chat_id`，`receive_id` 填对应值（发给群就填群 `chat_id`）。私有化部署可用 `api_base` 覆盖域名。

也支持环境变量覆盖（适合服务器/CI）：`FEISHU_WEBHOOK`、`FEISHU_SECRET`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_PUSH_PROVIDER`。

### 卡片长什么样

标题栏按上证涨跌配色（**红涨绿跌**），正文分区块：

```
┌─ A股复盘 2026-09-14 ────────────────┐  ← 绿/红色标题栏
│ 上证 3885.33　-0.07%                │
│ 深证成指 -0.64% | 创业板指 -1.10%   │
│ 小盘：中证1000 +0.55% | 国证2000 …  │
├─────────────────────────────────────┤
│ 成交 16292亿　涨跌 3126↑ / 2224↓    │
│ 情绪 涨停55　跌停16　炸板率35%      │
│ 主力 净流出 165.3亿                 │
│ 两融 26316亿（2026-09-11）          │
├─────────────────────────────────────┤
│ 领涨 医疗研发外包+5.8% / 医疗服务…  │
│ 领跌 教育运营-7.9% / 其他种植业…    │
│ 热点 昨日连板+4.9%、CRO+4.6%…       │
├─────────────────────────────────────┤
│ 偏多信号   • 缩量 -18.7%，杀跌衰减  │
│ 偏空信号   • 均线空头排列…          │
├─────────────────────────────────────┤
│ 关键点位　压力 3920–3937　支撑 3867 │
└─────────────────────────────────────┘
```

实际卡片 JSON 约 1.8KB（飞书上限 30KB），超长会自动截断。

### 推送频率

**默认不限次数**——想推几次推几次，一天内多次运行都会正常发出（脚本会显示"当天第 N 次推送"）。

| 配置 | 默认 | 说明 |
|---|---|---|
| `once_per_day` | `false` | 设为 `true` 则同一交易日只推一次，重复运行自动跳过（`--force` 可强推） |

```bash
python feishu_push.py --set-once-per-day true    # 改成每天只推一次
python feishu_push.py --set-once-per-day false   # 改回不限次数
```

`only_if` 控制的是**什么条件下才推**（和不限次数不冲突）：

| 值 | 行为 |
|---|---|
| `always` | 每个交易日都推 |
| `pressure` | 偏空信号 ≥3 条才推（震荡市不打扰） |
| `bigmove` | 上证涨跌幅 ≥1% 才推（只在大波动日提醒） |

### 一天推多次会拿到最新数据吗？——会

脚本默认带**盘中实时模式**：

- **交易时段内（周一~周五 09:10–15:05）**：自动绕过缓存，每次抓的都是当时的最新快照。所以 10:30 推的是早盘、11:30 推的是午盘、15:05 推的是收盘，内容各不相同。
- **收盘后**：走缓存，秒级完成，且结果与收盘时一致。
- **盘中抓的数据不会写回缓存**，避免盘中快照覆盖掉"收盘定稿"的那份，导致晚上再跑反而读到旧数据。

手动控制：

```bash
python stock_review.py --push            # 默认行为（盘中自动实时）
python stock_review.py --push --live     # 强制实时（不吃缓存）
python stock_review.py --push --no-live  # 即使盘中也不吃缓存（想快速重发同一份内容时用）
```

> 注意：盘中实时模式要重新抓全市场数据（约 60 个请求），比走缓存慢；在网络较慢的环境下可能需要 1–2 分钟。

### 每个交易日自动跑（Windows 计划任务）

**一键注册**（推荐；本机已注册为任务「A股每日复盘推送」）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup_task.ps1
```

每周一~周五 **15:30** 自动运行 `每日复盘推送.bat`（生成报告 + 推送飞书），
输出追加到 `logs\daily_push.log`。卸载或改时间：

```powershell
Unregister-ScheduledTask -TaskName 'A股每日复盘推送' -Confirm:$false
Get-ScheduledTask  -TaskName 'A股每日复盘推送' | Get-ScheduledTaskInfo   # 看上次/下次运行
```

手工创建的话：任务计划程序 → 创建基本任务 → 触发器：每周一~周五 **15:30**
（收盘后）→ 操作 → 启动程序 → 程序填 `每日复盘推送.bat` 的完整路径，
「起始于」填 `F:\DeepSeek\stock-review`，并勾选「不管用户是否登录都要运行」。

> 因为 `push_config.json` 里 `once_per_day = true`，节假日即使触发也只会拿到
> 最近一个交易日的数据、且与已推送记录同日，会自动跳过，不会重复轰炸。

Linux/macOS 用 crontab：

```cron
30 15 * * 1-5 cd /path/to/stock-review && ./run.sh --push >> logs/daily_push.log 2>&1
```

### 单独使用推送工具

`feishu_push.py` 不依赖主脚本，可以对任意一份 `.summary.json` 操作：

```bash
python feishu_push.py                                  # 推最新一份摘要
python feishu_push.py --summary reports/xxx.summary.json
python feishu_push.py --summary reports/xxx.summary.json --dry-run
python feishu_push.py --text "自定义消息"
python feishu_push.py --test --dry-run                 # 自检，不发送
```

> 注：本机环境里的 `jira-feishu-collector` MCP 也能发飞书消息，但它是远程服务、需要二次确认，且当前身份没有配 `feishu_open_id`（私聊发不到自己），所以日推送走自定义机器人更合适。

---

## 数据来源

全部为公开行情接口，无需 API Key：

- **新浪财经** `hq.sinajs.cn` —— 指数与商品/汇率实时报价
- **东方财富** `push2*.eastmoney.com` —— 指数、板块、资金流、全市场个股、日K
- **东方财富** `push2ex.eastmoney.com` —— 涨停/跌停/炸板池
- **东方财富** `datacenter-web.eastmoney.com` —— 两融余额
- **东方财富** `np-listapi.eastmoney.com` —— 7×24 快讯

交易日取自行情接口返回的日期字段，**不依赖本机时钟**，所以周末或节假日运行会自动回溯到最近一个交易日。

---

## 几个实用设计

**磁盘缓存**：每个成功请求的原始 JSON 都会落到 `.cache/<交易日>/`。同一交易日重跑只要 15~20 秒；若某次抓取有缺页，**直接再跑一次就会自动补抓缺失部分**（已有缓存不会重复请求）。想让脚本永远抓最新数据，加 `--refresh`。

**多主机容错**：东财行情主机有多台备机，脚本会记住最近可用的那台；该主机失效时自动回退到全量探测。

**并发抓取**：板块、资金、日K、全市场分页、新闻各页，以及主力资金/两融/涨跌停池/外围/商品这 5 个互相独立的源，均并发执行，`--workers` 可调（默认 8）。

**部分失败不影响出报告**：任一接口挂掉只会在"数据缺口"里记一笔，其余章节照常生成。

---

## 自检（改代码后先跑这个）

```bash
python selftest.py
```

不联网、不到 1 秒，用构造数据跑一遍核心纯计算与渲染路径，覆盖：

- `analyze()` 的统计口径（涨跌家数、涨跌幅分布桶边界、中位数/均值、TOP-N 排序）
- `build_signals()` 多空信号与关键点位
- `render()` 报告渲染（章节齐全、个股三榜、新闻关键词过滤）
- `build_summary()` 推送摘要结构
- `parse_sina_lines()` 新浪行情文本解析
- `fetch_all_sources()` 并发取数的失败隔离
- `feishu_push` 卡片渲染、占位符替换（含制表符等控制字符）、错误码解析

改动渲染或统计逻辑后，除了跑自检，还可以做一次**逐字节回归**：拿同一份 `.cache/<交易日>/` 缓存，分别用改动前后两版脚本离线跑一遍，比对报告：

```bash
python stock_review.py --date 20260918 --out /tmp/new.md   # 改后
python old_stock_review.py --date 20260918 --cache-dir ./.cache/2026-09-18 --out /tmp/old.md  # 改前
diff /tmp/old.md /tmp/new.md
```

> `--date` 离线模式只读缓存、不联网、不写缓存，所以安全且可重复。

---

## 常见问题

**跑得很慢？** 正常情况下全量约 20~60 秒。如果每个请求都卡住数秒，通常是所在网络走了代理（可用 `--no-breadth` 先跑轻量版验证）。

**提示"停牌或无报价"？** 正常。全市场约 5900 只股票里有 300~400 只当日停牌，`f3` 字段为 `-`，脚本会单独统计，不参与涨跌家数。

**想加指数 / 板块？** 改 `INDICES` 列表（新浪代码 + 名称 + 东财 secid）。中证/国证系列若只想要基础涨跌，第三个字段留 `None` 即可跳过均线计算。

**报告里的数字和行情软件对不上？** 成交额等字段新浪与东财口径略有差异（分页快照时点不同），差异通常在 1% 以内。

---

## 免责声明

本脚本自动抓取公开行情数据生成报告，仅作数据整理与逻辑推演之用，**不构成任何投资建议**。市场有风险，决策需谨慎。
