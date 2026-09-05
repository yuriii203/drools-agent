# Drools 策略 Agent

基于 LLM + Tool-Calling 的墨西哥营销触达 Drools 策略代码自动生成与审查 Agent。

> **脱敏说明**：本仓库为脱敏演示项目，技能文档（`.qoder/skills/drools-strategy/`）与代码中的平台名、灰度桶名、批次号、字段名等均已替换为占位符，不代表任何真实生产环境配置。若要在自己的业务中使用本 Agent：将 SKILL.md / reference.md 替换为你自己业务的函数签名、分流泳道与字段约定即可（文档结构可照搬），整套“生成 → 校验 → 流程图 → 交叉校验”工作流即可复用。

> **在线体验**: https://drools-agent-6gnjqj54sbm9fzvedxhmjb.streamlit.app

## 功能概览

| 模式 | 说明 |
|------|------|
| ✏️ **生成模式** | 描述策略需求 → Agent 追问细节 → 生成 Drools 代码 → 自动校验 → 交付 `.java` 文件 |
| 📊 **分析模式** | 上传已有 `.drl`/`.java` → 解析规则元数据 → 校验问题 → LLM 业务叙述 + 交叉校验报告 |
| 🎨 **流程图** | 分析模式附带 HTML 策略流程图：多分组泳道 + 逐轮触达时间线（确定性计算，非 LLM 生成） |

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    Streamlit Web UI                       │
│                    (drools_agent/app.py)                  │
├─────────────────────────────────────────────────────────┤
│              Conversation Agent Loop                      │
│              (drools_agent/agent.py)                      │
│                                                           │
│   User Requirement ──→ LLM (DeepSeek) ──→ Tool Calls     │
│         ↑                                      │          │
│         └──── 追问/确认 ←──── Tool Results ←───┘          │
├─────────────────────────────────────────────────────────┤
│                     Tools Layer                           │
│              (drools_agent/tools.py)                      │
│                                                           │
│  read_skill    read_reference   run_checker              │
│  save_strategy read_generated_file                       │
├─────────────────────────────────────────────────────────┤
│              Parser + Checker Pipeline                    │
│   parser.py ──→ RuleMetadata ──→ checker.py ──→ Issues   │
├─────────────────────────────────────────────────────────┤
│           Flow + Cross-Validation Layer                  │
│   flow_html.py      确定性时间线渲染（HTML 流程图）        │
│   cross_validate.py LLM 交叉校验 + 结构化纠错闭环         │
├─────────────────────────────────────────────────────────┤
│                   Knowledge Base                          │
│   .qoder/skills/drools-strategy/SKILL.md                 │
│   .qoder/skills/drools-strategy/reference.md             │
└─────────────────────────────────────────────────────────┘
```

**核心设计理念**: Agent 不是被动接收全文 prompt，而是主动决定何时调哪个工具：
- `SKILL.md`（~7000字）作为规范预加载
- `reference.md` 按需读取（LLM 需要查参数时才调）
- 生成 → 校验 → 修复闭环（最多 3 次迭代）

## 分析模式：确定性 + LLM 互补

分析链路：`parser（正则，精确）→ checker（静态检查）→ flow_html（时间线渲染）→ cross_validate（LLM 交叉校验）`。

设计原则：**数字归确定性代码，语义归 LLM**。触达时间线是累计延迟求和的纯数学，由 flow_html 精确计算；LLM 只负责“读源码找 parser 盲区”和“兜底无法静态求值的动态延迟”，且只输出结构化修正，不直接改写报告文字/数字。

### 逐轮触达时间线

第 N 轮触达时刻 = 前 N-1 轮 delay 之和。动态延迟（delaySeconds / delayToNextHHMM / 变量）按以下优先级解析，全部失败才标“待确认”：

1. 纯算术求值（如 `30*60`）
2. `delayToNextHHMM` + 注释天偏移（如 delayToNext10 / delayToNext930 配合 “T+1:” / “第二天9:30”）
3. 已知绝对时间函数表（delayToNextDay8 等）
4. 注释绝对/相对时间（“T+1 8点” / “次日9:30” / “T0 16小时”）
5. 🤖 LLM 兜底（流程图紫色标签，与确定性琥珀色标签区分）

### 分组识别（两种语义）

| 语义 | 源码形态 | 流程图渲染 |
|---|---|---|
| 外层分组 | `"plus".equals(groupType)` 包裹整条 callTimes 链 | 独立泳道，各自时间线 |
| 分支内布尔分组 | `boolean is95Group = regScore<=0.125` + 分支内 `if(is95Group)` 选模板 | 单时间线 + 每轮模板变体徽章 |

### LLM 结构化纠错闭环

cross_validate 对比「源码 + parser 结果」，除自然语言 findings 外还返回结构化 `corrections`（语义出口路径归并、漏识的布尔分组），由 `apply_corrections()` **只增不覆**地合并回 RuleMetadata 后再渲染报告/流程图。交叉校验在报告渲染**之前**执行，因此纠错能直接落到报告里；时间线/模板等精确数据仍由 parser 提供，LLM 碰不到。

## 项目结构

```
├── drools_agent/           # 核心代码
│   ├── app.py              # Streamlit Web UI 入口
│   ├── agent.py            # Conversation Agent 循环
│   ├── llm.py              # DeepSeek API 封装（httpx）
│   ├── tools.py            # 5 个 Agent 工具实现
│   ├── parser.py           # Drools .java/.drl 规则解析器
│   ├── checker.py          # 规则校验器（11 类检查项）
│   ├── report.py           # 分析报告生成（Markdown + JSON）
│   ├── flow_html.py        # HTML 流程图（确定性时间线渲染）
│   ├── cross_validate.py   # LLM 交叉校验 + 结构化纠错闭环
│   └── main.py             # CLI 入口
├── .qoder/skills/drools-strategy/
│   ├── SKILL.md            # 策略开发规范（硬性规则 + 骨架模板）
│   └── reference.md        # ActionFunction 函数速查表
├── generated/              # Agent 输出的策略文件
├── requirements.txt        # Python 依赖
└── README.md
```

## 快速开始

### 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 API Key
# 创建 drools_agent/.env 文件:
echo "DEEPSEEK_API_KEY=sk-your-key" > drools_agent/.env

# 3. 启动 Web UI
cd drools_agent
python -m streamlit run app.py
# 访问 http://localhost:8501
```

