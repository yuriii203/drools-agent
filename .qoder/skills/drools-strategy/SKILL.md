---

name: drools-strategy  
description: 生成与审查墨西哥营销触达 Drools 策略规则（SMS/WABA/Push/邮件/发券、callTimes 多轮延迟触达编排、灰度AB分流、decisionTag打标）。当用户要求编写新营销策略、检查或修改 .drl/rule 代码、询问 ActionFunction 函数（buildWaba/buildSms/buildPush/buildMail/buildCoupon/appendDelayReach）用法、或提到轮次、灰度、对照组、发券、延迟决策等关键词时使用。使用指南：调用前备齐必填五项（①人群与 categoryCode（圈人入口无需提供，Drools 只管圈好的人群）②触达渠道与轮次时间线 ③分组口径与分流比例/灰度桶名（分流比例不指定默认 9:1；9:1；5:5）④模型分/资格变量的类型与缺失值约定 ⑤模板编码与券批次号），可选项如 DW 传参（attachment 字段映射、手机号取值 key 等）有则提供；rule 名用 categoryCode（或拼接字母数字后缀），不许中文。注意 ActionFunction 函数调用必须带 `ActionFunction.` 前缀。详见文内「使用指南」章节。

---

# Drools 营销策略开发助手

## 使用指南（调用方备料清单）

新写一条策略前，先按下面模板把**必填项** list 清楚，再调用本 skill 即可写出需要的 Drools 规则；必填项缺失时 skill 会主动追问（见工作流A），可选项不提供则按默认处理。

**备料模板**（可直接复制填写）：

```
【必填】
1. 人群与 categoryCode：
2. 触达渠道与轮次时间线（每轮发什么、几点发、共几轮）：
3. 分组口径与分流比例/灰度桶名：
4. 模型分/资格变量（类型、缺失值约定）：无则写"无"
5. 模板编码、券批次号：
【可选】
6. DW 传参（attachment 字段映射，如手机号/邮箱取哪个 key）：
7. rule 名（不填默认 = categoryCode）：
```

### 必填项（5 项）

1.  **人群与 categoryCode**：只需给出 categoryCode 本身（必须与圈人侧配置完全一致）。圈人入口**不需要关心、也不用追问**——Drools 只负责决策，到达规则的人群一定已经圈好
2.  **触达渠道与轮次时间线**：每轮发什么、几点发、共几轮（相邻轮次建议轮换渠道，避免同一方式反复刺激用户）
3.  **分组口径与分流**：ABCD 或 95/99 折等分组口径、分流比例、灰度桶名（桶名按三级泳道表查，不许自创）
4.  **模型分/资格变量**：类型、缺失值约定、量纲（缺失哨兵值绝不能落入发券分组）；无则明确写"无"
5.  **模板编码、券批次号**：标注"待审批"的要在上线前提醒人工确认（券有效期在 PMS 批次配置，Drools 不管）

### 可选项（有则提供，无则按默认）

1.  **DW 传参（attachment 字段映射）**：圈人侧塞进 attachment 的变量及其 key。典型例子：\*\*WABA 和短信所用的手机号取自 `$request.getAttachment().get("mobile")`\*\*（mobile 即圈人侧映射的手机号 key）；不指定时手机号默认取 `$request.getUserInfo().getMobile()`
2.  **rule 名**：不提供时默认直接用 categoryCode；需要区分同 categoryCode 多条规则时可拼接字母数字后缀（如 `demo_recall_v2`），命名限制见硬性规则 12

## 背景事实

*   人群经 PMS 汇入 Drools 决策，决策产生的指令由 PMS 执行。**规则编写不关心人群来源**，只需 categoryCode 精确匹配，到达规则的人群一定已经圈好
*   attachment 是圈人侧传入的变量 Map；规则内统一取法 `$request.getAttachment().get(key)`
*   规则绑定变量是 `$request`/`$response`，规则体内如需短名要显式 `RequestDto request = $request;`
*   `appendDelayReach` 单位是**秒**，参数类型是 \*\*`Integer`\*\*（传 `long` 会编译失败）；延迟到期后 PMS 自动再次决策且 `callTimes` +1
*   `callTimes` 是链路内计数器：本轮挂了延迟才有下一轮；跨批次会重置，不能用于跨批次频控（频控必须在圈人侧做滚动窗口去重）
*   `setDecision` 语义（上游确认）：**1=可营销、有指令；9=暂不营销、无指令**（defaultRule 初始 9）；调 buildXxx 的本质就是往 response.campaigns 创建指令，由 PMS 消费执行（指令清单见 reference.md）
*   函数签名、参数顺序、延迟公式速查表见 [reference.md](reference.md)

