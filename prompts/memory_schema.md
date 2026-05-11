# finAgent Memory Schema

这份文档说明 `memory.json` 里存了什么、结构怎么规划的。LLM 在 prompt 里
知道大原则；具体的 entity 类型约定在这里，便于你（人类）**直接编辑 memory.json
来修正 agent 的理解**，以及**未来迁移数据时有据可查**。

---

## 核心设计

memory 是一张**语义知识图谱**：
- **Entity**：一个名词性的节点（公司、主题、人、你自己）
- **Observation**：挂在 entity 上的事实陈述（短句，单条独立成立）
- **Relation**：两个 entity 之间的有向关系

三类数据都以 JSONL 形式写在 `memory.json` 里，每行一条。

---

## Entity 类型清单

### 1. `UserProfile`（**单例**，name 固定为 `"me"`）

表示"主人"自己。**全程只有一个**，不要建第二个。

**Observation 分类约定**（每条 observation 在句首用中括号标前缀，便于过滤）：

- `[风险]` — 风险容忍度、杠杆偏好、单日/单标的回撤红线
- `[风格]` — 交易频率、时间视野、风格标签
- `[能力圈]` — 熟悉的行业 / 品种 / 策略
- `[no-go]` — 明确不碰的东西
- `[仓位]` — 当前持仓概览（标的 + 占比或量级），会随交易变化
- `[watchlist]` — 在看但没开仓的标的
- `[主题]` — 当前关注的宏观 / 行业叙事
- `[原则]` — 个人交易原则、复盘得出的经验
- `[情绪信号]` — 什么情况下会意识到自己情绪化
- `[偏好]` — 跟 agent 交互的偏好（回答风格、briefing 时点）
- `[账户]` — 账户规模量级、基础货币、券商等

**示例：**

```json
{"type":"entity","name":"me","entityType":"UserProfile","observations":[
  "[风险] 单日回撤容忍 3%，单标的最大仓位 15%",
  "[风险] 不使用 3 倍以上杠杆 ETF；期权仅限 covered call / cash-secured put",
  "[风格] 中线为主，持仓中位数 2-8 周；偶尔做事件驱动（财报、FOMC）",
  "[能力圈] 美股科技 / 半导体 / 云计算；美债曲线；DXY 与利差联动",
  "[no-go] 小盘 meme 股、中概受制裁名单、任何杠杆 ETF 过夜",
  "[watchlist] NVDA MSFT TLT DXY UUP",
  "[主题] Fed 2026 降息节奏；AI capex 回报可见性；中美脱钩边际",
  "[原则] 亏损单不加仓；涨得太快反而减仓；不碰自己说不清楚的东西",
  "[偏好] 回答要数字重、有确切来源；不喜欢空洞的'表现不错'这种形容"
]}
```

### 2. `Company`

单一上市公司 / 发行体。

**Observation 约定**：
- 尽量引用时间戳（"(2026-04-20 数据)"）
- 偏好**跨季度仍有参考价值**的事实，而非一次性数字（今日股价不存，Q2 cRPO 存）
- 常见类别：基本面、财报关键指标、分析师一致预期、叙事转折点、管理层变动

### 3. `MacroTheme`

宏观 / 行业叙事（如"Fed 降息节奏"、"AI capex 回报可见性"、"美元走弱"）。

**Observation 约定**：
- 每条 observation 代表**叙事的一次状态**，追加不覆盖
- 建议前缀：`[演进]` / `[数据锚]` / `[bull]` / `[bear]` / `[反证]` / `[日期 YYYY-MM-DD]`
- 相关 Company 用 relation 连接（"驱动" / "受益于" / "反指")

**示例：**

```json
{"type":"entity","name":"Fed_2026_rate_cuts","entityType":"MacroTheme","observations":[
  "[演进] 2026-03 市场定价全年 3 次降息",
  "[演进] 2026-04-17 CPI 数据后降至 2 次预期",
  "[数据锚] 核心 CPI YoY 2.8% (BLS 2026-04-10)",
  "[bull] 就业市场降温、零售转弱暗示软着陆窗口",
  "[bear] 原油反弹 + 关税传导导致 headline 通胀粘性",
  "[反证] TIPS breakeven 5Y 回升至 2.4%"
]}
```

### 4. `Position`（暂未实装，Phase 11 再启）

当前持仓的独立 entity（每个标的一个）。Phase 8 先在 UserProfile 的 `[仓位]`
observation 里粗粒度记一下即可。

### 5. `Trade` / `Thesis`（暂未实装，Phase 11）

每一笔开 / 平仓 + 对应的论点。届时会设计关系图。

### 6. 其他可选

- `Person`（基金经理 / 央行官员 / 分析师）
- `Sector`（行业板块）
- `Policy`（政策事件，如"Fed 议息 2026-05-07"）
- `MacroIndicator`（具体数据系列，比 MacroTheme 更原子，如 CPIAUCSL / T10Y2Y）

---

## Relation 约定

- `from` 和 `to` 是 entity name
- `relationType` 用**主动态短语**，能组成"主语 + 谓语 + 宾语"读得通

**常见关系：**

- `驱动` / `被驱动`
- `受益于` / `受损于`
- `关注` (UserProfile → Company / MacroTheme)
- `持有` (UserProfile → Company，Phase 11 改成 → Position)
- `隶属于` (Company → Sector)
- `对立于` / `替代于`

---

## LLM 对本文档的使用

- 本文档**不直接塞进 system prompt**，token 太贵
- system prompt 里只有核心原则 + UserProfile 的分类前缀
- Agent 需要细节时可以 filesystem.read_file("../prompts/memory_schema.md")
  （⚠️ 目前 filesystem MCP 白名单只有 reports/，够不着 prompts/；Phase 8 暂不靠
  agent 自读，主要靠人类维护和 prompt 里的精要表达）

---

## 人类维护指南

- `memory.json` 是 JSONL，每行一条 JSON，UTF-8
- 想直接编辑 UserProfile？更简单的路径：**编辑 `reports/_profile/me.md`**，然后
  下次启动让 agent 做一次"memory 与 me.md 对账"
- 定期备份 `memory.json`——里面有你的所有画像和历史观点
