"""
checker.py — Drools 策略规则校验器（MVP 第 2 步）

职责：读取 parser.py 产出的 RuleMetadata，套用领域规则书（SKILL.md 硬性规则）
     自动检查常见漏洞，输出问题清单（IssueList）。

检查项来源：SKILL.md「工作流B审查清单」中可机械判定的项。
不可机械判定的项（模板审批、文案匹配）归入「人工确认清单」。

设计原则：
  - 只做能机械判定的检查，做不了的列入「人工确认清单」（INFO 级）
  - 每条检查有明确的 rule_id，方便文档追溯（例如 "MISSING_TAG" → SKILL.md 硬性规则1）
  - 严重度三档：ERROR / WARN / INFO（对应 SKILL.md 工作流B 的三档结论）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from parser import RuleMetadata, RuleExit, CallTimesBranch


# ---------------------------------------------------------------------------
# 检查项注册表（用于 --list-checks 自省）
# ---------------------------------------------------------------------------

# 每项：(rule_id, severity, description, coverage_note)
CHECK_REGISTRY: list[tuple[str, str, str, str]] = [
    # ---- 严格错误（编译失败 / 引擎契约违反）----
    ("MISSING_CATEGORY_CODE",  "ERROR", "规则必须有 categoryCode（否则永不触发）",              "已实现"),
    ("BARE_BINDING_VAR",       "ERROR", "when 子句使用 $request/$response，无裸名",           "已实现"),
    ("DUPLICATE_RULE_NAME",    "ERROR", "同一文件内 rule 名不能重复（否则编译失败）",         "已实现（文件级）"),
    ("FUNC_NAME_CASE",         "WARN",  "buildXxx 函数名大小写正确（buildPush 不是 buildpush）", "已实现"),
    ("MISSING_TAG",            "WARN",  "每个 return 出口必须 setDecisionTag",                "已实现"),
    ("UNSTABLE_EXIT",          "WARN",  "setResultObject(2) 必须搭配 decisionTag",            "已实现"),
    ("NO_OVERFLOW_FALLBACK",   "WARN",  "必须有 callTimes > N 兜底分支",                     "已实现"),
    ("LAST_ROUND_HAS_DELAY",   "WARN",  "末轮不能挂 appendDelayReach",                        "已实现"),
    # ---- 需人工确认（工具无法校验）----
    ("TEMPLATE_IS_VARIABLE",   "INFO",  "buildXxx 模板为变量引用，无法自动核对",                "需人工"),
    ("COUPON_BATCH_REVIEW",    "INFO",  "券批次号需人工核对审批状态",                           "需人工"),
    ("TEMPLATE_CONTENT_MATCH", "INFO",  "模板文案与折扣档位匹配",                               "需人工"),
]
# 业务逻辑类检查（分流顺序、延迟公式符号、哨兵值混入、对照组打标时点）
# 不在此层实现——这些约束在「写」层的 skill 文档中作为生成条件强制遵守。


def list_checks() -> str:
    """输出检查项清单（用于 --list-checks 命令）"""
    strict = [c for c in CHECK_REGISTRY if c[3] == "已实现" or c[3].startswith("已实现（文件级")]
    manual = [c for c in CHECK_REGISTRY if c[3] == "需人工"]

    lines = []
    lines.append(f"🔴 严格错误检查（编译失败 / 引擎契约违反）({len(strict)} 项):")
    for rid, sev, desc, _ in strict:
        lines.append(f"  [{sev:<5}] {rid:<28} {desc}")
    lines.append("")
    lines.append(f"👤 需人工确认（工具无法校验）({len(manual)} 项):")
    for rid, sev, desc, _ in manual:
        lines.append(f"  [{sev:<5}] {rid:<28} {desc}")
    lines.append("")
    lines.append("ℹ️  业务逻辑类（分流顺序、延迟公式符号、哨兵值、getter 混用等）不在此层检查——")
    lines.append("   这些约束在「写」层的 skill 文档中作为生成条件强制遵守。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

Severity = Literal["ERROR", "WARN", "INFO"]


@dataclass
class Issue:
    """一条被检出的问题"""
    severity: Severity
    rule_id: str                       # 检查项 ID，例如 "MISSING_TAG"
    line_no: int | None                # 相关行号（None = 规则级问题）
    message: str                       # 给人看的说明
    hint: str = ""                     # 修复建议（可选）


@dataclass
class CheckResult:
    """单条 rule 的检查结果"""
    rule_name: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "ERROR" for i in self.issues)

    def summary(self) -> str:
        err = sum(1 for i in self.issues if i.severity == "ERROR")
        wrn = sum(1 for i in self.issues if i.severity == "WARN")
        inf = sum(1 for i in self.issues if i.severity == "INFO")
        return f"{self.rule_name}: {err} ERROR / {wrn} WARN / {inf} INFO"


# ---------------------------------------------------------------------------
# 校验入口
# ---------------------------------------------------------------------------

def check_rule(rule: RuleMetadata) -> CheckResult:
    """对单条 rule 跑全部检查项，返回问题清单"""
    result = CheckResult(rule_name=rule.name)

    # ---- 规则级检查 ----
    _check_category_code(rule, result)
    _check_binding_variables(rule, result)          # A类新增：裸变量名
    _check_function_name_case(rule, result)         # A类新增：函数名大小写
    _check_has_overflow_fallback(rule, result)
    _check_last_round_no_delay(rule, result)

    # ---- 出口级检查（逐条 return）----
    for exit_ in rule.exits:
        _check_exit_tag(exit_, result)
        _check_stable_exit(exit_, result)

    # ---- 人工确认提示（INFO 级）----
    _add_manual_review_reminders(rule, result)

    return result


def check_file(rules: list[RuleMetadata]) -> list[CheckResult]:
    """对文件中所有 rule 跑检查，并补充文件级检查（如 rule 名重复）"""
    results = [check_rule(r) for r in rules]

    # 文件级检查：rule 名重复
    seen: dict[str, int] = {}
    for r in rules:
        seen[r.name] = seen.get(r.name, 0) + 1
    for r, cr in zip(rules, results):
        if seen[r.name] > 1:
            cr.issues.append(Issue(
                severity="ERROR",
                rule_id="DUPLICATE_RULE_NAME",
                line_no=None,
                message=f"rule 名 {r.name!r} 在文件中出现 {seen[r.name]} 次（Drools 同 package 内会编译失败）",
                hint="确认是替换还是新增，如果是替换需要删除旧 rule",
            ))

    return results


# ---------------------------------------------------------------------------
# 具体检查项
# ---------------------------------------------------------------------------

def _check_category_code(rule: RuleMetadata, result: CheckResult) -> None:
    """ERROR：categoryCode 不能为空（否则规则永远不会触发）"""
    if not rule.category_code:
        result.issues.append(Issue(
            severity="ERROR",
            rule_id="MISSING_CATEGORY_CODE",
            line_no=None,
            message="未识别到 categoryCode，规则可能永远不会被 PMS 触发",
            hint="检查 when 子句：RequestDto(categoryCode == \"xxx\") 或 "
                 "$request.getCategoryCode().equals(\"xxx\")",
        ))


def _check_binding_variables(rule: RuleMetadata, result: CheckResult) -> None:
    """ERROR：when 子句中的绑定变量必须用 $request / $response，不能用裸名

    Drools 惯例：$request: RequestDto(...) 中，$request 是绑定变量名，
    RequestDto 是类型。如果在 then 体中用了裸名 request / response，
    会导致编译失败或未定义引用。
    例外：显式声明了别名（如 RequestDto request = $request）。
    """
    import re
    body = rule.raw_body

    # 检查 then 体中是否有裸 request / response（不是 $request/$response）
    # 排除声明别名行：RequestDto request = $request
    then_section = body.split("then", 1)[-1] if "then" in body else body

    # 查找裸 request/response 用法：前面没有 $、后面没有 : RequestDto
    # 匹配：request.getXxx() / response.setXxx() 但不是 $request / $response
    bare_req = re.search(r'(?<!\$)(?<![A-Za-z_])request\.', then_section)
    bare_res = re.search(r'(?<!\$)(?<![A-Za-z_])response\.', then_section)

    # 如果有裸名，且没有显式别名声明（RequestDto request = $request）
    has_alias = re.search(r'RequestDto\s+request\s*=', body) or \
                re.search(r'ResponseDto\s+response\s*=', body)

    if (bare_req or bare_res) and not has_alias:
        result.issues.append(Issue(
            severity="ERROR",
            rule_id="BARE_BINDING_VAR",
            line_no=None,
            message="then 体中使用了裸名 request/response，但 when 子句绑定的是 $request/$response",
            hint="统一使用 $request / $response，或在 then 体开头显式声明别名："
                 "RequestDto request = $request;",
        ))


def _check_function_name_case(rule: RuleMetadata, result: CheckResult) -> None:
    """WARN：buildXxx 函数名必须用 camelCase（buildPush 不是 buildpush）

    Java 大小写敏感，buildpush 会编译失败。
    检查所有已知的 ActionFunction 函数名是否大小写正确。
    """
    import re
    body = rule.raw_body

    # 已知函数名正确形式
    correct_names = [
        "buildSms", "buildSmsByUnRegister", "buildWaba", "buildPush",
        "buildPushByUnRegister", "buildPushTelemarketing", "buildMail",
        "buildCoupon", "buildCouponByDelay", "buildIncreaseAmountOfferByDelay",
        "buildAwardCashbackReach", "buildBlu", "appendDelayReach",
    ]

    # 找到所有 buildXxx 调用，检查是否匹配正确形式
    for m in re.finditer(r'\b(build\w+)\s*\(', body):
        called = m.group(1)
        # 检查是否是小写版本（如 buildpush、buildsms）
        lower_version = called.lower()
        correct_lower_map = {n.lower(): n for n in correct_names}
        if lower_version in correct_lower_map and called != correct_lower_map[lower_version]:
            result.issues.append(Issue(
                severity="WARN",
                rule_id="FUNC_NAME_CASE",
                line_no=None,
                message=f"函数名 {called!r} 大小写错误（正确应为 {correct_lower_map[lower_version]!r}）",
                hint=f"Java 大小写敏感，{called} 会编译失败",
            ))


def _check_has_overflow_fallback(rule: RuleMetadata, result: CheckResult) -> None:
    """WARN：每个分组必须有 callTimes > N 兜底分支（防止轮次失控）"""
    if not rule.call_times_branches:
        return

    # 按分组检查
    groups = {}
    for b in rule.call_times_branches:
        g = b.group or "__default__"
        groups.setdefault(g, []).append(b)

    for gname, g_branches in groups.items():
        has_overflow = any(b.call_times.startswith(">") for b in g_branches)
        if not has_overflow:
            label = f"{gname} 组" if gname != "__default__" else ""
            result.issues.append(Issue(
                severity="WARN",
                rule_id="NO_OVERFLOW_FALLBACK",
                line_no=None,
                message=f"{label}缺少 callTimes > N 兜底分支（当前 {len(g_branches)} 个分支）",
                hint="在最后一个 callTimes 分支后添加：if (callTimes > N) { "
                     "$response.setDecisionTag(tag + \"_exceed\"); $response.setResultObject(1); }",
            ))


def _check_last_round_no_delay(rule: RuleMetadata, result: CheckResult) -> None:
    """WARN：每个分组的最后一个明确轮次（== N）不能挂 delay（否则多一轮空决策）"""
    if not rule.call_times_branches:
        return

    # 按分组检查
    groups = {}
    for b in rule.call_times_branches:
        g = b.group or "__default__"
        groups.setdefault(g, []).append(b)

    for gname, g_branches in groups.items():
        equal_branches = [b for b in g_branches if b.call_times.startswith("==")]
        if not equal_branches:
            continue
        last = equal_branches[-1]
        if "appendDelayReach" in last.actions:
            label = f"{gname} 组" if gname != "__default__" else ""
            result.issues.append(Issue(
                severity="WARN",
                rule_id="LAST_ROUND_HAS_DELAY",
                line_no=None,
                message=f"{label}末轮 (callTimes {last.call_times}) 仍挂了 appendDelayReach，会导致多一轮空决策",
                hint="末轮只发触达不挂延迟，链路自然结束",
            ))


def _check_exit_tag(exit_: RuleExit, result: CheckResult) -> None:
    """WARN：每个 return 出口必须有 decisionTag（硬性规则 1：不打标无法归因）"""
    if exit_.decision_tag is None:
        # 但 buildXxx 内部的自动 tag 也合法（parser 已标注为 <auto:...>）
        # 这里只有完全 None 才报警
        result.issues.append(Issue(
            severity="WARN",
            rule_id="MISSING_TAG",
            line_no=exit_.line_no,
            message=f"第 {exit_.line_no} 行 return 前没有 setDecisionTag，落库无法归因",
            hint=f"上下文: {exit_.context or '(无 if 上下文)'}。"
                 "return 前加 $response.setDecisionTag(decisionTag + \"_xxx\")",
        ))


def _check_stable_exit(exit_: RuleExit, result: CheckResult) -> None:
    """WARN：setResultObject(2) 必须搭配 decisionTag（硬性规则 10：标准稳定退出三件套）"""
    if exit_.result_object == 2 and exit_.decision_tag is None:
        result.issues.append(Issue(
            severity="WARN",
            rule_id="UNSTABLE_EXIT",
            line_no=exit_.line_no,
            message=f"第 {exit_.line_no} 行 setResultObject(2) 但缺少 decisionTag，不符合标准稳定退出模式",
            hint="三件套：setDecisionTag(明确tag) + setResultObject(2) + return",
        ))


def _add_manual_review_reminders(rule: RuleMetadata, result: CheckResult) -> None:
    """INFO：列出需要人工确认的项目（checker 机械判定的边界）"""
    # 模板字面量检查：按分组聚合，每组只报一条
    groups_with_var_template = {}  # group -> count
    for branch in rule.call_times_branches:
        has_build = any(a.startswith("build") and a != "buildCoupon" for a in branch.actions)
        if has_build and not branch.templates:
            g = branch.group or "__default__"
            groups_with_var_template[g] = groups_with_var_template.get(g, 0) + 1

    for gname, count in groups_with_var_template.items():
        label = f"{gname} 组" if gname != "__default__" else ""
        result.issues.append(Issue(
            severity="INFO",
            rule_id="TEMPLATE_IS_VARIABLE",
            line_no=None,
            message=f"{label}有 {count} 个分支的 buildXxx 使用了变量引用模板，无法自动核对模板编码",
            hint="人工确认：模板编码是否与模板平台配置一致，审批状态是否已通过",
        ))

    # 券批次号：总是提示人工确认
    for branch in rule.call_times_branches:
        for batch in branch.batch_nos:
            result.issues.append(Issue(
                severity="INFO",
                rule_id="COUPON_BATCH_REVIEW",
                line_no=None,
                message=f"券批次号 {batch!r} 需要人工核对",
                hint="核对：批次号是否审批通过、有效期配置是否符合业务要求（Drools 不管券有效期）",
            ))


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from parser import parse_rule_file

    # 支持 --list-checks 命令
    if len(sys.argv) > 1 and sys.argv[1] == "--list-checks":
        print(list_checks())
        sys.exit(0)

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        print("用法: python checker.py <path_to_rule_file>")
        print("      python checker.py --list-checks  # 查看所有检查项")
        sys.exit(1)

    rules = parse_rule_file(target)
    results = check_file(rules)  # 改用 check_file（含文件级检查）
    for cr in results:
        print(cr.summary())
        for issue in cr.issues:
            loc = f"L{issue.line_no}" if issue.line_no else "规则级"
            print(f"  [{issue.severity}] {loc} [{issue.rule_id}] {issue.message}")
            if issue.hint:
                print(f"         ↳ 修复建议: {issue.hint}")