## 轮次契约（多轮编排的核心）

| 本轮行为 | 后果 |
| --- | --- |
| 挂 `appendDelayReach` | callTimes+1，到点再回来 |
| 不挂延迟直接 return | 链路终止 |

*   最后一轮**不挂延迟**即自然结束；必须配 `callTimes > N` 兜底分支，按标准稳定退出模式收尾：`setDecisionTag(_超出轮次)` + `setResultObject(2)` + `return`（完整代码范式见 reference.md「标准稳定退出模式」）
*   T0 = 策略核心动作发生日（通常是发券日），不是第1轮；第1轮往往只是延迟等待轮\*\*(待确认)\*\*

## 规则骨架模板

```java
rule "策略名"
no-loop true
lock-on-active true
salience 9999
when
    $request: RequestDto($request.getCategoryCode().equals("策略名"))
    $response: ResponseDto()
then
    System.out.println("规则触发: " + drools.getRule().getName());

    $response.setDecision(1);                    // 可营销标记，入口即打
    String decisionTag = "策略名";
    $response.setDecisionTag(decisionTag);       // 兜底tag，后续覆盖式更新

    // 模板/批次号配置区
    // callTimes 判空（Integer包装类型可为null）
    Integer callTimes = $request.getCallTimes();
    if (callTimes == null) { callTimes = 1; }

    // 当前时刻（分钟制）—— 必须用 UtilsFunction，禁止 java.util.Calendar / java.time
    int cur = UtilsFunction.getCurrentHour() * 60 + UtilsFunction.getCurrentMinute();

    // 资格判断（黑名单/手机号/模型分/戳额/发标）——每个出口必须打标 return
    // 灰度分流——未命中打 _not_in_xxx_gray + setResultObject(1) return
    // 轮次分支：callTimes == 1/2/3...（触达/延迟一律 ActionFunction.buildXxx / ActionFunction.appendDelayReach）
    //   延迟轮次顺序强制：先 ActionFunction.appendDelayReach(...)、再 $response.setDecisionTag(decisionTag + "_已延时到XX")（先挂延时后打标）
    // 兜底：callTimes > N → _超出轮次 + setResultObject(2) + return（标准稳定退出）
end
```

## 灰度三级泳道（桶名规定）

桶名不许自创，按本文档规定泳道表查（全表见 reference.md）：

*   一级全局：`overall_new_user`（新客全局）
*   二级节点：如 `register_unstamped`（注册未戳额）、`grant_success_nobid`（给额未发标）、`credit_expiration_recall`（额度过期召回）、`unpass`（戳额未过件）
*   三级工具：节点名 + 后缀 `_telemarketing`（电销）/ `_com`（SMS/AI外呼/IVR/WABA）/ `_coupon`（优惠券）/ `_push`；活跃节点无工具后缀，三级桶名直接是 `active_user`（某次版本调整起并入新客全局一级）
*   注意：活跃并入一级后，**活跃用户也会先过一级全局 9:1（一级就切 10% 对照组）**，再走 `active_user` 分流；写活跃类策略时一级检查不能漏

检查顺序一级→二级→三级（三个 get\_hashed\_isin 依次判断，任一级未命中即走对照组出口）。**同名泳道 = 同一分流，跨策略复用是设计本意**（保证同节点各工具的实验/对照组人群一致），不要自创新桶名。

**默认分流比例**：用户未指定时，一级、二级、三级依次为 **9:1、9:1、5:5**，即 `get_hashed_isin(桶名, userId+"", 0, 0.9)`、`get_hashed_isin(桶名, userId+"", 0, 0.9)`、`get_hashed_isin(桶名, userId+"", 0, 0.5)`（**区间 0\~1 浮点数，左闭右开，绝不是 0\~100 整数**）；只有用户明确要求其他比例时才调整。

