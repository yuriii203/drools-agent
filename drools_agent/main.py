"""
main.py — Drools 策略 Agent 命令行入口

读模式（分析已有策略）：
  python main.py <规则文件>                  分析策略，输出报告到终端
  python main.py <规则文件> -o report.md     分析并保存 Markdown 报告
  python main.py <规则文件> --llm            启用 LLM 业务叙述

写模式（生成新策略）：
  python main.py --write "需求描述"          Agent 生成策略代码

工具：
  python main.py --list-checks              查看全部检查项清单
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="drools-agent",
        description="Drools 策略 Agent：读模式（分析已有策略）/ 写模式（生成新策略）",
    )
    parser.add_argument("file", nargs="?", help="要分析的 .drl / .java 规则文件路径（读模式）")
    parser.add_argument("-o", "--output", help="保存 Markdown 报告的文件路径")
    parser.add_argument("--json", dest="json_output", help="保存 JSON 报告的文件路径")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 业务叙述（调 DeepSeek API）")
    parser.add_argument("--with-flow", action="store_true", help="同时生成 HTML 流程图（确定性渲染时间线）")
    parser.add_argument("--write", metavar="REQUIREMENT", help="写模式：描述策略需求，Agent 生成代码")
    parser.add_argument("--list-checks", action="store_true", help="输出全部检查项清单后退出")

    args = parser.parse_args()

    # --list-checks：输出检查项清单
    if args.list_checks:
        from checker import list_checks
        print(list_checks())
        return 0

    # --write：写模式（Agent 生成策略）
    if args.write:
        from agent import run_write
        run_write(args.write, output_dir=args.output)
        return 0

    # 没有指定文件：提示用法
    if not args.file:
        parser.print_help()
        return 1

    # 检查文件存在
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}", file=sys.stderr)
        return 1

    # 解析
    from parser import parse_rule_file
    from checker import check_file
    from report import generate_report

    print(f"📖 解析: {file_path}")
    rules = parse_rule_file(file_path)
    print(f"   找到 {len(rules)} 条规则")

    # 校验
    print(f"🔍 校验: 跑 {len(rules)} 条规则的全部检查项...")
    results = check_file(rules)

    # 统计
    total_err = sum(sum(1 for i in cr.issues if i.severity == "ERROR") for cr in results)
    total_wrn = sum(sum(1 for i in cr.issues if i.severity == "WARN") for cr in results)
    total_inf = sum(sum(1 for i in cr.issues if i.severity == "INFO") for cr in results)

    # --llm：先交叉校验（corrections 需在渲染报告前合并进元数据）
    cv_result = None
    if args.llm:
        from cross_validate import cross_validate, build_parser_summary, apply_corrections

        print("🔍 LLM: 交叉校验 parser 提取结果...")
        source_code = file_path.read_text(encoding="utf-8")
        parser_summary = build_parser_summary(rules)
        cv_result = cross_validate(source_code, parser_summary)
        if cv_result.corrections:
            n_fix = apply_corrections(rules, cv_result.corrections)
            if n_fix:
                print(f"   🛠 已应用 LLM 结构化纠错（{n_fix} 条规则）")
        if cv_result.findings:
            warn_count = sum(1 for f in cv_result.findings if f.severity == "WARN")
            info_count = sum(1 for f in cv_result.findings if f.severity == "INFO")
            print(f"   发现: {warn_count} 项遗漏, {info_count} 项补充")
        else:
            print("   ✅ Parser 无遗漏")

    # 生成报告（含纠错后的分组口径/语义出口）
    md_content, json_content = generate_report(
        rules, results,
        md_path=args.output if not args.llm else None,  # --llm 模式下先不存文件
        json_path=args.json_output,
    )

    # --llm：业务叙述 + 交叉校验段落
    if args.llm:
        from llm import generate_narrative
        from cross_validate import render_cross_validation_md

        print("🤖 LLM: 生成业务叙述...")

        # 构造问题摘要文本（给 LLM 看的）
        issues_lines = []
        for cr in results:
            for iss in cr.issues:
                sev_map = {"ERROR": "🔴 ERROR", "WARN": "🟡 WARN", "INFO": "👤 INFO"}
                line_info = f"L{iss.line_no}" if iss.line_no else "规则级"
                issues_lines.append(
                    f"- {sev_map.get(iss.severity, iss.severity)}: "
                    f"[{line_info}] {iss.message} (建议: {iss.hint})"
                )
        issues_text = "\n".join(issues_lines) if issues_lines else "无问题"

        narrative = generate_narrative(json_content, issues_text)
        cv_md = render_cross_validation_md(cv_result)

        # 最终报告 = LLM 叙述 + 数据表格 + 交叉校验
        md_content = f"# 业务总结\n\n{narrative}\n\n---\n\n{md_content}\n\n---\n\n{cv_md}"

        # 存文件
        if args.output:
            Path(args.output).write_text(md_content, encoding="utf-8")
            print(f"   ✅ LLM 增强报告已保存: {args.output}")

    # --with-flow：生成 HTML 流程图（确定性渲染，不依赖 LLM）
    if args.with_flow:
        from flow_html import render_flow_html

        print("🎨 流程图: 渲染 HTML 时间线...")
        # 输出路径：优先用 -o 的同名 .html，否则用输入文件名
        if args.output:
            flow_path = Path(args.output).with_suffix(".html")
        else:
            flow_path = file_path.with_name(file_path.stem + "_flow.html")

        # 多条 rule 时拼接所有流程图
        html_parts = [render_flow_html(rule, str(file_path)) for rule in rules]
        if len(html_parts) == 1:
            flow_html_content = html_parts[0]
        else:
            # 多 rule：用分隔线拼接 body（简单策略）
            flow_html_content = html_parts[0].replace(
                "</div>\n</body>",
                "".join(p.split('<div class="wrap">', 1)[1].rsplit("</div>\n</body>", 1)[0] for p in html_parts[1:]) + "</div>\n</body>",
            )
        flow_path.write_text(flow_html_content, encoding="utf-8")
        print(f"   ✅ HTML 流程图已保存: {flow_path}")

    # 输出
    if not args.output:
        # 没指定 -o 就打到终端
        print()
        print(md_content)

    # 总结
    print("=" * 60)
    print(f"✅ 分析完成")
    print(f"   规则数: {len(rules)}")
    print(f"   🔴 ERROR: {total_err}   🟡 WARN: {total_wrn}   👤 INFO: {total_inf}")
    if args.output:
        print(f"   📄 Markdown 报告: {args.output}")
    if args.with_flow:
        print(f"   🎨 HTML 流程图: {flow_path}")
    if args.json_output:
        print(f"   📦 JSON 报告: {args.json_output}")
    if total_err > 0:
        print(f"\n⚠️  发现 {total_err} 个 ERROR，建议修复后再上线")
        return 2  # 非零退出码表示有严重问题

    return 0


if __name__ == "__main__":
    sys.exit(main())
