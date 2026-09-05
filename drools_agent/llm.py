"""
llm.py — LLM 业务叙述生成器

做什么：
  接收 parser + checker 的结构化输出（JSON），调 DeepSeek API 生成
  一段人类可读的业务总结。

为什么要有这一层：
  parser + checker 只产出精确数字和表格（callTimes 分支、模板编号、
  延迟公式），但业务方看不懂这些。LLM 读结构化数据，用自然语言
  描述"这个策略在做什么、每一步目的是什么、有什么问题"。

架构角色：
  parser/checker → 确定性工具（数字 100% 准确）
  llm.py         → LLM 推理（叙述，不碰原始代码）
  report.py      → 最终排版（LLM 叙述 + 数据表格）
"""
import os
import sys
import json
import httpx
from pathlib import Path

# ---------------------------------------------------------------------------
# API 配置
# ---------------------------------------------------------------------------

API_URL = "https://api.deepseek.com/v1/chat/completions" # https://api.deepseek.com
MODEL = "deepseek-chat" # deepseek-v4-pro

def _load_api_key() -> str:
    """从 Streamlit secrets / .env 文件 / 环境变量 读取 API Key"""
    # 优先读 Streamlit secrets（Cloud 部署）
    try:
        import streamlit as st
        key = st.secrets.get("DEEPSEEK_API_KEY", "")
        if key:
            return key
    except Exception:
        pass

    # 其次读 .env 文件（本地开发）
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    
    # 降级到环境变量
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(
            "未找到 DeepSeek API Key。\n"
            "请在 drools_agent/.env 中设置 DEEPSEEK_API_KEY=sk-xxx\n"
            "或设置环境变量 DEEPSEEK_API_KEY"
        )
    return key

# ---------------------------------------------------------------------------
# Prompt 工程
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位资深金融营销策略分析师，熟悉墨西哥市场的 SMS、WABA、Push、邮件、电销和发券触达业务。

你的任务是根据 Drools 策略文件的结构化分析数据，写一份清晰、专业的业务总结报告。

报告结构（严格按此顺序）：

1. **策略概述**（2-3 句）
   - 这个策略在做什么？目标用户是谁？通过什么渠道触达？
   - 例如："本策略针对'给额未发标'用户（拿到授信额度但尚未申请借款），通过 5 轮电销跟进推动用户完成首次借款。"

2. **分流设计**（如有灰度桶）
   - 实验怎么分的？每组占比多少？目的是什么？
   - 例如："用户被随机分为 A/B/C 三组：A 组先电销后邮件（50%），B 组仅邮件（40%），C 组对照不打扰（10%）。"

3. **触达时间线**（按轮次，用人话描述）
   - 不要写代码，用时间线叙述
   - 例如："第 1 轮：注册当天延迟 1 小时后发送邮件提醒"
   - 如果有对照组差异，分别说明

4. **问题与风险**（如有检查问题）
   - 用业务语言解释每个问题的实际影响
   - 例如："第 64 行出口缺少标签，会导致数据分析时无法归因——这个用户最终借款了，但我们不知道是哪一轮电销促成的"
   - 如果没有问题，写"经自动校验，未发现结构性问题"

要求：
- 全文中文，控制在 300-500 字
- 面向业务方（产品经理、运营），不要出现代码变量名
- 如果数据不足以判断某些内容，明确说"数据未体现"，不要编造
- 不要重复罗列原始数据（表格部分会单独呈现）
"""

# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------

def generate_narrative(json_data: str, issues_summary: str) -> str:
    """
    调 DeepSeek API 生成业务叙述。
    
    Args:
        json_data:      parser 输出的 JSON（不含 raw_body，已脱敏）
        issues_summary:  checker 输出的问题摘要文本
    
    Returns:
        LLM 生成的业务叙述 Markdown
    """
    api_key = _load_api_key()
    
    user_prompt = f"""请根据以下 Drools 策略的结构化分析数据，撰写业务总结报告。

## 策略结构化数据
```json
{json_data}
```

## 自动校验结果
{issues_summary if issues_summary.strip() else "未发现任何问题（0 ERROR, 0 WARN, 0 INFO）"}
"""
    
    try:
        resp = httpx.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                "temperature": 0.3,   # 低温度减少幻觉
                "max_tokens": 1500,   # 足够 500 字中文
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        body = e.response.text[:200]
        if status == 401:
            return f"❌ **LLM 叙述生成失败**：API Key 无效或已过期，请检查 .env 文件"
        elif status == 429:
            return f"❌ **LLM 叙述生成失败**：API 请求频率超限，请稍后重试"
        elif status == 500:
            return f"❌ **LLM 叙述生成失败**：DeepSeek 服务端错误（500），请稍后重试"
        else:
            return f"❌ **LLM 叙述生成失败**：HTTP {status} — {body}"
    
    except httpx.ConnectError:
        return "❌ **LLM 叙述生成失败**：无法连接 DeepSeek API，请检查网络"
    
    except httpx.TimeoutException:
        return "❌ **LLM 叙述生成失败**：请求超时（60 秒），请稍后重试"
    
    except Exception as e:
        return f"❌ **LLM 叙述生成失败**：{type(e).__name__} — {e}"


# ---------------------------------------------------------------------------
# Agent 用：支持 function calling 的聊天接口
# ---------------------------------------------------------------------------

def chat_with_tools(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """
    调 DeepSeek API（带 function calling 支持）。
    
    与 generate_narrative 的区别：
      - 接收完整的 messages 列表（支持多轮对话）
      - 接收 tools schema（支持工具调用）
      - 返回原始 message dict（可能含 tool_calls 或 content）
    
    Returns:
        API 返回的 message dict，格式：
        {"role": "assistant", "content": "...", "tool_calls": [...]}
    
    Raises:
        RuntimeError: API 调用失败
    """
    api_key = _load_api_key()
    
    payload: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    if tools:
        payload["tools"] = tools
    
    try:
        resp = httpx.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,  # Agent 生成长代码可能需要更久
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]
    
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 401:
            raise RuntimeError("API Key 无效或已过期，请检查 .env 文件")
        elif status == 429:
            raise RuntimeError("API 请求频率超限，请稍后重试")
        else:
            raise RuntimeError(f"HTTP {status}: {e.response.text[:200]}")
    
    except httpx.ConnectError:
        raise RuntimeError("无法连接 DeepSeek API，请检查网络")
    
    except httpx.TimeoutException:
        raise RuntimeError("请求超时（120秒），请稍后重试")