## 硬性规则（生成和审查都遵守）

1.  **打标规范**：入口打基础tag；每个 return 出口前必须 `setDecisionTag(细分tag)`；对照组 `setResultObject(1)`、不合格排除/超出轮次等异常退出 `setResultObject(2)`（本文档惯例）；**buildXxx 函数内部已自动 setDecisionTag + setDecision(1)，调用后不要再用同 tag 手动重复 setDecisionTag**
    *   **buildXxx 的最后一个参数（decisionTag）必须用 `decisionTag` 变量拼接**：规则开头声明 `String decisionTag = "策略名"`，调用时传 `decisionTag + "_描述后缀"`（如 `ActionFunction.buildSms(..., decisionTag + "_实验组_发送短信")`），**绝不允许传纯硬编码字符串**（如 `"demo_recall_sms"`）——buildXxx 内部会用这个参数调 `setDecisionTag`，硬编码会导致落库 tag 无法跨策略区分
    *   **延迟轮次先挂延时、再打标（顺序强制）**：调用顺序必须是 `ActionFunction.appendDelayReach(...)` 在前、`$response.setDecisionTag(...)` 在后。因为 appendDelayReach 内部有 `if (delayTimes != null && delayTimes > 0)` 守卫，延迟值算错（≤0 或 null）时延时不会挂上；若先打 `_已延时到930` 标再挂延时，一旦延时没挂上，落库 tag 就谎报“已延时”（反面案例：demo_recall 第1/2轮 setDecisionTag 写在 appendDelayReach 之前）。手动 tag 同样用 `decisionTag + "_已延时到930"` 变量拼接，不写硬编码
2.  **空值语义**：`Integer/Long/String` 包装类型必须判 null；`int/float` 基本类型不可能为 null，缺失靠哨兵值（如 -1）或默认值 0 识别——**模型分缺失值 0/-1 绝不能落入发券分组**
3.  **分流顺序**：先资格判断（戳额/发标/模型分/手机号）再灰度分流，**保证实验组与对照组同为合格人群**
4.  **分流时点对齐**：对照组打标必须与处理动作（实验组的action）同一轮发生，不能提前一天打标
5.  **延迟公式**（cur = 时×60+分）：
    *   固定次日 HH:MM：`(目标分钟 + 1440 - cur) * 60`
    *   最近的某时刻：`cur < 目标分钟 ? (目标分钟 - cur) * 60 : (目标分钟 + 1440 - cur) * 60`
    *   分钟项永远是**减号**，写成加号会偏差 2×currentMinute
6.  **参数顺序**：只有 buildWaba 是"模板在前、手机在后"，buildSms/buildPush 都是手机在前（详见 reference.md）
7.  **categoryCode** 必须与圈人侧配置完全一致；同 package 内 rule 重名会编译失败，上线新规则先确认是替换还是新增
8.  发券（buildCoupon）应搭配触达通知（WABA/SMS/Push）——业务约定（否则用户不知道有券），技术上可单独发；券有效期在优惠券平台上配置，Drools 不管
9.  **戳额 ≠ 发标，严禁混用 getter**：\*\*戳额（额度申请）用 `getLastLimitApplyInfo()`；发标（借款/进件）必须用 `getLastLoanListInfo()`，完整判断范式见 reference.md「严禁混用戳额与发标 getter」，混用会导致中途转化停触达完全失效（反面案例：给额未发标策略误用戳额判断发标状态）
10.  **标准稳定退出模式（强制）**：轮次结束或异常分支必须按此三件套收尾，防止规则引擎循环或状态不一致：所有非正常流程出口（资格不符、字段缺失、超出轮次）都先打明确 `decisionTag`、再 `setResultObject(2)`、最后直接 `return`（完整代码范式见 reference.md「标准稳定退出模式」）
11.  **函数调用必须带类名前缀（编译级强制）**：`buildXxx`/`appendDelayReach` 等是 `ActionFunction` 的静态方法，drl 只 import 了类、没有静态函数导入，裸调用（如 `buildPush(...)`）会被当成规则类自身方法，报 "The method buildPush(...) is undefined for the type Rule\_xxx" 编译错误——必须写 `ActionFunction.buildPush(...)`、`ActionFunction.appendDelayReach(...)`（UtilsFunction 同理）。且 `appendDelayReach` 延迟参数是 `**Integer**`**（秒）**：延迟变量必须声明为 `int`，传 `long` 无法装箱为 Integer 同样编译失败
12.  **rule 命名规范（不许中文）**：rule 名一律用**英文/数字/下划线**，默认直接取 categoryCode，需要区分时可用 categoryCode 拼接字母数字后缀（如 `demo_recall`、`demo_recall_v2`）；**禁止中文 rule 名**（编译/检索/归因均不友好）
13.  **时间计算必须用 UtilsFunction（禁止 raw Java API）**：当前时刻统一写 `int cur = UtilsFunction.getCurrentHour() * 60 + UtilsFunction.getCurrentMinute();`，**禁止使用 `java.util.Calendar`、`java.time` 等原生 API**——UtilsFunction 是平台统一封装，所有延迟公式都基于它；用 raw API 会导致风格不统一且容易出错
14.  **多实验组时对照组标签必须携带分组标识**：策略含多个实验组（如 95折/99折、A/B/C 组）时，对照组 decisionTag 必须拼上所属分组名，确保回收 SQL 能按组精确匹配「某组实验组 vs 同组对照组」。做法：将三级灰度 `!hasHit` 检查**下沉到实验动作所在轮次的分组 if 分支内**（而非统一前置拦截），对照组标签追加组标识（如 `_not_in_xxx_gray_95coupon` / `_not_in_xxx_gray_99coupon`）。一二级灰度（全局/节点层）的排除仍统一前置处理、不带组标识（这些人与实验组不可比）。完整代码范式见 reference.md「多实验组对照组打标范式」

