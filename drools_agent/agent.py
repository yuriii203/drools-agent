"""
agent.py — Drools 策略生成 Agent

做什么：
  Conversation 类：可步进式 Agent 对话（CLI 和 Streamlit 共用）
  agent_loop()：CLI 兼容入口（内部用 Conversation + print/input）

为什么这样设计（与 prompt stuffing 的区别）：
  - LLM 主动决定调什么工具、什么时候调（不是预先全塞 prompt）
  - SKILL.md 7000 字预加载（小，总是需要）
  - reference.md 按需读（LLM 在需要查参数时才调 read_reference）
  - checker 闭环：生成 → 校验 → 修复 → 再校验（最多 3 次）
  - Conversation.step() 可以被 Streamlit 逐步调用，不阻塞
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from llm import chat_with_tools
from tools import TOOL_SCHEMAS, TOOL_FUNCTIONS, call_tool, read_skill

# ---------------------------------------------------------------------------
# Agent 系统 prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是 Drools 策略生成 Agent。你的任务是根据用户需求，生成符合规范的墨西哥营销触达 Drools 策略代码（.java/.drl 格式）。

你拥有以下工具：
- read_skill:           读取策略开发规范（硬性规则、骨架模板）
- read_reference:       读取函数速查表（buildXxx 参数顺序、延迟公式、泳道表）
- run_checker:          校验生成的代码是否有编译级或逻辑问题
- save_strategy:        保存最终代码到文件
- read_generated_file:  读取已保存的策略文件（用于修改场景）
- analyze_existing:     分析已有策略文件，返回结构化摘要（读写协同）

## 工作流程

1. **理解需求**
   - 分析用户给出的 5 类信息：①人群/categoryCode ②触达渠道与轮次 ③分流/灰度桶 ④模型分/资格变量 ⑤模板编码/券批次号
   - 如果关键信息缺失（categoryCode、渠道、轮次），主动向用户追问
   - 分流比例未指定时默认 9:1、9:1、5:5

2. **查阅规范**
   - 必须先调 read_skill 了解硬性规则
   - 生成代码时调 read_reference 查参数顺序和延迟公式

3. **生成代码**
   - 严格按 read_skill 中的规则骨架模板生成
   - 所有 buildXxx/appendDelayReach 必须带 ActionFunction. 前缀
   - 每个 return 出口前必须 setDecisionTag
   - 末轮不挂延迟，必须有 callTimes > N 兜底分支（标准稳定退出）

4. **校验修复**
   - 生成后调 run_checker 自动校验
   - 有 ERROR 必须修复后重新校验
   - 最多重试 3 次

5. **保存交付**
   - 校验通过后调 save_strategy 保存文件
   - 输出代码要点说明（用中文，不要代码）

## 读写协同（参考已有策略生成新策略）

当用户提供了参考文件（上传的策略文件、或指定了已有策略名）：
1. 先调 analyze_existing 分析参考策略的结构
2. 理解参考策略的分组、轮次、渠道、延迟设计
3. 根据用户需求，以参考策略为模板生成新策略
4. 保持相似结构，只修改用户指定的部分（如换渠道、改轮次、调整延迟）

典型场景：
- "参考这个push策略，帮我写一个邮件版" → analyze → 改渠道为 buildMail
- "这个策略39轮太多了，精简到7轮" → analyze → 缩减轮次
- "复制这个策略的分组逻辑，写个新场景" → analyze → 复用泳道结构

## 修改已有策略

当用户要求修改已生成的策略时：
1. 先调 read_generated_file 读取当前代码
2. 根据用户要求修改代码
3. 调 run_checker 校验修改后的代码
4. 调 save_strategy 保存（用相同文件名覆盖）
5. 输出修改要点说明

## 约束

- 如果信息不足，停下来追问，不要猜测编造
- 代码必须完整可运行，不留 TODO 或占位符
- 回复用中文，代码用英文注释
- **绝对不要在回复文本中输出完整代码**。代码只通过 save_strategy 工具保存到文件，回复中只写要点说明
- **必须调用 save_strategy 保存文件**，不要只是把代码贴在对话里
"""

# ---------------------------------------------------------------------------
# Conversation 类（核心，CLI 和 Streamlit 共用）
# ---------------------------------------------------------------------------

