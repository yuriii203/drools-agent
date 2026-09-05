"""
cross_validate.py — LLM 交叉校验层

做什么：
  parser 用正则提取结构化数据后，把「源码 + parser结果」一起发给 LLM，
  让 LLM 用自然语言理解能力对比，找出 parser 遗漏的内容。

为什么需要这一层：
  - parser（正则）精确但有盲区：遇到没见过的写法就会漏
  - LLM 理解语义但有数字误差：可能数错分支数
  - 交叉校验 = parser数字精确 + LLM不遗漏，两者互补

架构位置：
  parser.py → checker.py → report.py → cross_validate.py（补充发现）
                                              ↓
                                   追加到报告"交叉校验"段落
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from llm import _load_api_key, API_URL, MODEL
import httpx


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class CrossValidationFinding:
    """LLM 交叉校验发现的一条遗漏/补充"""
    category: str      # "missed_group" / "missed_branch" / "logic_gap" / "info"
    description: str   # 自然语言描述
    severity: str = "INFO"  # WARN = parser漏了重要内容, INFO = 补充说明


@dataclass
class CrossValidationResult:
    """交叉校验的完整结果"""
    findings: list[CrossValidationFinding] = field(default_factory=list)
    corrections: dict = field(default_factory=dict)  # 结构化纠错 {rule_name: {exit_paths, group_variants}}
    llm_summary: str = ""  # LLM 的一句话总结（如"parser 正确识别了所有分组"）
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

CROSS_VALIDATE_SYSTEM = """你是一个代码审查专家，专门审查 Drools 策略规则的自动化解析结果。

你的任务是：对比「源码」和「parser 提取的结构化数据」，找出 parser 遗漏或不准确的地方。

检查要点：
1. **分组遗漏**：源码中有几个策略分组（如 regular/plus/vip）？parser 是否全部识别？
2. **轮次遗漏**：每个分组有多少轮 callTimes？parser 的计数是否正确？
3. **逻辑遗漏**：是否有 parser 未捕捉到的特殊逻辑（如条件嵌套、动态延迟计算、时间窗口判断）？
4. **灰度分流**：源码中有几层灰度？parser 是否全部识别？
5. **出口路径**：是否有 return/退出路径被 parser 遗漏？

输出格式（严格 JSON）：
```json
{
  "summary": "一句话总结 parser 的表现",
  "findings": [
    {
      "category": "missed_group|missed_branch|logic_gap|info",
      "description": "具体描述（中文）",
      "severity": "WARN|INFO"
    }
  ],
  "corrections": {
    "<rule_name>": {
      "exit_paths": ["语义出口路径描述（如：callTimes==1 手机号为空退出）", ...],
      "group_variants": {"<flag名>": "<分组判定表达式>"}
    }
  }
}
```

corrections 字段说明（**用于自动修正报告，务必结构化、可机器合并**）：
- exit_paths：仅当 parser 的 exit_count（原始 return 计数）与“语义出口路径数”不符时提供，
  把多个同义 return 归并为一条语义路径（如 95/99 两处的 hasHit2 未命中算一条）。
- group_variants：仅当 parser 漏识了“分支内布尔分组”（如 is95Group/is99Group 这类由分数算出的布尔开关）时提供。
- 若某项 parser 已正确，省略该键；corrections 可为空对象 {}。

如果 parser 完全正确，输出：
```json
{
  "summary": "parser 正确识别了所有分组和轮次，无遗漏",
  "findings": [],
  "corrections": {}
}
```

注意：
- 不要重复 parser 已经正确提取的内容
- 只报告遗漏或错误
- severity=WARN 表示重要遗漏（影响报告准确性），INFO 表示补充信息
"""


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------

def cross_validate(source_code: str, parser_summary: dict) -> CrossValidationResult:
    """
    LLM 交叉校验：对比源码和 parser 结果，找遗漏。

    Args:
        source_code:    原始 .java/.drl 源码全文
        parser_summary: parser 提取的结构化摘要（dict），包含：
                        - rule_name, category_code
                        - groups: [{name, branch_count, rounds}]
                        - gray_buckets: [{name, level, range}]
                        - exit_count
                        - total_branches

    Returns:
        CrossValidationResult
    """
    api_key = _load_api_key()

    # 截断过长源码（保留前 8000 字符 + 后 2000 字符）
    if len(source_code) > 12000:
        truncated = source_code[:8000] + "\n\n... [中间省略] ...\n\n" + source_code[-2000:]
    else:
        truncated = source_code

    user_prompt = f"""## 源码（完整）