## 工作流A：生成新策略

按顺序确认，缺什么问什么：

1.  人群与 categoryCode（**不需要追问圈人入口**——Drools 只管决策，到达规则的人群一定已圈好；仅需确认 categoryCode 拼写与圈人侧一致）
2.  触达渠道与轮次时间线（每轮发什么、几点发、共几轮）
3.  分组口径（ABCD 或 95/99 折等）与分流比例、灰度桶名（**分流比例用户未指定时默认 9:1、9:1、5:5，不必追问**；桶名按三级泳道表查）
4.  模型分/资格变量的类型、缺失值约定、量纲
5.  模板编码、券批次号（标注"待审批"的要提醒上线前确认）
6.  可选项确认：DW 传参（attachment 字段映射，如手机号取哪个 key——例：mobile）与 rule 名（不指定则默认 = categoryCode）；均不追问，用户未提供按默认处理（attachment 缺失默认 `getUserInfo().getMobile()`，rule 名默认 categoryCode）
7.  按骨架模板生成代码，套用上节全部硬性规则（rule 名遵守规则 12：不许中文）
8.  生成后自查：对照工作流B清单过一遍再交付

## 工作流B：审查已有规则

按此清单逐项检查，输出结论分三档：**编译错误** / **逻辑漏洞** / **业务配置提醒**：

*   `$request`/`$response` 绑定变量名正确，无裸 `request`/`response`（除非显式声明别名）
*   函数名大小写（`buildPush` 不是 `buildpush`）、参数顺序（尤其 buildWaba）、**调用必须带** `**ActionFunction.**` **前缀**（裸调用直接编译失败）；`appendDelayReach` 延迟秒参数必须是 `int/Integer`（传 `long` 编译失败）
*   基本类型与小数比较（int vs 0.125 永远不成立）、缺失值（0/-1）是否混入分组
*   每个 return 出口有 tag；对照组 setResultObject(1)、不合格排除/超出轮次等异常退出 setResultObject(2)
*   轮次链完整：每轮该挂延迟的挂了，最后一轮不挂；有超出轮次兜底且兜底走标准稳定退出（tag + setResultObject(2) + return）
*   资格判断在分流之前；分流对所有人同一轮评估（命中=实验组发券、未命中=对照组打标），不得提前一轮打对照组标
*   多实验组场景：对照组标签是否携带分组标识（如 `_gray_95coupon` / `_gray_99coupon`），一二级灰度排除是否前置且不带组标识，确保回收 SQL 能按组精确 AB 对比
*   延迟公式符号与单位（秒、1440）正确；延迟语义（当日 vs 次日）与业务一致
*   中途转化停触达：戳额/发标/进件检查放在轮次分支之上（每轮都执行）；**戳额用 getLastLimitApplyInfo()、发标用 getLastLoanListInfo()，按场景选对，严禁混用**
*   categoryCode 与圈人侧配置一致；rule 名在 drl 文件内唯一，且**不含中文**（categoryCode 或拼接字母数字后缀）
*   DW 传参取值：attachment 的 key 与圈人侧映射一致（如手机号 `mobile`），且做了 attachment 判空 + 空值校验
*   模板/券批次号、文案与折扣档位是否匹配（drools 层查不出，见下人工审核项）