class Conversation:
    """
    可步进式 Agent 对话。

    用法：
        conv = Conversation()
        conv.start("帮我写一个3轮SMS策略")

        while not conv.is_done():
            events = conv.step()
            # 处理 events（CLI 打印 / Streamlit 渲染）

            if conv.status == "needs_input":
                reply = get_user_input()  # CLI: input() / Streamlit: st.chat_input
                if reply:
                    conv.add_reply(reply)
                else:
                    conv.finish()

    每个 event 是一个 dict：
        {"type": "thinking", "round": int}
        {"type": "tool_call", "name": str, "detail": str}
        {"type": "tool_result", "name": str, "result": str}
        {"type": "auto_save", "path": str}
        {"type": "error", "message": str}
    """

    def __init__(self, output_dir: str | None = None, max_rounds: int = 15):
        self.output_dir = output_dir
        self.max_rounds = max_rounds
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.status: str = "idle"      # idle / running / needs_input / done / error
        self.save_called: bool = False
        self._round: int = 0
        self.last_content: str = ""

    def start(self, requirement: str):
        """初始化对话"""
        skill_content = read_skill()
        self.messages = [
            {
                "role": "system",
                "content": (
                    f"{SYSTEM_PROMPT}\n\n"
                    f"--- BEGIN SKILL DOCUMENT ---\n"
                    f"{skill_content}\n"
                    f"--- END SKILL DOCUMENT ---\n"
                ),
            },
            {
                "role": "user",
                "content": f"请帮我生成一个 Drools 策略：{requirement}",
            },
        ]
        self.status = "running"

    def is_done(self) -> bool:
        return self.status in ("done", "error")

    def step(self) -> list[dict]:
        """
        执行一轮 Agent 处理（LLM 调用 + 工具执行）。
        返回本轮产生的事件列表。
        """
        if self.is_done():
            return []

        self._round += 1
        self.events = [
            {"type": "thinking", "round": self._round},
        ]

        if self._round > self.max_rounds:
            self.status = "error"
            self.events.append({"type": "error", "message": f"达到最大轮数 ({self.max_rounds})"})
            return self.events

        # 调用 LLM
        try:
            msg = chat_with_tools(self.messages, TOOL_SCHEMAS)
        except RuntimeError as e:
            self.status = "error"
            self.events.append({"type": "error", "message": str(e)})
            return self.events

        self.messages.append(msg)

        # 情况 1：工具调用
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                # 构建工具调用描述
                detail = ""
                if func_name == "save_strategy" and "filename" in args:
                    detail = args["filename"]
                elif func_name == "run_checker":
                    detail = f"{len(args.get('code', ''))} 字符"
                elif func_name == "read_generated_file" and "filename" in args:
                    detail = args["filename"]

                self.events.append({
                    "type": "tool_call",
                    "name": func_name,
                    "detail": detail,
                })

                if func_name == "save_strategy":
                    self.save_called = True

                result = call_tool(func_name, args)

                self.events.append({
                    "type": "tool_result",
                    "name": func_name,
                    "result": result,
                })

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            # 工具调用后自动继续下一轮（不暂停）
            return self.events

        # 情况 2：文本回复
        content = msg.get("content", "")
        if content:
            # 自动保存兜底
            if not self.save_called:
                auto_path = self._auto_save(content)
                if auto_path:
                    self.save_called = True
                    self.events.append({"type": "auto_save", "path": str(auto_path)})

            self.last_content = content
            self.status = "needs_input"
            return self.events

        # 异常情况
        self.status = "error"
        self.events.append({"type": "error", "message": "Agent 返回空响应"})
        return self.events

    def add_reply(self, text: str):
        """添加用户回复，继续对话"""
        self.messages.append({"role": "user", "content": text})
        self.status = "running"

    def finish(self):
        """结束对话"""
        self.status = "done"

    def _auto_save(self, content: str) -> Path | None:
        """从文本中提取代码块并自动保存"""
        code_blocks = re.findall(r'```(?:java|drl)?\s*\n(.*?)```', content, re.DOTALL)
        if not code_blocks:
            return None

        code = max(code_blocks, key=len)
        rule_name_m = re.search(r'rule\s+"([^"]+)"', code)
        auto_name = rule_name_m.group(1) if rule_name_m else "generated_strategy"
        auto_name = auto_name.replace(" ", "_")
        if not auto_name.endswith((".java", ".drl")):
            auto_name += ".java"

        out_dir = Path(self.output_dir) if self.output_dir else Path(__file__).parent.parent / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / auto_name
        out_path.write_text(code, encoding="utf-8")
        return out_path


# ---------------------------------------------------------------------------
# CLI 入口（保持原有行为，内部用 Conversation）
# ---------------------------------------------------------------------------

def agent_loop(
    requirement: str,
    output_dir: str | None = None,
    max_rounds: int = 15,
    interactive: bool = True,
) -> str:
    """
    CLI 版 Agent 循环（向后兼容）。
    内部使用 Conversation 类，通过 print/input 交互。
    """
    conv = Conversation(output_dir=output_dir, max_rounds=max_rounds)
    conv.start(requirement)

    while not conv.is_done():
        events = conv.step()

        # 打印事件
        for ev in events:
            if ev["type"] == "thinking":
                print(f"\n{'─'*50}")
                print(f"🤖 Agent 第 {ev['round']} 轮思考...")
                print(f"{'─'*50}")
            elif ev["type"] == "tool_call":
                print(f"  🔧 调用工具: {ev['name']}" + (f"({ev['detail']})" if ev["detail"] else ""))
            elif ev["type"] == "tool_result":
                preview = ev["result"][:300].replace("\n", " ")
                if len(ev["result"]) > 300:
                    preview += "..."
                print(f"  📋 结果: {preview}")
            elif ev["type"] == "auto_save":
                print(f"\n💾 自动保存: 已自动写入 {ev['path']}")
            elif ev["type"] == "error":
                print(f"❌ {ev['message']}")

        # 需要用户输入
        if conv.status == "needs_input":
            print(f"\n🤖 Agent:")
            print(conv.last_content)

            if interactive:
                try:
                    print()
                    user_reply = input("💬 回复 Agent（直接回车=结束对话）: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n\n👋 对话结束")
                    conv.finish()
                    return conv.last_content

                if user_reply:
                    conv.add_reply(user_reply)
                else:
                    print("\n👋 对话结束")
                    conv.finish()
                    return conv.last_content
            else:
                conv.finish()
                return conv.last_content

    return conv.last_content or "Agent 异常停止"


def run_write(requirement: str, output_dir: str | None = None) -> str:
    """写模式入口。被 main.py 调用。"""
    print(f"📝 生成模式启动")
    print(f"   需求: {requirement}")
    print(f"   输出目录: {output_dir or 'generated/'}")
    return agent_loop(requirement, output_dir=output_dir)