```java
{truncated}
```

## Parser 提取结果
```json
{json.dumps(parser_summary, ensure_ascii=False, indent=2)}
```

请对比源码和 parser 结果，输出 JSON 格式的交叉校验报告。"""

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
                    {"role": "system", "content": CROSS_VALIDATE_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,  # 极低温度 → 减少幻觉
                "max_tokens": 2000,
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_llm_response(content)

    except httpx.HTTPStatusError as e:
        return CrossValidationResult(
            llm_summary=f"❌ 交叉校验失败: HTTP {e.response.status_code}",
            raw_response=str(e),
        )
    except httpx.ConnectError:
        return CrossValidationResult(
            llm_summary="❌ 交叉校验失败: 无法连接 API",
        )
    except httpx.TimeoutException:
        return CrossValidationResult(
            llm_summary="❌ 交叉校验失败: 请求超时（90秒）",
        )
    except Exception as e:
        return CrossValidationResult(
            llm_summary=f"❌ 交叉校验失败: {type(e).__name__}",
            raw_response=str(e),
        )


def _parse_llm_response(content: str) -> CrossValidationResult:
    """解析 LLM 返回的 JSON（可能被 ```json 包裹）"""
    import re

    # 提取 JSON 块
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    json_str = json_match.group(1) if json_match else content

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # JSON 解析失败，返回原文作为 summary
        return CrossValidationResult(
            llm_summary=content[:500],
            raw_response=content,
        )

    findings = []
    for f in data.get("findings", []):
        findings.append(CrossValidationFinding(
            category=f.get("category", "info"),
            description=f.get("description", ""),
            severity=f.get("severity", "INFO"),
        ))

    return CrossValidationResult(
        findings=findings,
        corrections=data.get("corrections", {}) or {},
        llm_summary=data.get("summary", ""),
        raw_response=content,
    )


# ---------------------------------------------------------------------------
# 辅助：从 parser 结果生成摘要 dict
# ---------------------------------------------------------------------------

def build_parser_summary(rules: list) -> dict:
    """从 parser 的 RuleMetadata 列表构建交叉校验用的摘要 dict"""
    summaries = []
    for rule in rules:
        # 按 group 聚合分支
        groups = {}
        for b in rule.call_times_branches:
            g = b.group or "(无分组)"
            groups.setdefault(g, {"branch_count": 0, "rounds": []})
            groups[g]["branch_count"] += 1
            groups[g]["rounds"].append(b.call_times)

        summaries.append({
            "rule_name": rule.name,
            "category_code": rule.category_code,
            "total_branches": len(rule.call_times_branches),
            "groups": groups,
            "group_criteria": rule.group_criteria,  # 分支内布尔分组（parser 已识别），避免 LLM 误报 missed_group
            "gray_buckets": [
                {"name": gb.exp_name, "level": gb.level, "range": f"[{gb.start}, {gb.end})"}
                for gb in rule.gray_buckets
            ],
            "exit_count": len(rule.exits),
        })

    return {"rules": summaries}


def apply_corrections(rules: list, corrections: dict) -> int:
    """把 LLM 结构化纠错合并回 RuleMetadata，返回被修正的规则数

    corrections: {rule_name: {"exit_paths": [...], "group_variants": {flag: expr}}}
    原则：只增不覆——语义出口路径作补充展示；group_variants 仅补 parser 漏掉的 flag。
    时间线/模板等精确数据仍由 parser 确定性提供，LLM 碰不到。
    """
    if not corrections:
        return 0
    by_name = {r.name: r for r in rules}
    applied = 0
    for rname, corr in corrections.items():
        rule = by_name.get(rname)
        if not rule or not isinstance(corr, dict):
            continue
        changed = False
        ep = corr.get("exit_paths")
        if ep and isinstance(ep, list):
            rule.llm_exit_paths = [str(x) for x in ep]
            changed = True
        gv = corr.get("group_variants")
        if gv and isinstance(gv, dict):
            for flag, expr in gv.items():
                if flag not in rule.group_criteria:
                    rule.group_criteria[flag] = str(expr)
                    changed = True
        if changed:
            applied += 1
    return applied


# ---------------------------------------------------------------------------
# 报告渲染：追加交叉校验段落
# ---------------------------------------------------------------------------

def render_cross_validation_md(result: CrossValidationResult) -> str:
    """将交叉校验结果渲染为 Markdown 段落（追加到报告末尾）"""
    lines = []
    lines.append("### 🔍 LLM 交叉校验")
    lines.append("")

    if result.llm_summary:
        lines.append(f"> {result.llm_summary}")
        lines.append("")

    if not result.findings:
        lines.append("✅ Parser 提取结果与源码一致，未发现遗漏。")
    else:
        warns = [f for f in result.findings if f.severity == "WARN"]
        infos = [f for f in result.findings if f.severity == "INFO"]

        if warns:
            lines.append(f"⚠️ **Parser 遗漏 ({len(warns)} 项)**")
            for f in warns:
                lines.append(f"- [{f.category}] {f.description}")
            lines.append("")
        if infos:
            lines.append(f"💡 **补充发现 ({len(infos)} 项)**")
            for f in infos:
                lines.append(f"- [{f.category}] {f.description}")
            lines.append("")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM 兜底：解析确定性引擎无法求值的动态延迟时间
# ---------------------------------------------------------------------------

RESOLVE_TIMES_SYSTEM = """你是 Drools 营销策略时间线分析专家。
给定若干“动态延迟”轮次（延迟表达式无法静态求值，如 delaySeconds / delayToNext10 / 变量），
请结合源码与注释，推断每一轮的**触达时刻**（相对进件/注册时刻 T0）。

输出格式（严格 JSON，不要多余文字）：
{
  "resolved": {
    "<group>|<round_no>": "T+1 10:00",
    ...
  }
}
时间标签约定：相对用 "T0+Xmin"/"T0+Xh"/"T0+Xd"；绝对用 "T+N HH:MM"。
只输出你能从源码/注释确证的轮次；无法确证的不要编造、直接省略。
"""


def llm_resolve_times(source_code: str, uncertain_rounds: list) -> dict:
    """用 LLM 解析确定性引擎无法求值的动态延迟轮次

    uncertain_rounds: [{group, round_no, call_times, delay_exprs, comment}, ...]
    返回: {"{group}|{round_no}": "T+1 10:00", ...}
    """
    if not uncertain_rounds:
        return {}

    api_key = _load_api_key()
    if not api_key:
        return {}

    # 源码截断
    if len(source_code) > 12000:
        truncated = source_code[:8000] + "\n...[中间省略]...\n" + source_code[-2000:]
    else:
        truncated = source_code

    rounds_desc = []
    for r in uncertain_rounds:
        rounds_desc.append(
            f'- group={r.get("group", "") or "(无)"}, round=R{r.get("round_no")}, '
            f'callTimes {r.get("call_times", "")}, delay={r.get("delay_exprs", [])}, '
            f'comment={r.get("comment", "")!r}'
        )

    user_prompt = f"""## 源码
```java
{truncated}
```

## 待解析的动态延迟轮次
{chr(10).join(rounds_desc)}

请推断每轮触达时刻，输出 JSON。"""

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
                    {"role": "system", "content": RESOLVE_TIMES_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 1000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        import re
        json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        json_str = json_match.group(1) if json_match else content
        data = json.loads(json_str)
        resolved = data.get("resolved", {})
        return {str(k): str(v) for k, v in resolved.items()}
    except Exception:
        return {}