> 人工审核项（drools 层无法校验，上线前提醒用户人工确认）：模板/券批次审批状态、文案与折扣档位匹配（别抄错别家的文案）。

## 高频坑速查

| 坑 | 后果 | 正解 |
| --- | --- | --- |
| int 型模型分与 0.125 比较 | 分组永远不成立，缺失值0反而进低分组 | 确认量纲后用整数阈值，或改 float + 哨兵值 |
| 延迟公式分钟项写成加号 | 触达时间晚 2×currentMinute | 永远减 currentMinute |
| 最后一轮仍挂 appendDelayReach | 多一轮空决策 | 末轮只发触达不挂延迟 |
| 延迟轮次先 setDecisionTag 再 appendDelayReach | appendDelayReach 内部有 delayTimes>0 守卫，延迟算错没挂上时 tag 谎报“已延时” | 先 appendDelayReach 后 setDecisionTag（先挂延时再打标） |
| 首日就打对照组标 | 对照组混入自然转化用户，增量失真 | 分流对齐发券时点，先戳额检查再分流 |
| 注册分第1轮就取 | 模型未出分必为缺失值 | 第1轮纯延迟，第2轮再取分 |
| 灰度桶名自创 | 不符合本文档泳道规定，分流对不齐 | 按三级泳道表查桶名，复用同名泳道 |
| 戳额/发标 getter 混用（getLastLimitApplyInfo 与 getLastLoanListInfo） | 中途转化停触达失效，已转化用户被反复触达 | 戳额用 getLastLimitApplyInfo()；发标必须用 getLastLoanListInfo().getLoanListApplyTime() |
| 异常/超轮次出口裸 return 不打标 | 落库无法归因，规则状态不一致 | 标准稳定退出：decisionTag + setResultObject(2) + return |
| 裸调用 buildPush/appendDelayReach（无类名前缀） | 编译错误 "The method xxx is undefined for the type Rule\_xxx" | 一律 `ActionFunction.buildPush(...)`、`ActionFunction.appendDelayReach(...)` |
| appendDelayReach 传 long 延迟 | 编译失败（参数是 Integer，long 无法自动装箱） | 延迟变量声明为 `int`（最大约 86400 秒，int 足够） |
| rule 名用中文 | 编译/检索/落库归因不友好，本文档规范禁止 | rule 名 = categoryCode，或 categoryCode 拼接字母数字后缀（如 `demo_recall_v2`） |
| get_hashed_isin 参数写成 0\~100 整数（如 0, 90） | 区间 0\~1 浮点数，传 90 相当于传 1.0（100%），分流完全错误 | 分流比例用 0\~1 浮点数：90% = 0.9、50% = 0.5、10% = 0.1 |
| buildXxx 的 decisionTag 传纯硬编码字符串（如 `"demo_recall_sms"`） | 落库归因无法区分不同策略，多个策略的 tag 可能重名 | 规则开头声明 `String decisionTag`，调 buildXxx 时传 `decisionTag + "_描述后缀"` |
| 用 `java.util.Calendar` / `java.time` 取当前时间 | 与平台封装不一致，风格混乱且易出错 | 统一 `UtilsFunction.getCurrentHour() * 60 + UtilsFunction.getCurrentMinute()` |
| 多实验组对照组打统一标签（不分组） | 回收时无法区分「95折对照组」与「99折对照组」，AB 对比数据污染 | 三级灰度 `!hasHit` 检查下沉到分组分支内，对照组 tag 拼组标识（如 `_gray_95coupon` / `_gray_99coupon`） |