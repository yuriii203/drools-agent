"""
flow_html.py — 确定性 HTML 流程图渲染器

设计原则（关键）：
  时间线是"累计延迟求和"的纯数学计算，必须精确 → 用确定性代码，不用 LLM。
  数据源 = parser.py 解析出的 RuleMetadata（与 MD 报告、交叉校验共享同一份）。

延迟语义（已与源码注释核对）：
  第 N 轮触达时刻 = 前 N-1 轮 delay 之和
  branch[N-1].delay_exprs 排的是第 N+1 轮的触达时刻

动态延迟处理（delaySeconds 等无法静态求值）：
  1. 算术求值（"20*60" → 1200秒）
  2. 失败 → 从分支注释提取绝对时间（"T+1 8点触达" → 次日08:00）
  3. 再失败 → 已知绝对时间函数表
  4. 全失败 → 标注"⏳动态(待确认)"，绝不瞎编
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from parser import RuleMetadata, CallTimesBranch, parse_rule_file


# ---------------------------------------------------------------------------
# 渠道映射（ActionFunction → CSS 类 + 中文名）
# ---------------------------------------------------------------------------
CHANNEL_MAP = {
    "buildSms": ("sms", "短信"),
    "buildPush": ("push", "Push"),
    "buildMail": ("mail", "邮件"),
    "buildWaba": ("waba", "WhatsApp"),
    "buildCoupon": ("coupon", "发券"),
    "buildCouponWithAmount": ("coupon", "发券(带金额)"),
}

# 已知绝对时间延迟函数 → (天偏移, 时, 分)
ABSOLUTE_DELAY_FUNCTIONS = {
    "delayToNextDay8": (1, 8, 0),
    "delayToNextDay10": (1, 10, 0),
    "delayTo2Days10": (2, 10, 0),
    "delayTo2Days8": (2, 8, 0),
}


# ---------------------------------------------------------------------------
# 延迟求值
# ---------------------------------------------------------------------------
def eval_arithmetic(expr: str) -> Optional[int]:
    """对纯算术延迟表达式求值，返回秒数；无法求值返回 None

    支持: "20 * 60"、"24 * 60 * 60"、"2 * 60 * 60" 等
    不支持: 变量(delaySeconds)、函数调用(getDelaySeconds(...))
    """
    if not expr:
        return None
    e = expr.strip()
    # 只允许数字、空格、* / + - ( )
    if not re.fullmatch(r'[\d\s\*\+\-\(\)/]+', e):
        return None
    try:
        val = eval(e, {"__builtins__": {}}, {})
        return int(val)
    except Exception:
        return None


def parse_comment_time(comment: str) -> Optional[tuple]:
    """从分支注释提取时间线索

    返回:
      ("absolute", day, hour, minute)  — 如 "T+1 8点触达" → (absolute, 1, 8, 0)
      ("relative", seconds)            — 如 "T0 16小时触达" → (relative, 57600)
      None                             — 无线索
    """
    if not comment:
        return None
    # 绝对时间：T+N H点 / T+N H点M分
    m = re.search(r'T\+(\d+)\s*(\d{1,2})\s*点\s*(\d{1,2})?\s*分?', comment)
    if m:
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3)) if m.group(3) else 0
        return ("absolute", day, hour, minute)
    # 绝对时间：次日/注册次日 H:MM（如“注册次日9:30”）→ day=1
    m = re.search(r'次日\s*(\d{1,2})\s*[:：]\s*(\d{1,2})', comment)
    if m:
        return ("absolute", 1, int(m.group(1)), int(m.group(2)))
    # 绝对时间：第N天 H:MM / 第N天 H点（第二天 = T+1）
    m = re.search(r'第([一二三四五六七八九十\d]+)天\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})?', comment)
    if m:
        d = _cn_day(m.group(1))
        if d:
            return ("absolute", d - 1, int(m.group(2)), int(m.group(3)) if m.group(3) else 0)
    # 相对时间：T0 X天 / T0 X小时 / T0 X分钟
    m = re.search(r'T0\s*(\d+)\s*天', comment)
    if m:
        return ("relative", int(m.group(1)) * 86400)
    m = re.search(r'T0\s*(\d+)\s*小时', comment)
    if m:
        return ("relative", int(m.group(1)) * 3600)
    m = re.search(r'T0\s*(\d+)\s*分钟', comment)
    if m:
        return ("relative", int(m.group(1)) * 60)
    return None


def resolve_absolute_function(expr: str) -> Optional[tuple]:
    """识别已知绝对时间延迟函数 → (day, hour, minute)"""
    if not expr:
        return None
    for fname, (d, h, mi) in ABSOLUTE_DELAY_FUNCTIONS.items():
        if fname in expr:
            return (d, h, mi)
    return None


def _cn_day(s: str) -> Optional[int]:
    """中文/阿拉伯天序 → 整数（第二天→2）"""
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if s.isdigit():
        return int(s)
    return cn.get(s)


def resolve_next_hh(expr: str) -> Optional[tuple]:
    """识别 delayToNext<HHMM> 型延迟（如 delayToNext10=下一个10:00、delayToNext930=下一个9:30）→ (hour, minute)

    语义：延迟到“下一个 HH:MM”（若当前已过则顺延一天）。
    位数约定：2位=HH、3位=H+MM、4位=HH+MM。
    """
    if not expr:
        return None
    m = re.search(r'delayToNext(?:Day)?(\d{2,4})(?:[_:.(](\d{1,2}))?(?![\d)])', expr)
    if m:
        if m.group(2):
            hh, mi = int(m.group(1)), int(m.group(2))
        else:
            v = m.group(1)
            if len(v) == 2:
                hh, mi = int(v), 0
            elif len(v) == 3:
                hh, mi = int(v[0]), int(v[1:])
            else:
                hh, mi = int(v[:2]), int(v[2:])
        if 0 <= hh <= 23 and 0 <= mi <= 59:
            return (hh, mi)
    return None


def parse_comment_day(comment: str) -> Optional[int]:
    """从注释提取纯天偏移（只有天、无钟点），如 "T+1:" / "T+2天" / "第二天" → 1 / 2 / 1"""
    if not comment:
        return None
    m = re.search(r'T\+(\d+)\s*[:：]', comment)
    if m:
        return int(m.group(1))
    m = re.search(r'T\+(\d+)\s*天', comment)
    if m:
        return int(m.group(1))
    # 第N天（第二天 = T+1）
    m = re.search(r'第([一二三四五六七八九十\d]+)天', comment)
    if m:
        d = _cn_day(m.group(1))
        if d:
            return d - 1
    return None


# ---------------------------------------------------------------------------
# 时间格式化
# ---------------------------------------------------------------------------
def format_relative(sec: int) -> str:
    """秒 → T0 相对时间标签"""
    if sec == 0:
        return "T0（进件时刻）"
    if sec < 3600:
        return f"T0+{sec // 60}min"
    if sec < 86400:
        if sec % 3600 == 0:
            return f"T0+{sec // 3600}h"
        return f"T0+{sec // 60}min"
    d = sec // 86400
    rem = sec % 86400
    if rem == 0:
        return f"T0+{d}d"
    if rem % 3600 == 0:
        return f"T0+{d}d{rem // 3600}h"
    return f"T0+{d}d{rem // 60}min"


def format_absolute(total_min: float) -> str:
    """自 T0 当天 00:00 起的总分钟 → T+N HH:MM"""
    total_min = int(round(total_min))
    day = total_min // 1440
    rem = total_min % 1440
    hh = rem // 60
    mm = rem % 60
    return f"T+{day} {hh:02d}:{mm:02d}"


# ---------------------------------------------------------------------------
# 时间线计算
# ---------------------------------------------------------------------------
@dataclass
class RoundTime:
    """一轮的触达时间信息"""
    round_no: int                 # 轮次序号（1-based）
    call_times: str               # "== 1" / "> 7"
    branch_index: int             # 对应 branches 列表索引
    label: str                    # 显示标签
    kind: str                     # relative / absolute / uncertain / terminal
    channel_class: str = "sms"    # CSS 类
    channel_name: str = ""        # 渠道中文名
    actions: list = None
    template: str = ""
    group_templates: dict = None   # 分支内布尔分组模板变体 {flag: [templates]}
    delay_exprs: list = None
    comment: str = ""
    line_no: Optional[int] = None
    is_overflow: bool = False     # 是否兜底分支（callTimes > N）
    is_last: bool = False         # 是否末轮（不再排期）


def compute_timeline(branches: list[CallTimesBranch], overrides: dict = None) -> list[RoundTime]:
    """计算一组分支的触达时间线

    核心：第 N 轮触达时刻 = 前 N-1 轮 delay 之和。
    动态延迟用注释/函数表解析为绝对时间锚点，之后切换绝对模式累加。
    overrides: {round_no: label} —— LLM 兜底解析出的时间（用于确定性无法解析的轮）
    """
    rounds: list[RoundTime] = []
    overrides = overrides or {}
    cum_seconds = 0                 # 相对模式累计秒
    anchored = False                # 是否已切换到绝对时间锚点
    anchor_total_min = 0.0          # 绝对模式：自 T0 当天 00:00 的分钟
    prev_unresolved = False         # 前一轮延迟无法解析

    n = len(branches)
    for i, b in enumerate(branches):
        round_no = i + 1
        is_overflow = b.call_times.strip().startswith(">")
        # 末轮：== 分支且无 delay（不再排期）
        is_last = (not is_overflow) and (not b.delay_exprs)

        # --- 1. 确定当前轮触达时间（优先级：兜底 > LLM兜底 > 未解析 > 绝对 > 相对）---
        if is_overflow:
            label, kind = "兜底（超出轮次）", "terminal"
        elif prev_unresolved and round_no in overrides:
            label, kind = overrides[round_no], "llm"
        elif prev_unresolved:
            label, kind = "⏳动态(待确认)", "uncertain"
        elif anchored:
            label, kind = format_absolute(anchor_total_min), "absolute"
        else:
            label, kind = format_relative(cum_seconds), "relative"

        # --- 2. 渠道/动作信息 ---
        channel_class, channel_name = "sms", ""
        for act in b.actions:
            if act in CHANNEL_MAP:
                channel_class, channel_name = CHANNEL_MAP[act]
                break
        all_channels = []
        for act in b.actions:
            if act in CHANNEL_MAP and CHANNEL_MAP[act][1] not in all_channels:
                all_channels.append(CHANNEL_MAP[act][1])
        if len(all_channels) > 1:
            channel_name = "+".join(all_channels)

        rounds.append(RoundTime(
            round_no=round_no,
            call_times=b.call_times,
            branch_index=i,
            label=label,
            kind=kind,
            channel_class=channel_class,
            channel_name=channel_name or "（无动作）",
            actions=list(b.actions),
            template=b.templates[0] if b.templates else "",
            group_templates=dict(b.group_templates) if b.group_templates else None,
            delay_exprs=list(b.delay_exprs),
            comment=b.comment,
            line_no=b.line_no,
            is_overflow=is_overflow,
            is_last=is_last,
        ))

        # --- 3. 处理当前轮 delay（排下一轮），更新累计 ---
        if is_overflow or not b.delay_exprs:
            # 兜底/末轮不排期，后续轮（若有）无法用算术推进
            if i < n - 1:
                prev_unresolved = True
            continue

        # 多 delay 时，用最后一个排下一轮（前面的排同轮后续触达）
        delay_expr = b.delay_exprs[-1]
        sec = eval_arithmetic(delay_expr)

        if sec is not None:
            prev_unresolved = False
            if anchored:
                anchor_total_min += sec / 60.0
            else:
                cum_seconds += sec
        else:
            # 动态延迟：尝试解析为绝对锚点
            resolved = False
            next_comment = branches[i + 1].comment if i + 1 < n else ""
            # 3a. delayToNext<HH> 型：延迟到下一个 HH:MM
            nxt = resolve_next_hh(delay_expr)
            if nxt:
                hh, mi = nxt
                if anchored:
                    # 推进到当前锚点之后下一个 HH:MM（若已过则 +1 天）
                    cand = int(anchor_total_min // 1440) * 1440 + hh * 60 + mi
                    if cand <= anchor_total_min:
                        cand += 1440
                    anchor_total_min = cand
                    resolved = True
                else:
                    day = parse_comment_day(next_comment) or parse_comment_day(b.comment)
                    if day is not None:
                        anchor_total_min = day * 1440 + hh * 60 + mi
                        anchored = True
                        resolved = True
            # 3b. 已知绝对时间函数表
            if not resolved:
                abs_fn = resolve_absolute_function(delay_expr)
                if abs_fn:
                    d, h, mi = abs_fn
                    anchor_total_min = d * 1440 + h * 60 + mi
                    anchored = True
                    resolved = True
            # 3c. 注释绝对/相对时间（含“次日9:30”“T+1 8点”）
            if not resolved:
                ct = parse_comment_time(next_comment) or parse_comment_time(b.comment)
                if ct and ct[0] == "absolute":
                    _, d, h, mi = ct
                    anchor_total_min = d * 1440 + h * 60 + mi
                    anchored = True
                    resolved = True
                elif ct and ct[0] == "relative":
                    cum_seconds = ct[1]
                    resolved = True
            prev_unresolved = not resolved

    return rounds


# ---------------------------------------------------------------------------
# CSS（内嵌，源自 strategy-flow-html/reference.md，保证部署稳健）
# ---------------------------------------------------------------------------
FLOW_CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f6fa;color:#222;margin:0;padding:24px}
.wrap{max-width:1400px;margin:0 auto}
.hd{background:#fff;border-radius:12px;padding:20px 24px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:20px}
.hd h1{margin:0 0 8px;font-size:22px;color:#1a73e8}
.hd .meta{color:#666;font-size:13px;line-height:1.8}
.hd .badge{display:inline-block;background:#e8f0fe;color:#1a73e8;border-radius:4px;padding:2px 8px;margin-right:6px;font-size:12px}
.lane{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
.lane-t{font-size:16px;font-weight:600;margin-bottom:14px;padding-left:10px;border-left:4px solid #1a73e8}
.tl{position:relative;padding-left:8px;overflow-x:auto}
.tl::before{content:'';position:absolute;left:0;top:24px;bottom:24px;width:2px;background:#dadce0}
.rnd{position:relative;padding:10px 0 10px 28px;margin-bottom:6px}
.rnd::before{content:'';position:absolute;left:-1px;top:22px;width:12px;height:12px;border-radius:50%;border:2px solid #fff;background:#9aa0a6;box-shadow:0 0 0 2px #9aa0a6}
.rnd-sms::before{background:#34a853;box-shadow:0 0 0 2px #34a853}
.rnd-push::before{background:#fbbc04;box-shadow:0 0 0 2px #fbbc04}
.rnd-mail::before{background:#4285f4;box-shadow:0 0 0 2px #4285f4}
.rnd-waba::before{background:#25d366;box-shadow:0 0 0 2px #25d366}
.rnd-coupon::before{background:#ea4335;box-shadow:0 0 0 2px #ea4335}
.rnd-terminal::before{background:#9aa0a6;box-shadow:0 0 0 2px #9aa0a6}
.rnd-uncertain::before{background:#fff;box-shadow:0 0 0 2px #ea4335;border:2px dashed #ea4335}
.rnd-llm::before{background:#a142f4;box-shadow:0 0 0 2px #a142f4}
.rnd-hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rnd-no{font-weight:700;font-size:14px;color:#202124}
.rnd-ch{font-size:12px;padding:2px 8px;border-radius:10px;color:#fff;background:#9aa0a6}
.ch-sms{background:#34a853}.ch-push{background:#fbbc04;color:#333}.ch-mail{background:#4285f4}
.ch-waba{background:#25d366}.ch-coupon{background:#ea4335}
.rnd-time{font-size:12px;color:#5f6368;background:#f1f3f4;border-radius:4px;padding:2px 8px;font-family:monospace}
.rnd-time.abs{background:#fef7e0;color:#b06000}
.rnd-time.unc{background:#fce8e6;color:#c5221f}
.rnd-time.llm{background:#f3e8fd;color:#7627bb}
.rnd-ct{font-size:11px;color:#80868b;font-family:monospace}
.rnd-detail{font-size:12px;color:#5f6368;margin-top:4px;line-height:1.6}
.rnd-detail code{background:#f1f3f4;border-radius:3px;padding:1px 5px;font-size:11px;color:#c5221f}
.rnd-cmt{font-size:11px;color:#80868b;margin-top:2px;font-style:italic}
.sum{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
.sum h2{font-size:16px;margin:0 0 12px;color:#202124}
.sum table{width:100%;border-collapse:collapse;font-size:13px}
.sum th,.sum td{border:1px solid #e0e0e0;padding:8px 10px;text-align:left}
.sum th{background:#f8f9fa;font-weight:600}
.sum tr:nth-child(even) td{background:#fafafa}
.ft{text-align:center;color:#9aa0a6;font-size:12px;margin-top:20px}
"""


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------
def _esc(s: str) -> str:
    """HTML 转义"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_round(rt: RoundTime) -> str:
    """渲染单个轮次节点"""
    # 节点 CSS 类
    if rt.kind == "uncertain":
        node_cls = "rnd rnd-uncertain"
    elif rt.kind == "terminal":
        node_cls = "rnd rnd-terminal"
    elif rt.kind == "llm":
        node_cls = f"rnd rnd-llm"
    else:
        node_cls = f"rnd rnd-{rt.channel_class}"

    # 时间标签样式
    if rt.kind == "absolute":
        time_cls = "rnd-time abs"
    elif rt.kind == "uncertain":
        time_cls = "rnd-time unc"
    elif rt.kind == "llm":
        time_cls = "rnd-time llm"
    else:
        time_cls = "rnd-time"

    # 渠道徽章
    ch_cls = f"ch-{rt.channel_class}" if rt.kind not in ("terminal", "uncertain") else ""
    badge = f'<span class="rnd-ch {ch_cls}">{_esc(rt.channel_name)}</span>' if rt.channel_name else ""

    # 详情行
    details = []
    if rt.group_templates:
        for flag, tpls in rt.group_templates.items():
            details.append(f'{_esc(flag)}: <code>{_esc(", ".join(tpls))}</code>')
    elif rt.template:
        details.append(f'模板 <code>{_esc(rt.template)}</code>')
    if rt.delay_exprs:
        details.append(f'排期延迟 <code>{_esc(", ".join(rt.delay_exprs))}</code>')
    elif not rt.is_overflow:
        details.append('<span style="color:#34a853">末轮·不再排期</span>' if rt.is_last else '')
    detail_html = f'<div class="rnd-detail">{" · ".join(d for d in details if d)}</div>' if any(details) else ""

    # 注释行（含时间线索）
    cmt_html = f'<div class="rnd-cmt">📝 {_esc(rt.comment[:80])}</div>' if rt.comment else ""

    # callTimes + 行号
    ct_html = f'<span class="rnd-ct">callTimes {_esc(rt.call_times)}'
    if rt.line_no:
        ct_html += f' · L{rt.line_no}'
    ct_html += '</span>'

    return f'''<div class="{node_cls}">
  <div class="rnd-hd">
    <span class="rnd-no">R{rt.round_no}</span>
    {badge}
    <span class="{time_cls}">{_esc(rt.label)}</span>
    {ct_html}
  </div>
  {detail_html}
  {cmt_html}
</div>'''


def _render_summary_table(group_name: str, rounds: list[RoundTime]) -> str:
    """渲染一组的时间线表格"""
    rows = []
    for rt in rounds:
        delay = ", ".join(rt.delay_exprs) if rt.delay_exprs else "—"
        if rt.group_templates:
            tpl_cell = "<br>".join(f'{_esc(k)}: {_esc(", ".join(v))}' for k, v in rt.group_templates.items())
        else:
            tpl_cell = _esc(rt.template or "—")
        rows.append(
            f'<tr><td>R{rt.round_no}</td><td>{_esc(rt.channel_name)}</td>'
            f'<td>{_esc(rt.label)}</td><td><code>{_esc(delay)}</code></td>'
            f'<td>{tpl_cell}</td></tr>'
        )
    title = f"{group_name} 组" if group_name else "时间线"
    return f'''<div class="sum">
  <h2>📊 {title}轮次时间线（{len(rounds)} 轮）</h2>
  <table>
    <thead><tr><th>轮次</th><th>渠道</th><th>触达时间</th><th>排期延迟</th><th>模板</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>'''


def split_groups(rule: RuleMetadata) -> list:
    """把一条 rule 的分支按 group 切分（保持顺序）→ [(group_name, branches), ...]"""
    branches = rule.call_times_branches
    group_order = []
    group_branches = {}
    for b in branches:
        g = b.group or ""
        if g not in group_branches:
            group_order.append(g)
            group_branches[g] = []
        group_branches[g].append(b)
    return [(g, group_branches[g]) for g in group_order]


def render_flow_html(rule: RuleMetadata, source_file: str = "", time_overrides: dict = None) -> str:
    """渲染单条 rule 的 HTML 流程图

    time_overrides: {"{group}|{round_no}": label} —— LLM 兜底解析的时间
    """
    branches = rule.call_times_branches
    time_overrides = time_overrides or {}

    # 按 group 切分（保持顺序）
    group_order = []
    group_branches = {}
    for b in branches:
        g = b.group or ""
        if g not in group_branches:
            group_order.append(g)
            group_branches[g] = []
        group_branches[g].append(b)

    # 头部信息
    badges = [f'<span class="badge">categoryCode: {_esc(rule.category_code or "未识别")}</span>']
    if rule.salience is not None:
        badges.append(f'<span class="badge">salience: {rule.salience}</span>')
    badges.append(f'<span class="badge">{len(group_order)} 个分组</span>')
    badges.append(f'<span class="badge">{len(branches)} 个分支</span>')
    for gb in rule.gray_buckets:
        badges.append(f'<span class="badge">🎛 {_esc(gb.exp_name)} [{gb.start}, {gb.end})]</span>')

    meta_lines = []
    if source_file:
        meta_lines.append(f"源文件：{_esc(Path(source_file).name)}")
    if len(rule.exits) > 0:
        meta_lines.append(f"退出路径：{len(rule.exits)} 个 halt()")

    # 泳道 + 汇总表
    lanes_html = []
    sums_html = []
    for g in group_order:
        gb = group_branches[g]
        # 本组的 overrides：{round_no: label}
        g_overrides = {}
        for key, lab in time_overrides.items():
            kg, _, rn = key.partition("|")
            if kg == g and rn.isdigit():
                g_overrides[int(rn)] = lab
        rounds = compute_timeline(gb, g_overrides)
        gname = g if g else "默认"
        # 泳道
        nodes = "".join(_render_round(rt) for rt in rounds)
        lanes_html.append(f'''<div class="lane">
  <div class="lane-t">🏊 {gname} 组（{len(rounds)} 轮）</div>
  <div class="tl">{nodes}</div>
</div>''')
        # 汇总表
        sums_html.append(_render_summary_table(gname, rounds))

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{_esc(rule.name)} - 策略流程图</title>
<style>{FLOW_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <h1>📋 {_esc(rule.name)}</h1>
    <div class="meta">{"".join(badges)}</div>
    <div class="meta">{" · ".join(meta_lines)}</div>
  </div>
  {"".join(lanes_html)}
  {"".join(sums_html)}
  <div class="ft">由 Drools 策略 Agent 确定性渲染（parser → flow_html，非 LLM 生成）· 时间线基于累计延迟精确计算</div>
</div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# 文件级便捷入口
# ---------------------------------------------------------------------------
def generate_flow_html_for_file(file_path: str | Path) -> dict:
    """解析文件并生成每条 rule 的 HTML 流程图

    Returns: {"file": ..., "flows": [{"rule_name":..., "html":...}, ...]}
    """
    path = Path(file_path)
    rules = parse_rule_file(path)
    flows = []
    for rule in rules:
        html = render_flow_html(rule, str(path))
        flows.append({"rule_name": rule.name, "html": html})
    return {"file": path.name, "flows": flows}