### CLI 模式

```bash
cd drools_agent

# 分析已有策略（含 LLM 叙述 + 交叉校验）
python main.py path/to/your_strategy.java --llm

# 分析并额外输出 HTML 流程图
python main.py path/to/your_strategy.java --llm --with-flow

# 生成新策略
python main.py --write "3轮SMS提醒策略，categoryCode是Demo_SMS，模板TEMPLATE_001"
```

### Streamlit Cloud 部署

1. Fork 本仓库到你的 GitHub
2. 前往 [share.streamlit.io](https://share.streamlit.io/deploy)
3. 填写：
   - Repository: `你的用户名/drools-agent`
   - Branch: `main`
   - Main file: `drools_agent/app.py`
4. Advanced Settings → Secrets 添加：
   ```toml
   DEEPSEEK_API_KEY = "sk-your-key"
   ```
5. 点击 Deploy

## 技术栈

- **前端**: Streamlit (Python Web UI)
- **LLM**: DeepSeek API (`deepseek-chat` 模型)
- **HTTP**: httpx
- **校验**: 自研 Drools 规则解析器 + 11 类静态检查
- **部署**: Streamlit Community Cloud (免费)

## 校验器检查项

| # | 检查项 | 级别 |
|---|--------|------|
| 1 | categoryCode 必须匹配 rule 名 | ERROR |
| 2 | 每个 return 前必须 setDecisionTag | ERROR |
| 3 | ActionFunction 调用必须带类名前缀 | ERROR |
| 4 | callTimes 末轮必须有兜底分支 | ERROR |
| 5 | 全局 try-catch 防护 | WARN |
| 6 | 灰度三级泳道顺序 | WARN |
| 7 | 延迟参数类型必须为 int | WARN |
| ... | ... | ... |

## License

MIT
