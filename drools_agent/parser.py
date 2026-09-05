"""
parser.py — Drools 策略规则解析器（MVP 第 1 步）

职责：读取 .drl / .java 文件，提取语义关键信息，输出结构化数据对象。
下游：checker.py（校验）和 report.py（报告生成都消费这里的数据。

设计选择：
  - 用正则 + 启发式提取，不写完整 AST 解析器（Drools 是 Java 语法糖，
    写 AST 太重，正则对结构良好的 .drl 够用且可控）。
  - 输出用 @dataclass，不用 dict（类型契约明确，字段改名时 IDE 会报错）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 数据结构（解析产出物 = 数据契约）
# ---------------------------------------------------------------------------

@dataclass
class RuleExit:
    """规则中每一个 return 出口的快照"""
    line_no: int                       # 源码行号（从 1 开始）
    decision_tag: Optional[str]        # setDecisionTag 的参数（可能为 None = 漏打标）
    result_object: Optional[int]       # setResultObject 的参数（1=对照/2=排除，None=漏打）
    has_delay: bool                    # 这个 return 之前是否挂了 appendDelayReach
    context: str = ""                  # 所处 if 分支简述（报告里方便定位）


@dataclass
class CallTimesBranch:
    """callTimes 判等的每一个分支"""
    call_times: str                    # "==" 或 ">" + 数值，例如 "== 1"、"> 3"
    group: str = ""                    # 所属分组名（如 "regular"/"plus"），无分组时为空
    actions: list[str] = field(default_factory=list)     # buildSms / buildMail / appendDelayReach 等
    templates: list[str] = field(default_factory=list)   # 模板编码
    batch_nos: list[str] = field(default_factory=list)   # 券批次号
    delay_expr: Optional[str] = None                     # appendDelayReach 延迟公式原文（首个，向后兼容）
    delay_exprs: list[str] = field(default_factory=list)  # 一轮内全部 appendDelayReach 延迟公式（按顺序）
    comment: str = ""                  # 分支上方的 // 注释（常含“T+1 8点”等时间线索）
    line_no: Optional[int] = None      # 分支在源文件的行号（定位用）
    group_templates: dict = field(default_factory=dict)  # 分支内布尔分组模板变体 {flag: [templates]}，如 {is95Group:[...], is99Group:[...]}


@dataclass
class GrayBucket:
    """灰度分流信息"""
    exp_name: str                      # 泳道名（如 overall_new_user）
    start: float                       # 区间左边界
    end: float                         # 区间右边界
    level: int                         # 1=一级/2=二级/3=三级（按命名推断）


@dataclass
class RuleMetadata:
    """一条 rule 的全部语义信息（解析器对一条 rule 的最终产出）"""
    name: str
    file_path: str
    category_code: str
    salience: Optional[int] = None
    gray_buckets: list[GrayBucket] = field(default_factory=list)
    call_times_branches: list[CallTimesBranch] = field(default_factory=list)
    exits: list[RuleExit] = field(default_factory=list)
    group_criteria: dict = field(default_factory=dict)   # 布尔分组flag → 定义表达式，如 {is95Group: "regScore > 0 && regScore <= 0.125"}
    llm_exit_paths: list = field(default_factory=list)   # LLM 纠错归并后的语义出口路径（补充展示）
    raw_body: str = ""                 # 源码保留，报告里引用原文片段用


# ---------------------------------------------------------------------------
# 解析入口
# ---------------------------------------------------------------------------

def parse_rule_file(file_path: str | Path) -> list[RuleMetadata]:
    """读取一个文件，提取所有 rule 块，逐条解析"""
    text = Path(file_path).read_text(encoding="utf-8")
    rules: list[RuleMetadata] = []

    # 切出每个 rule 块：rule "名字" ... end
    # - 非贪婪 .*? 保证 end 是当前 rule 自己的 end
    # - DOTALL 让 . 匹配换行符
    pattern = re.compile(
        r'rule\s+"(?P<name>[^"]+)"\s*'
        r'(?P<body>.*?)'
        r'\nend\s*',
        re.DOTALL,
    )

    for m in pattern.finditer(text):
        # 计算 rule body 在源文件的起始行号（用于分支行号定位）
        body_base_line = text[:m.start("body")].count("\n") + 1
        md = _parse_single_rule(
            name=m.group("name"),
            body=m.group("body"),
            file_path=str(file_path),
            base_line=body_base_line,
        )
        rules.append(md)

    if not rules:
        raise ValueError(f"{file_path}: 未识别到任何 rule 块，请检查文件格式")
    return rules


# ---------------------------------------------------------------------------
# 单条 rule 解析
# ---------------------------------------------------------------------------

def _parse_single_rule(name: str, body: str, file_path: str, base_line: int = 1) -> RuleMetadata:
    """解析单个 rule 的 when/then 体，填充 RuleMetadata"""
    md = RuleMetadata(name=name, file_path=file_path, category_code="", raw_body=body)

    # salience
    m = re.search(r'salience\s+(\d+)', body)
    md.salience = int(m.group(1)) if m else None

    # categoryCode：同时匹配 Java getter 形式和 Drools pattern 形式
    #   形式A：getCategoryCode().equals("xxx")      -- Java getter
    #   形式B：RequestDto(categoryCode == "xxx")    -- Drools pattern（更常见）
    m = re.search(r'getCategoryCode\(\)\s*\.equals\("([^"]+)"\)', body)
    if not m:
        m = re.search(r'RequestDto\s*\([^)]*categoryCode\s*==\s*"([^"]+)"', body)
    md.category_code = m.group(1) if m else ""

    # 灰度桶：get_hashed_isin("桶名", userId, start, end)
    for gm in re.finditer(
        r'get_hashed_isin\(\s*"([^"]+)"\s*,\s*[^,]+,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)',
        body,
    ):
        md.gray_buckets.append(GrayBucket(
            exp_name=gm.group(1),
            start=float(gm.group(2)),
            end=float(gm.group(3)),
            level=_infer_swimlane_level(gm.group(1)),
        ))

    # 布尔分组 flag（如 boolean is95Group = regScore > 0 && ...;）→ 分支内模板变体
    for fm in re.finditer(r'boolean\s+(\w+)\s*=\s*([^;]+);', body):
        md.group_criteria[fm.group(1)] = fm.group(2).strip()

    # 模板变量字面量（如 String coupon95 ="batch_demo_xxx";）→ 解析 buildXxx 的变量参数
    var_literals = {}
    for vm in re.finditer(r'String\s+(\w+)\s*=\s*"([^"]+)"', body):
        var_literals[vm.group(1)] = vm.group(2)

    # callTimes 分支 + 出口
    md.call_times_branches = _extract_call_times_branches(
        body, base_line, flag_names=set(md.group_criteria.keys()), var_literals=var_literals,
    )
    # group_criteria 只保留真正作为模板变体出现的 flag（排除 alreadyStamped 这类守卫布尔量）
    used_flags = set()
    for b in md.call_times_branches:
        used_flags.update(b.group_templates.keys())
    md.group_criteria = {k: v for k, v in md.group_criteria.items() if k in used_flags}
    md.exits = _extract_exits(body)

    return md


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _infer_swimlane_level(name: str) -> int:
    """根据桶名推断泳道层级（本文档规定的三级泳道命名约定）"""
    if name.startswith("overall_new_user"):
        return 1
    if name.endswith(("_telemarketing", "_com", "_coupon", "_push")):
        return 3
    return 2


def _extract_balanced_braces(text: str, start: int) -> tuple[int, int]:
    """从 start 位置（text[start] 必须是 '{'）提取平衡大括号块

    返回 (开括号位置, 闭括号位置+1)，即 text[start:end] 是完整的 {…}
    如果找不到平衡的闭括号，返回 (start, len(text))
    """
    depth = 0
    i = start
    while i < len(text):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    return start, len(text)


def _extract_preceding_comment(text: str) -> str:
    """从一段文本里提取 // 注释内容（多行拼接）

    用于抓取分支上方的时间线索注释，如“// 第13轮：T+1 8点触达”。
    """
    comments = re.findall(r'//\s*(.+)', text)
    return " ".join(c.strip() for c in comments).strip()


def _extract_call_times_branches(body: str, base_line: int = 1, flag_names: set = None, var_literals: dict = None) -> list[CallTimesBranch]:
    """抽 callTimes / request.getCallTimes() 的 if / else if / else 分支

    支持：
      1. 无分组（扁平 callTimes 链）
      2. 多分组（外层 "xxx".equals(groupType) 包裹，每组内部独立 callTimes 链）
      3. 分支内布尔分组模板变体（is95Group/is99Group）→ group_templates
    """
    flag_names = flag_names or set()
    var_literals = var_literals or {}
    # 先检测是否有分组层
    groups = _detect_groups(body)
    if groups:
        # 多分组模式：对每个分组块单独解析 callTimes
        all_branches: list[CallTimesBranch] = []
        for group_name, group_block, block_line_offset in groups:
            sub_branches = _parse_calltimes_chain(group_block, base_line + block_line_offset, flag_names, var_literals)
            for b in sub_branches:
                b.group = group_name
            all_branches.extend(sub_branches)
        return all_branches
    else:
        # 无分组：直接解析整个 body
        return _parse_calltimes_chain(body, base_line, flag_names, var_literals)


def _detect_groups(body: str) -> list[tuple[str, str]]:
    """检测分组层："xxx".equals(varName) 或 varName.equals("xxx") 包裹的 if/else-if 块

    返回 [(group_name, block_content), ...] 或空列表（无分组）
    """
    # 匹配: if ("xxx".equals(var)) 或 else if ("xxx".equals(var))
    # 也匹配: if (var.equals("xxx"))
    GROUP_RE = re.compile(
        r'(?:else\s+)?if\s*\(\s*'
        r'(?:"([^"]+)"\s*\.equals\s*\([\w.]+\)'   # "xxx".equals(var)
        r'|[\w.]+\.equals\s*\(\s*"([^"]+)"\s*\))'  # var.equals("xxx")
        r'\s*\)'
    )

    # 额外检测：赋值型分组（如 groupType = "plus"; if (groupType == "plus")）
    GROUP_EQ_RE = re.compile(
        r'(?:else\s+)?if\s*\(\s*'
        r'[\w.]+\s*==\s*"([^"]+)"'
        r'\s*\)'
    )

    matches = []
    for m in GROUP_RE.finditer(body):
        name = m.group(1) or m.group(2)
        if name:  # 排除 null 等
            matches.append((name, m))

    if not matches:
        for m in GROUP_EQ_RE.finditer(body):
            name = m.group(1)
            if name:
                matches.append((name, m))

    if len(matches) < 2:
        # 少于2个分组条件 → 不算分组模式
        return []

    # 验证：每个分组块内部必须包含 callTimes 条件
    COND_RE = r'[\w.]*[cC]allTimes(?:\(\))?\s*(?:==|>)\s*\d+'
    groups: list[tuple[str, str, int]] = []
    for name, m in matches:
        brace_pos = body.find('{', m.end())
        if brace_pos < 0:
            continue
        block_start, block_end = _extract_balanced_braces(body, brace_pos)
        block_content = body[block_start + 1:block_end - 1]
        if re.search(COND_RE, block_content):
            # 分组块内容在 body 中的行偏移（用于计算分支的源文件行号）
            block_line_offset = body[:block_start + 1].count("\n")
            groups.append((name, block_content, block_line_offset))

    return groups if len(groups) >= 2 else []


def _resolve_tpl_arg(arg: str, var_literals: dict) -> str:
    """模板参数可能是字面量 "xxx" 或变量 coupon95，统一解析为字面量"""
    arg = arg.strip()
    if arg.startswith('"') and arg.endswith('"'):
        return arg[1:-1]
    return var_literals.get(arg, arg)


def _extract_tpl_from_block(block: str, var_literals: dict) -> tuple:
    """从一个代码块抽 (templates, batch_nos)，支持字面量与变量参数"""
    templates, batch_nos = [], []
    for tpl in re.finditer(r'build(?:Sms|Push|Mail)\s*\([^,]+,\s*[^,]+,\s*("[^"]+"|[\w.]+)', block):
        templates.append(_resolve_tpl_arg(tpl.group(1), var_literals))
    for tpl in re.finditer(r'buildWaba\s*\([^,]+,\s*("[^"]+"|[\w.]+)', block):
        templates.append(_resolve_tpl_arg(tpl.group(1), var_literals))
    for bn in re.finditer(r'buildCoupon\w*\s*\([^,]+,\s*("[^"]+"|[\w.]+)', block):
        batch_nos.append(_resolve_tpl_arg(bn.group(1), var_literals))
    return templates, batch_nos


def _parse_calltimes_chain(body: str, base_line: int = 1, flag_names: set = None, var_literals: dict = None) -> list[CallTimesBranch]:
    """在一个代码块内解析 callTimes 的 if/else-if 链

    base_line：当前代码块在源文件的起始行号（用于计算分支行号）
    flag_names：布尔分组flag名集合（用于抽分支内模板变体）
    var_literals：模板变量→字面量映射
    """
    flag_names = flag_names or set()
    var_literals = var_literals or {}
    branches: list[CallTimesBranch] = []

    COND_RE = r'[\w.]*[cC]allTimes(?:\(\))?\s*(==|>)\s*(\d+)'

    first_m = re.search(r'if\s*\(\s*' + COND_RE + r'\s*\)', body)
    if not first_m:
        return branches

    pos = first_m.start()
    chain_mode = False
    first_iter = True

    while pos < len(body):
        # 跳过当前位置前的注释和空白
        skipped = _skip_comments(body[pos:])
        # 计算实际跳过的字符数
        effective_pos = pos + (len(body[pos:]) - len(skipped))

        # 分支上方的注释区域（首个分支往前看 4 行，其余看被跳过的区域）
        if first_iter:
            preceding_lines = body[:pos].split("\n")[-4:]
            comment_region = "\n".join(preceding_lines)
        else:
            comment_region = body[pos:effective_pos]

        if_m = re.match(r'\s*(?:else\s+)?if\s*\(\s*' + COND_RE + r'\s*\)', skipped)
        if if_m:
            op = if_m.group(1)
            n = if_m.group(2)
            brace_search_start = effective_pos + if_m.end()
        else:
            if chain_mode:
                else_m = re.match(r'\s*else\s*\{', skipped)
                if else_m:
                    op, n = '>', 'max'
                    brace_search_start = effective_pos + else_m.end() - 1
                else:
                    break
            else:
                next_m = re.search(r'if\s*\(\s*' + COND_RE + r'\s*\)', skipped)
                if next_m:
                    op = next_m.group(1)
                    n = next_m.group(2)
                    brace_search_start = effective_pos + next_m.end()
                else:
                    break

        brace_pos = body.find('{', brace_search_start)
        if brace_pos < 0:
            break

        block_start, block_end = _extract_balanced_braces(body, brace_pos)
        inner = body[block_start + 1:block_end - 1]

        branch = CallTimesBranch(call_times=f"{op} {n}")
        branch.comment = _extract_preceding_comment(comment_region)
        branch.line_no = base_line + body[:effective_pos].count("\n")

        for act in re.finditer(r'(build\w+|appendDelayReach)\s*\(', inner):
            branch.actions.append(act.group(1))

        tpl_list, bn_list = _extract_tpl_from_block(inner, var_literals)
        branch.templates.extend(tpl_list)
        branch.batch_nos.extend(bn_list)

        # 分支内布尔分组模板变体：if (is95Group){...} else if (is99Group){...}
        for flag in flag_names:
            for fm in re.finditer(r'(?:else\s+)?if\s*\(\s*' + re.escape(flag) + r'\s*\)\s*\{', inner):
                bs, be = _extract_balanced_braces(inner, fm.end() - 1)
                fblock = inner[bs + 1:be - 1]
                f_tpl, f_bn = _extract_tpl_from_block(fblock, var_literals)
                if f_tpl or f_bn:
                    branch.group_templates.setdefault(flag, []).extend(f_tpl + f_bn)
                break  # 每个 flag 只取首个匹配块（if 与 else-if 各会命中一次，靠 finditer 遍历）

        # 延迟公式：一轮内可能多次 appendDelayReach，全部按顺序提取
        for dm in re.finditer(r'appendDelayReach\s*\([^,]+,\s*(.+?)\)\s*;', inner):
            branch.delay_exprs.append(dm.group(1).strip())
        if branch.delay_exprs:
            branch.delay_expr = branch.delay_exprs[0]

        branches.append(branch)
        pos = block_end
        first_iter = False

        # 跳过注释和空行，检测是否进入链式模式
        rest = _skip_comments(body[pos:])
        if rest.startswith('else'):
            chain_mode = True

    return branches


def _skip_comments(text: str) -> str:
    """跳过开头的空白、单行注释(//...)、多行注释(/*...*/)，返回第一个有效字符开始的内容"""
    i = 0
    while i < len(text):
        # 跳过空白
        if text[i] in ' \t\r\n':
            i += 1
        # 跳过单行注释
        elif text[i:i+2] == '//':
            nl = text.find('\n', i)
            i = nl + 1 if nl >= 0 else len(text)
        # 跳过多行注释
        elif text[i:i+2] == '/*':
            end = text.find('*/', i + 2)
            i = end + 2 if end >= 0 else len(text)
        else:
            break
    return text[i:]


def _extract_exits(body: str) -> list[RuleExit]:
    """抽每个 return 出口的 tag / resultObject / 是否有 delay"""
    exits: list[RuleExit] = []
    lines = body.splitlines()

    for i, line in enumerate(lines):
        if not re.search(r'\breturn\b', line):
            continue

        # 向前看 5 行，找最近的 setDecisionTag / setResultObject
        window = "\n".join(lines[max(0, i - 5):i + 1])
        tag_m = re.search(r'setDecisionTag\(\s*([^)]+)\)', window)

        # 如果 return 前没有显式 setDecisionTag，但调用了 buildXxx：
        # buildXxx 内部会自动 setDecisionTag，第 5 个参数就是 tag（buildWaba 第 4 个）
        if not tag_m:
            build_m = re.search(
                r'build(?:Sms|Push|Mail|Coupon\w*)\s*\([^;]+,\s*([^,)]+(?:\+[^,)]+)*)\s*\)',
                window,
            )
            if build_m:
                tag_m = type('M', (), {'group': lambda self, n: f"<auto:{build_m.group(1).strip()}>"})()

        res_m = re.search(r'setResultObject\(\s*(\d+)\s*\)', window)
        has_delay = bool(re.search(r'appendDelayReach\s*\(', window))

        exits.append(RuleExit(
            line_no=i + 1,
            decision_tag=tag_m.group(1).strip() if tag_m else None,
            result_object=int(res_m.group(1)) if res_m else None,
            has_delay=has_delay,
            context=_brief_context(lines[max(0, i - 8):i]),
        ))

    return exits


def _brief_context(prev_lines: list[str]) -> str:
    """取 return 前最近的 if 条件作为上下文简述"""
    for ln in reversed(prev_lines):
        if "if (" in ln or "if(" in ln:
            return ln.strip()
    return ""


# ---------------------------------------------------------------------------
# 快速自测：直接运行本文件解析你已有的策略
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from pprint import pprint

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        print("用法: python parser.py <path_to_rule_file>")
        print("示例: python parser.py ../your_strategy.java")
        sys.exit(1)

    rules = parse_rule_file(target)
    for r in rules:
        print(f"\n=== rule: {r.name} ===")
        print(f"categoryCode: {r.category_code}")
        print(f"salience: {r.salience}")
        print(f"灰度桶: {r.gray_buckets}")
        print(f"callTimes 分支数: {len(r.call_times_branches)}")
        for b in r.call_times_branches:
            group_info = f" [{b.group}]" if b.group else ""
            print(f"  - callTimes {b.call_times}{group_info}: actions={b.actions}, tpl={b.templates}, batch={b.batch_nos}, delay={b.delay_expr}")
        print(f"出口数: {len(r.exits)}")
        for e in r.exits:
            print(f"  - L{e.line_no}: tag={e.decision_tag} result={e.result_object} hasDelay={e.has_delay} | {e.context}")
