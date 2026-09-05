"""
app.py — Drools 策略 Agent Web UI（Streamlit）

用法：
  cd drools_agent
  streamlit run app.py

功能：
  - 📊 分析模式：上传 .drl/.java 文件 → 解析 + 校验 + LLM 叙述 → 可视化报告
  - ✏️ 生成模式：聊天界面 → Agent 追问 + 生成 + 校验 + 修改
"""
import streamlit as st
import tempfile
from pathlib import Path

# ⚠️ Streamlit 进程会在 sys.modules 里缓存已 import 的项目模块：
# 改代码后旧进程仍用旧类定义（如 CallTimesBranch 缺 group/delay_exprs 字段）
# 导致 AttributeError。每次 rerun 先清除项目模块缓存，保证读到最新代码。
import sys as _sys
for _m in ["parser", "checker", "report", "llm", "cross_validate", "flow_html", "agent", "tools"]:
    _sys.modules.pop(_m, None)

# 项目路径
AGENT_DIR = Path(__file__).parent
GENERATED_DIR = AGENT_DIR.parent / "generated"

# ---------------------------------------------------------------------------
# 页面配置
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Drools 策略 Agent",
    page_icon="🤖",
    layout="wide",
)

# 自定义样式
st.markdown("""
<style>
    .stChatMessage { padding: 0.8rem; }
    .tool-event { background: #f0f2f6; padding: 0.5rem 1rem; border-radius: 8px; margin: 4px 0; font-size: 0.85em; }
    .metric-card { text-align: center; padding: 1rem; }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("🤖 Drools 策略 Agent")
    st.markdown("分析已有策略 / 生成新策略")
    st.divider()
    mode = st.radio("选择模式", ["✏️ 生成策略", "📊 分析策略"], label_visibility="collapsed")
    st.divider()
    st.markdown("**工具链**")
    st.caption("read_skill → read_reference → run_checker → save_strategy")
    st.caption(f"已生成文件: {len(list(GENERATED_DIR.glob('*.java')))} 个" if GENERATED_DIR.exists() else "暂无生成文件")

# ---------------------------------------------------------------------------
# 分析模式
# ---------------------------------------------------------------------------

def render_analyze():
    st.header("📊 策略分析")
    st.caption("上传 .drl / .java 策略文件 → 自动解析 + 校验 + LLM 业务叙述")

    uploaded = st.file_uploader("选择策略文件", type=["java", "drl"])
    use_llm = st.checkbox("启用 LLM 业务叙述（调用 DeepSeek API）", value=True)
    use_flow = st.checkbox("🎨 生成 HTML 流程图（确定性渲染时间线，不调 LLM）", value=True)

    if uploaded and st.button("🔍 开始分析", type="primary"):
        from parser import parse_rule_file
        from checker import check_file
        from report import generate_report

        # 写入临时文件（放系统 temp：避免污染仓库目录，兼容 Cloud 只读目录）
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"_tmp_{uploaded.name}"
        tmp.write_bytes(uploaded.getvalue())

        try:
            with st.spinner("解析中..."):
                rules = parse_rule_file(tmp)

            with st.spinner("校验中..."):
                results = check_file(rules)

            # 统计
            total_err = sum(sum(1 for i in cr.issues if i.severity == "ERROR") for cr in results)
            total_wrn = sum(sum(1 for i in cr.issues if i.severity == "WARN") for cr in results)
            total_inf = sum(sum(1 for i in cr.issues if i.severity == "INFO") for cr in results)

            # 指标卡片
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("规则数", len(rules))
            col2.metric("ERROR", total_err, delta=None if total_err == 0 else f"{total_err} 个严重问题")
            col3.metric("WARN", total_wrn)
            col4.metric("INFO", total_inf)

            # LLM 交叉校验（先跑：corrections 需在渲染报告前合并进元数据）
            cv_result = None
            if use_llm:
                with st.spinner("🔍 LLM 交叉校验 parser 结果..."):
                    from cross_validate import cross_validate, build_parser_summary, apply_corrections
                    source_code = tmp.read_text(encoding="utf-8")
                    parser_summary = build_parser_summary(rules)
                    cv_result = cross_validate(source_code, parser_summary)
                if cv_result.corrections:
                    n_fix = apply_corrections(rules, cv_result.corrections)
                    if n_fix:
                        st.caption(f"🛠 已应用 LLM 结构化纠错（{n_fix} 条规则），报告/流程图按修正后数据渲染")

            # 生成报告（含纠错后的分组口径/语义出口）
            md_content, json_content = generate_report(rules, results)

            # LLM 叙述 + 交叉校验展示
            if use_llm:
                with st.spinner("🤖 生成 LLM 业务叙述..."):
                    from llm import generate_narrative
                    issues_lines = []
                    for cr in results:
                        for iss in cr.issues:
                            issues_lines.append(f"- {iss.severity} [{iss.rule_id}]: {iss.message}")
                    issues_text = "\n".join(issues_lines) if issues_lines else "无问题"
                    narrative = generate_narrative(json_content, issues_text)
                    st.markdown("## 业务总结")
                    st.markdown(narrative)
                    st.divider()

                if cv_result and cv_result.findings:
                    warn_findings = [f for f in cv_result.findings if f.severity == "WARN"]
                    info_findings = [f for f in cv_result.findings if f.severity == "INFO"]
                    if warn_findings:
                        st.warning(f"⚠️ 交叉校验发现 {len(warn_findings)} 项 Parser 遗漏")
                        for f in warn_findings:
                            st.markdown(f"- **[{f.category}]** {f.description}")
                    if info_findings:
                        st.info(f"💡 补充发现 {len(info_findings)} 项")
                        for f in info_findings:
                            st.markdown(f"- [{f.category}] {f.description}")
                elif cv_result:
                    st.success("✅ 交叉校验通过：Parser 提取结果与源码一致")
                st.divider()

            # 详细报告
            with st.expander("📋 详细分析报告", expanded=not use_llm):
                st.markdown(md_content)

            # 🎨 HTML 流程图（确定性渲染 + LLM 兜底动态延迟）
            if use_flow:
                from flow_html import render_flow_html, compute_timeline, split_groups
                import streamlit.components.v1 as components

                st.markdown("## 🎨 策略流程图")
                st.caption("时间线由累计延迟精确计算（parser → flow_html 确定性渲染）；动态延迟先注释/函数表解析，解析不出再由 LLM 兜底")

                # 1) 先跑一遍找出确定性无法解析的轮
                uncertain_rounds = []
                for r in rules:
                    for gname, gb in split_groups(r):
                        for rt in compute_timeline(gb):
                            if rt.kind == "uncertain":
                                uncertain_rounds.append({
                                    "group": gname, "round_no": rt.round_no,
                                    "call_times": rt.call_times,
                                    "delay_exprs": rt.delay_exprs,
                                    "comment": rt.comment,
                                })
                # 2) 若有且启用 LLM → LLM 兜底解析
                time_overrides = {}
                if use_llm and uncertain_rounds:
                    with st.spinner(f"🤖 LLM 兜底解析 {len(uncertain_rounds)} 个动态延迟轮..."):
                        from cross_validate import llm_resolve_times
                        source_code = tmp.read_text(encoding="utf-8")
                        time_overrides = llm_resolve_times(source_code, uncertain_rounds)
                    if time_overrides:
                        st.caption(f"🤖 LLM 兜底解析了 {len(time_overrides)} 个动态延迟轮（紫色标签）")

                # 文件名用 rule 名（不用 _tmp_xxx）
                base_stem = Path(uploaded.name).stem
                flow_html_str = render_flow_html(rules[0], uploaded.name, time_overrides) if len(rules) == 1 else ""
                if len(rules) > 1:
                    # 多 rule：逐条渲染，用 tab 展示
                    flow_tabs = st.tabs([r.name for r in rules])
                    flow_htmls = []
                    for r, tab in zip(rules, flow_tabs):
                        h = render_flow_html(r, uploaded.name, time_overrides)
                        flow_htmls.append((r.name, h))
                        with tab:
                            components.html(h, height=650, scrolling=True)
                    # 下载：多 rule 分别下载
                    for rname, h in flow_htmls:
                        st.download_button(
                            f"📥 下载 {rname}_flow.html",
                            h,
                            file_name=f"{rname}_flow.html",
                            mime="text/html",
                            key=f"flow_dl_{rname}",
                        )
                else:
                    # 单 rule：直接预览 + 下载
                    components.html(flow_html_str, height=650, scrolling=True)
                    st.download_button(
                        f"📥 下载 {base_stem}_flow.html",
                        flow_html_str,
                        file_name=f"{base_stem}_flow.html",
                        mime="text/html",
                    )

            # 下载
            st.download_button("📥 下载 Markdown 报告", md_content, "report.md", "text/markdown")

        finally:
            if tmp.exists():
                tmp.unlink()


# ---------------------------------------------------------------------------
# 生成模式（聊天界面）
# ---------------------------------------------------------------------------

def render_generate():
    st.header("✏️ 策略生成")
    st.caption("描述你的策略需求，Agent 会追问细节、生成代码、自动校验")

    # 初始化 session state
    if "conv" not in st.session_state:
        st.session_state.conv = None
        st.session_state.chat_messages = []
        st.session_state.started = False

    # ---- 新对话入口 ----
    if not st.session_state.started:
        with st.form("new_conv_form"):
            requirement = st.text_area(
                "描述你的策略需求",
                placeholder="例如：帮我写一个3轮SMS提醒策略，categoryCode 是 NewUser_SMS_Reminder，模板 TEMPLATE_001",
                height=100,
            )
            ref_file = st.file_uploader(
                "📎 参考策略文件（可选）",
                type=["java", "drl"],
                help="上传已有策略作为参考，Agent 会分析其结构并以此为模板生成新策略",
            )
            submitted = st.form_submit_button("🚀 开始生成", type="primary")

            if submitted and requirement.strip():
                from agent import Conversation

                # 如果有参考文件，保存到临时目录并拼接到需求中
                final_requirement = requirement.strip()
                if ref_file:
                    ref_dir = AGENT_DIR / "_ref_uploads"
                    ref_dir.mkdir(exist_ok=True)
                    ref_path = ref_dir / ref_file.name
                    ref_path.write_bytes(ref_file.getvalue())
                    final_requirement += f"\n\n[参考策略文件已上传，路径: {ref_path}]"

                conv = Conversation()
                conv.start(final_requirement)
                st.session_state.conv = conv
                st.session_state.chat_messages = [
                    {"role": "user", "content": requirement.strip()},
                ]
                st.session_state.started = True
                # 记录对话前已有文件（用于检测新生成的文件）
                if GENERATED_DIR.exists():
                    st.session_state.existing_files = {
                        f.name for f in GENERATED_DIR.glob("*.java")
                    } | {f.name for f in GENERATED_DIR.glob("*.drl")}
                else:
                    st.session_state.existing_files = set()
                st.rerun()

        # 显示已有文件
        if GENERATED_DIR.exists():
            files = list(GENERATED_DIR.glob("*.java")) + list(GENERATED_DIR.glob("*.drl"))
            if files:
                st.markdown("---")
                st.caption(f"📁 已生成文件 ({len(files)} 个)")
                for f in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
                    size = f.stat().st_size
                    st.code(f.read_text(encoding="utf-8")[:200] + "...", language="java")
                    st.caption(f"📄 {f.name} ({size} 字符)")

        return

    # ---- 进行中的对话 ----
    conv = st.session_state.conv

    # 自动执行 Agent 步骤（直到需要用户输入或完成）
    if conv.status == "running":
        with st.spinner("🤖 Agent 思考中..."):
            while conv.status == "running":
                events = conv.step()
                # 把事件转为聊天消息
                for ev in events:
                    if ev["type"] == "thinking":
                        pass  # thinking 不单独显示
                    elif ev["type"] == "tool_call":
                        detail = f" ({ev['detail']})" if ev["detail"] else ""
                        st.session_state.chat_messages.append({
                            "role": "assistant",
                            "content": f"🔧 调用工具: **{ev['name']}**{detail}",
                            "is_tool": True,
                        })
                    elif ev["type"] == "tool_result":
                        # 检测 save_strategy 结果 → 提取代码用于下载
                        if ev["name"] == "save_strategy" and ev["result"].startswith("✅"):
                            # 从结果中提取文件路径
                            import re as _re
                            path_m = _re.search(r'已保存到: (.+)', ev["result"])
                            if path_m:
                                saved_path = Path(path_m.group(1).strip())
                                if saved_path.exists():
                                    code_content = saved_path.read_text(encoding="utf-8")
                                    st.session_state.chat_messages.append({
                                        "role": "assistant",
                                        "content": f"💾 已保存: **{saved_path.name}**",
                                        "is_tool": True,
                                    })
                                    st.session_state.chat_messages.append({
                                        "role": "assistant",
                                        "content": code_content,
                                        "is_code": True,
                                        "filename": saved_path.name,
                                    })
                                    continue  # 跳过普通 tool_result 显示
                        preview = ev["result"][:500]
                        if len(ev["result"]) > 500:
                            preview += "..."
                        st.session_state.chat_messages.append({
                            "role": "assistant",
                            "content": f"📋 {preview}",
                            "is_tool": True,
                        })
                    elif ev["type"] == "auto_save":
                        auto_path = Path(ev["path"])
                        st.session_state.chat_messages.append({
                            "role": "assistant",
                            "content": f"💾 自动保存到: `{ev['path']}`",
                            "is_tool": True,
                        })
                        # 自动保存也提供下载
                        if auto_path.exists():
                            code_content = auto_path.read_text(encoding="utf-8")
                            st.session_state.chat_messages.append({
                                "role": "assistant",
                                "content": code_content,
                                "is_code": True,
                                "filename": auto_path.name,
                            })
                    elif ev["type"] == "error":
                        st.session_state.chat_messages.append({
                            "role": "assistant",
                            "content": f"❌ {ev['message']}",
                            "is_tool": True,
                        })

                # Agent 输出文本 → 需要用户输入
                if conv.status == "needs_input":
                    st.session_state.chat_messages.append({
                        "role": "assistant",
                        "content": conv.last_content,
                    })

    # 渲染聊天消息
    for i, msg in enumerate(st.session_state.chat_messages):
        with st.chat_message(msg["role"]):
            if msg.get("is_code"):
                # 代码块 + 下载按钮
                fname = msg.get("filename", "strategy.java")
                with st.expander(f"📄 {fname}", expanded=True):
                    st.code(msg["content"], language="java")
                    st.download_button(
                        f"📥 下载 {fname}",
                        msg["content"],
                        file_name=fname,
                        mime="text/plain",
                        key=f"dl_{i}",
                    )
            elif msg.get("is_tool"):
                st.caption(msg["content"])
            else:
                st.markdown(msg["content"])

    # 用户输入
    if conv.status == "needs_input":
        if reply := st.chat_input("回复 Agent（输入修改意见或追问，留空结束对话）"):
            conv.add_reply(reply)
            st.session_state.chat_messages.append({"role": "user", "content": reply})
            st.rerun()
        else:
            # 检查是否有空提交（结束对话）
            pass

    if conv.status in ("done", "error") and st.session_state.started:
        st.info("对话已结束")
        if st.button("🔄 开始新对话"):
            st.session_state.conv = None
            st.session_state.chat_messages = []
            st.session_state.started = False
            st.rerun()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

if "分析" in mode:
    render_analyze()
else:
    render_generate()
