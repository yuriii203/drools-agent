"""
tools.py — Agent 可调用的工具定义与实现

做什么：
  为 Agent 提供 5 个工具：
  - read_skill:           读取 skill 规范文档（硬性规则、骨架模板）
  - read_reference:       读取函数速查表（签名、延迟公式、泳道表）
  - run_checker:          校验已生成的 Drools 代码
  - save_strategy:        保存最终代码到文件
  - read_generated_file:  读取已保存的策略文件（用于修改场景）

为什么这样设计：
  Agent 不是"被动接收全文 prompt"，而是主动决定何时调哪个工具、
  获取什么信息。SKILL.md 7000 字预加载（小），reference.md 按需读。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Skill 文档路径（相对于 drools_agent/）
_AGENT_DIR = Path(__file__).parent
_SKILL_DIR = _AGENT_DIR.parent / ".qoder" / "skills" / "drools-strategy"

# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def read_skill() -> str:
    """读取 SKILL.md，返回核心规范（硬性规则 + 骨架模板）"""
    skill_file = _SKILL_DIR / "SKILL.md"
    if not skill_file.exists():
        return f"错误：未找到 skill 文件 {skill_file}"
    
    content = skill_file.read_text(encoding="utf-8")
    
    # 去掉 YAML frontmatter（--- ... ---）
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()
    
    return content


def read_reference() -> str:
    """读取 reference.md，返回函数速查表（签名、延迟公式、泳道表）"""
    ref_file = _SKILL_DIR / "reference.md"
    if not ref_file.exists():
        return f"错误：未找到 reference 文件 {ref_file}"
    return ref_file.read_text(encoding="utf-8")


def run_checker(code: str) -> str:
    """
    对生成的 Drools 代码运行校验器。
    
    临时写入文件 → 调 parser + checker → 返回结果文本。
    """
    from parser import parse_rule_file
    from checker import check_file
    
    tmp_path = _AGENT_DIR / "_tmp_check.java"
    try:
        tmp_path.write_text(code, encoding="utf-8")
        rules = parse_rule_file(tmp_path)
        results = check_file(rules)
        
        lines = []
        err_count = 0
        for cr in results:
            for iss in cr.issues:
                icon = {"ERROR": "🔴", "WARN": "🟡", "INFO": "👤"}.get(iss.severity, "?")
                line_info = f"L{iss.line_no}" if iss.line_no else "规则级"
                lines.append(f"{icon} {iss.severity} [{iss.rule_id}] {line_info}: {iss.message}")
                lines.append(f"   建议: {iss.hint}")
                if iss.severity == "ERROR":
                    err_count += 1
        
        if not lines:
            return "✅ 校验通过：0 ERROR, 0 WARN, 0 INFO"
        
        summary = f"发现 {err_count} ERROR, 共 {len(lines)//2} 个问题"
        return summary + "\n" + "\n".join(lines)
    
    except Exception as e:
        return f"❌ 校验异常: {type(e).__name__}: {e}"
    
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def save_strategy(code: str, filename: str, output_dir: str | None = None) -> str:
    """将生成的策略代码保存到文件"""
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = _AGENT_DIR.parent / "generated"
    
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # 确保文件名有后缀
    if not filename.endswith((".java", ".drl")):
        filename += ".java"
    
    full_path = out_dir / filename
    full_path.write_text(code, encoding="utf-8")
    return f"✅ 已保存到: {full_path}"


def read_generated_file(filename: str) -> str:
    """
    读取 generated/ 目录下已保存的策略文件。
    用于用户要求修改已有策略时，先读取当前代码。
    """
    gen_dir = _AGENT_DIR.parent / "generated"
    
    # 支持带或不带路径的文件名
    file_path = gen_dir / filename
    if not file_path.exists():
        # 尝试加 .java 后缀
        file_path = gen_dir / (filename + ".java")
    if not file_path.exists():
        # 列出已有文件帮 Agent 定位
        existing = [f.name for f in gen_dir.glob("*.java")] + [f.name for f in gen_dir.glob("*.drl")]
        if existing:
            return f"错误：未找到 '{filename}'。已有文件: {', '.join(existing)}"
        return f"错误：generated/ 目录下没有任何文件"
    
    content = file_path.read_text(encoding="utf-8")
    return f"文件: {file_path.name} ({len(content)} 字符)\n```java\n{content}\n```"


def analyze_existing(file_path: str) -> str:
    """
    分析已有策略文件，返回结构化摘要（读写协同的核心工具）。
    
    Agent 调此工具后可以：
      - 理解旧策略的分组、轮次、渠道、延迟设计
      - 以此为参考模板生成新策略
      - 回答用户关于旧策略的问题
    """
    from parser import parse_rule_file
    from checker import check_file
    
    path = Path(file_path)
    if not path.exists():
        # 尝试在策略代码目录找
        alt_path = _AGENT_DIR.parent / "策略代码" / file_path
        if alt_path.exists():
            path = alt_path
        else:
            # 尝试 generated 目录
            alt_path2 = _AGENT_DIR.parent / "generated" / file_path
            if alt_path2.exists():
                path = alt_path2
            else:
                return f"错误：文件不存在 '{file_path}'"
    
    try:
        rules = parse_rule_file(path)
        results = check_file(rules)
    except Exception as e:
        return f"错误：解析失败 — {type(e).__name__}: {e}"
    
    # 构建摘要
    summary_parts = []
    summary_parts.append(f"📄 文件: {path.name}")
    summary_parts.append(f"📊 规则数: {len(rules)}")
    
    for rule, cr in zip(rules, results):
        summary_parts.append(f"\n--- rule: {rule.name} ---")
        summary_parts.append(f"categoryCode: {rule.category_code}")
        summary_parts.append(f"salience: {rule.salience}")
        
        # 灰度分流
        if rule.gray_buckets:
            summary_parts.append(f"灰度分流 ({len(rule.gray_buckets)} 层):")
            for gb in rule.gray_buckets:
                pct = (gb.end - gb.start) * 100
                summary_parts.append(f"  L{gb.level}: {gb.exp_name} [{gb.start},{gb.end}) = {pct:.0f}%")
        
        # 分组和轮次
        groups = {}
        for b in rule.call_times_branches:
            g = b.group or "(无分组)"
            groups.setdefault(g, []).append(b)
        
        summary_parts.append(f"分组数: {len(groups)}")
        for gname, branches in groups.items():
            eq_branches = [b for b in branches if b.call_times.startswith("==")]
            gt_branches = [b for b in branches if b.call_times.startswith(">")]
            summary_parts.append(f"  [{gname}] {len(eq_branches)} 轮" + (f" + 兜底>{gt_branches[0].call_times[2:]}" if gt_branches else ""))
            
            # 每轮摘要（只展示前5轮+最后1轮，避免太长）
            for b in eq_branches[:5]:
                actions = ", ".join(b.actions) if b.actions else "无动作"
                delay = f" delay={b.delay_expr}" if b.delay_expr else ""
                tpl = f" tpl={b.templates[0]}" if b.templates else ""
                summary_parts.append(f"    callTimes {b.call_times}: {actions}{tpl}{delay}")
            if len(eq_branches) > 5:
                summary_parts.append(f"    ... (中间省略 {len(eq_branches)-6} 轮)")
                b = eq_branches[-1]
                actions = ", ".join(b.actions) if b.actions else "无动作"
                delay = f" delay={b.delay_expr}" if b.delay_expr else ""
                summary_parts.append(f"    callTimes {b.call_times}: {actions}{delay}")
        
        # 模板编码
        all_templates = set()
        for b in rule.call_times_branches:
            all_templates.update(b.templates)
        if all_templates:
            summary_parts.append(f"模板编码: {', '.join(sorted(all_templates))}")
        
        # 校验结果
        err = sum(1 for i in cr.issues if i.severity == "ERROR")
        wrn = sum(1 for i in cr.issues if i.severity == "WARN")
        inf = sum(1 for i in cr.issues if i.severity == "INFO")
        summary_parts.append(f"校验: {err} ERROR / {wrn} WARN / {inf} INFO")
        if err > 0 or wrn > 0:
            for iss in cr.issues:
                if iss.severity in ("ERROR", "WARN"):
                    summary_parts.append(f"  ⚠️ [{iss.rule_id}] {iss.message}")
    
    return "\n".join(summary_parts)


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

# 函数名 → 实现函数
TOOL_FUNCTIONS: dict[str, callable] = {
    "read_skill":           read_skill,
    "read_reference":       read_reference,
    "run_checker":          run_checker,
    "save_strategy":        save_strategy,
    "read_generated_file":  read_generated_file,
    "analyze_existing":     analyze_existing,
}

# OpenAI 兼容格式的工具定义（给 DeepSeek API 的 schema）
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "读取 Drools 策略开发规范（硬性规则 11 条、规则骨架模板、灰度三级泳道规范、高频坑速查）。生成新策略前必须调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_reference",
            "description": "读取 ActionFunction 函数速查表（buildSms/buildWaba/buildPush/buildMail/buildCoupon 的参数顺序）、延迟公式、RequestDto 字段表、灰度泳道全表。生成代码时查参数顺序和延迟公式用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_checker",
            "description": "对生成的 Drools 代码运行自动校验（检查 categoryCode、setDecisionTag、callTimes 兜底、ActionFunction 前缀等）。生成代码后必须调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要校验的完整 Drools .java/.drl 代码",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_strategy",
            "description": "将最终生成的策略代码保存到文件。只在所有校验通过后调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "完整的策略代码内容",
                    },
                    "filename": {
                        "type": "string",
                        "description": "文件名（如 my_strategy.java）",
                    },
                },
                "required": ["code", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_generated_file",
            "description": "读取 generated/ 目录下已保存的策略文件。当用户要求修改已有策略时，先调用此工具获取当前代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名（如 NewUser_SMS_Reminder.java）",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_existing",
            "description": "分析已有策略文件，返回结构化摘要（分组、轮次、渠道、延迟、灰度、问题清单）。当用户提供参考策略文件时，调此工具理解其结构，然后以此为模板生成新策略。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "策略文件路径（如 ../策略代码/给额未发标push场景.java 或上传的文件名）",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]


def call_tool(name: str, arguments: dict) -> str:
    """根据函数名调用对应工具，返回结果字符串"""
    func = TOOL_FUNCTIONS.get(name)
    if not func:
        return f"错误：未知工具 '{name}'，可用工具: {list(TOOL_FUNCTIONS.keys())}"
    
    try:
        return func(**arguments)
    except TypeError as e:
        return f"错误：工具 '{name}' 参数不匹配 — {e}"
    except Exception as e:
        return f"错误：工具 '{name}' 执行异常 — {type(e).__name__}: {e}"
