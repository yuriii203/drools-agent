"""
report.py — 报告生成器（MVP 第 3 步）

职责：把 parser.py 的结构化数据 + checker.py 的问题清单，
     渲染成两种格式：
       - JSON（机器消费：后续测试case生成、跨规则冲突检测）
       - Markdown（人读：策略摘要 + 风险报告）

设计原则：
  - Markdown 报告面向非技术用户（领导/业务方），要一目了然
  - JSON 保留全部字段，供下游程序消费
  - 报告内嵌 checker 输出，不隐藏问题
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from parser import RuleMetadata
    from checker import CheckResult


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------

def render_markdown(
    rules: list["RuleMetadata"],
    results: list["CheckResult"],
) -> str:
    """将解析结果 + 检查结果渲染成完整 Markdown 报告"""
    lines: list[str] = []

    lines.append("# Drools 策略分析报告")
    lines.append("")

    # ---- 逐条 rule 输出 ----
    for rule, cr in zip(rules, results):
        lines.extend(_render_rule_section(rule, cr))

    # ---- 尾部：检查项摘要 ----
    lines.extend(_render_check_summary(results))

    return "\n".join(lines)


def _render_rule_section(rule: "RuleMetadata", cr: "CheckResult") -> list[str]:
    """渲染单条 rule 的完整报告段落"""
    lines: list[str] = []

    # 标题 + 元数据
    lines.append(f"## 规则: {rule.name}")
    lines.append("")
    lines.append("| 属性 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 策略名 | {rule.name} |")
    lines.append(f"| categoryCode | `{rule.category_code or '(未识别)'} ` |")
    lines.append(f"| salience | {rule.salience if rule.salience is not None else '(默认)' } |")
    lines.append(f"| 源文件 | `{rule.file_path}` |")
    lines.append("")

    # 灰度分流
    lines.append("### 灰度分流")
    if rule.gray_buckets:
        lines.append("")
        lines.append("| 泳道名 | 层级 | 区间 | 比例 |")
        lines.append("|---|---|---|---|")
        for b in rule.gray_buckets:
            pct = f"{(b.end - b.start) * 100:.0f}%"
            lines.append(f"| {b.exp_name} | 第{b.level}级 | [{b.start}, {b.end}) | {pct} |")
    else:
        lines.append("*(无灰度分流)*")
    lines.append("")

    # 分组口径（分支内布尔分组，如 is95Group/is99Group）
    if rule.group_criteria:
        lines.append("### 分组口径（分支内布尔分组）")
        lines.append("")
        for flag, expr in rule.group_criteria.items():
            lines.append(f"- **{flag}**: `{expr}`")
        lines.append("")

    # 轮次时间线
    lines.append("### 轮次时间线")
    lines.append("")
    if rule.call_times_branches:
        # 检测是否有分组
        groups_in_branches = []
        for b in rule.call_times_branches:
            if b.group and b.group not in groups_in_branches:
                groups_in_branches.append(b.group)

        if groups_in_branches:
            # 多分组模式：按组分别展示
            for gname in groups_in_branches:
                g_branches = [b for b in rule.call_times_branches if b.group == gname]
                lines.append(f"#### 🟢 {gname} 组（{len(g_branches)} 轮）")
                lines.append("")
                for b in g_branches:
                    branch_label = f"callTimes {b.call_times}"
                    actions_str = ", ".join(b.actions) if b.actions else "*(无动作)*"
                    lines.append(f"- **{branch_label}**: {actions_str}")
                    if b.templates:
                        lines.append(f"  - 模板: {', '.join(b.templates)}")
                    if b.batch_nos:
                        lines.append(f"  - 券批次: {', '.join(b.batch_nos)}")
                    if b.delay_expr:
                        lines.append(f"  - 延迟公式: `{b.delay_expr}`")
                lines.append("")
        else:
            # 无分组：扁平展示
            for b in rule.call_times_branches:
                branch_label = f"callTimes {b.call_times}"
                actions_str = ", ".join(b.actions) if b.actions else "*(无动作)*"
                lines.append(f"- **{branch_label}**: {actions_str}")
                if b.templates:
                    lines.append(f"  - 模板: {', '.join(b.templates)}")
                if b.batch_nos:
                    lines.append(f"  - 券批次: {', '.join(b.batch_nos)}")
                if b.group_templates:
                    for flag, tpls in b.group_templates.items():
                        lines.append(f"  - {flag}: {', '.join(tpls)}")
                if b.delay_expr:
                    lines.append(f"  - 延迟公式: `{b.delay_expr}`")
    else:
        lines.append("*(未识别到 callTimes 分支)*")
    lines.append("")

    # 出口路径
    lines.append("### 出口路径")
    lines.append("")
    lines.append("| 行号 | decisionTag | resultObject | 挂延迟 | 上下文 |")
    lines.append("|---|---|---|---|---|")
    for e in rule.exits:
        tag_display = e.decision_tag if e.decision_tag else "**⚠️ 缺失**"
        res_display = str(e.result_object) if e.result_object is not None else "-"
        delay_mark = "✓" if e.has_delay else "✗"
        ctx = e.context[:40] + "..." if len(e.context) > 40 else e.context
        lines.append(f"| L{e.line_no} | `{tag_display}` | {res_display} | {delay_mark} | `{ctx}` |")
    lines.append("")

    # LLM 纠错归并后的语义出口路径（补充展示，与上方精确 return 列表互补）
    if rule.llm_exit_paths:
        lines.append(f"**语义出口路径（LLM 归并，共 {len(rule.llm_exit_paths)} 条）**：上表为 {len(rule.exits)} 个原始 return 出口点，")
        lines.append("LLM 将同义 return 归并为以下语义路径：")
        lines.append("")
        for i, p in enumerate(rule.llm_exit_paths, 1):
            lines.append(f"{i}. {p}")
        lines.append("")

    # 问题清单
    lines.append("### 问题清单")
    lines.append("")
    if cr.issues:
        errors = [i for i in cr.issues if i.severity == "ERROR"]
        warns = [i for i in cr.issues if i.severity == "WARN"]
        infos = [i for i in cr.issues if i.severity == "INFO"]

        if errors:
            lines.append(f"🔴 **ERROR ({len(errors)} 项)**")
            for i in errors:
                lines.append(f"- [{i.rule_id}] {i.message}")
            lines.append("")
        if warns:
            lines.append(f"🟡 **WARN ({len(warns)} 项)**")
            for i in warns:
                lines.append(f"- [{i.rule_id}] {i.message}")
            lines.append("")
        if infos:
            lines.append(f"👤 **INFO / 人工确认 ({len(infos)} 项)**")
            for i in infos:
                lines.append(f"- [{i.rule_id}] {i.message}")
            lines.append("")
    else:
        lines.append("✅ 无问题检出")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


def _render_check_summary(results: list["CheckResult"]) -> list[str]:
    """报告尾部：整体统计"""
    total_err = sum(sum(1 for i in cr.issues if i.severity == "ERROR") for cr in results)
    total_wrn = sum(sum(1 for i in cr.issues if i.severity == "WARN") for cr in results)
    total_inf = sum(sum(1 for i in cr.issues if i.severity == "INFO") for cr in results)

    lines = [
        "## 整体统计",
        "",
        f"| 指标 | 数量 |",
        f"|---|---|",
        f"| 规则数 | {len(results)} |",
        f"| ERROR | {total_err} |",
        f"| WARN | {total_wrn} |",
        f"| INFO（人工确认）| {total_inf} |",
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# JSON 报告
# ---------------------------------------------------------------------------

def render_json(
    rules: list["RuleMetadata"],
    results: list["CheckResult"],
) -> str:
    """将解析结果 + 检查结果序列化为 JSON（机器消费）"""
    data = {
        "report_version": "1.0",
        "rules": [],
    }
    for rule, cr in zip(rules, results):
        rule_dict = asdict(rule)
        # raw_body 太大，JSON 里不保留（需要时看源码）
        rule_dict.pop("raw_body", None)
        rule_dict["issues"] = [asdict(i) for i in cr.issues]
        data["rules"].append(rule_dict)
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def generate_report(
    rules: list["RuleMetadata"],
    results: list["CheckResult"],
    md_path: str | None = None,
    json_path: str | None = None,
) -> tuple[str, str]:
    """生成 Markdown + JSON 报告，可选写入文件"""
    md_content = render_markdown(rules, results)
    json_content = render_json(rules, results)

    if md_path:
        from pathlib import Path
        Path(md_path).write_text(md_content, encoding="utf-8")
    if json_path:
        from pathlib import Path
        Path(json_path).write_text(json_content, encoding="utf-8")

    return md_content, json_content


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from parser import parse_rule_file
    from checker import check_file

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        print("用法: python report.py <path_to_rule_file> [output.md]")
        sys.exit(1)

    output_md = sys.argv[2] if len(sys.argv) > 2 else None

    rules = parse_rule_file(target)
    results = check_file(rules)
    md, _ = generate_report(rules, results, md_path=output_md)
    print(md)
    if output_md:
        print(f"\n📄 报告已保存: {output_md}")
