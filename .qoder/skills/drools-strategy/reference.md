# ActionFunction 函数速查

所有方法均为 `com.xxx.marketingxxx.ActionFunction` 静态方法。（由于function会不断更新，后续迭代reference.md也会持续更新）通用规则：

*   **调用必须带类名前缀**：drl 只 import 了 `ActionFunction` 类、没有静态函数导入，裸写 `buildPush(...)` 会报 "The method buildPush(...) is undefined for the type Rule\_xxx" 编译错误——一律写 `ActionFunction.buildPush(...)`、`ActionFunction.appendDelayReach(...)`（`UtilsFunction.xxx` 同理）
*   模板/批次号为空时函数直接 return，不打 tag（静默失败，上线前要确认模板审批状态）
*   \*\*每个 buildXxx 内部已自动 `setDecision(1)` + `setDecisionTag(decisionTag)`\*\*，调用后不要再用同 tag 手动重复 setDecisionTag；连调多个 buildXxx 时最终 tag = 最后一个
*   每次调用实际创建一条指令（MarketingCampaignDto）追加到 `response.getCampaigns()`，指令清单与执行语义见下文「指令（campaigns）是什么」

## 触达类（参数顺序重点）

| 函数 | 参数顺序 |
| --- | --- |
| buildSms | `(response, mobile, templateCode, parameters, decisionTag)` |
| buildWaba | `(response, templateCode, mobile, parameters, decisionTag)` ← **模板在前** |
| buildPush | `(response, mobile, templateCode, parameters, decisionTag)` |
| buildMail | `(response, emailAddress, templateCode, parameters, decisionTag)` |

*   `parameters`：Map\<String,Object\>，给模板文案占位符填值，key 必须与模板占位符名严格一致（无变量时传空 HashMap）
*   mobile 判空建议 `mobile == null || mobile.trim().isEmpty()`
*   **decisionTag 参数必须用变量拼接**：规则开头声明 `String decisionTag = "策略名"`，调 buildXxx 时传 `decisionTag + "_描述后缀"`（如 `decisionTag + "_实验组_发送短信"`、`decisionTag + "_实验组_发送WABA"`），**绝不允许传纯硬编码字符串**（如 `"demo_recall_sms"`）——硬编码无法跨策略区分，落库归因会混乱

## 指令（campaigns）是什么

**指令 = 一条可被 PMS 执行的营销动作记录（**`**MarketingCampaignDto**` **对象）**。Drools 不直接发短信/发券，规则里每调一次 buildXxx/appendDelayReach，就是 new 一个 `MarketingCampaignDto(指令名, 指令ID)`，填好参数后 `response.getCampaigns().add(...)`；决策结束后整个 campaigns 列表随 response 交回 PMS，由 PMS 逐条消费执行。一句话：\*\* Drools 只开任务清单，PMS 负责干活，指令就是清单上的一行\*\*。

每条指令的字段：指令名 + 指令ID（构造函数两参）、`paramInfo`（执行参数：模板/手机号/批次号等）、`delayTime`（指令自身延迟，buildXxx 默认0，仅 appendDelayReach 的再决策指令带延迟）、`orderId`（PMS 执行排序，小者先执行）、`onErrorIsContinue`（失败是否继续执行后续指令）。

完整指令对照表（示例）：

| 函数 | 指令名 | orderId | 失败继续 |
| --- | --- | --- | --- |
| buildWaba | wabaApiReach | **0（最先）** | 否 |
| buildSms / buildSmsByUnRegister | sms\_reach-V1.1 | 1 | y |
| buildPush | push\_reach-V1.1 | 1 | y |
| buildPushByUnRegister | push\_reach\_device | 1 | y |
| buildMail | 发送邮件(采用模板) | 1 | y |
| buildCoupon | grant\_coupon\_offer | 1 | y |
| buildIncreaseAmountOfferByDelay | increaseAmountOffer | 2 | y |
| buildPushTelemarketing | manual\_case\_reach\_action | 1 | y |
| buildAwardCashbackReach | awardCashbackReach | 1 | y |
| buildIvr | IVR 外呼指令 | 15 | y |
| appendDelayReach | 再次决策 | **99（最后）** | — |

要点：同一轮多条指令时 **WABA（orderId 0）先发，触达/发券类（orderId 1）随后，再决策指令（99）永远最后**；除 WABA 外所有指令都带 `onErrorIsContinue=y`（单条失败不阻断后续）。

## 发券类

| 函数 | 参数顺序 |
| --- | --- |
| buildCoupon | `(response, batchNo, count, decisionTag)` |
| buildCouponByDelay | `(response, batchNo, count, decisionTag, delayTimes)` |

*   `batchNo`：PMS/优惠券系统批次号（如 `batch_demo_xxx`）；`count` 发券张数
*   内部会 setDecision(1) 并生成 `grant_coupon_offer` 指令
*   券有效期在 PMS 批次上配置，口径示例："自发放日起后第5天23:59:59失效" = 发放当天算第1天

## 调度与工具

| 函数 | 说明 |
| --- | --- |
| appendDelayReach(response, delaySeconds) | 单位**秒**；\*\*参数类型 `Integer`\*\*（延迟变量必须声明 `int`，传 `long` 无法装箱为 Integer，编译失败）；**内部有 `if (delayTimes != null && delayTimes > 0)` 守卫，延迟值 ≤0 或 null 时静默不挂**；挂上后 PMS 到点再次决策，callTimes+1 |
| UtilsFunction.get\_hashed\_isin(expName, userId+"", start, end) | 按 (实验名,userId) 哈希分桶，命中返回 true；**区间 0\~1 浮点数**，左闭右开（如 `0, 0.9` 表示 90%，绝不能传 `0, 90`） |
| UtilsFunction.getCurrentHour() / getCurrentMinute() | 0\~23 / 0\~59；**时间计算必须用这两个函数**，禁止 `java.util.Calendar` / `java.time` 等 raw API |

> 标准写法：`int cur = UtilsFunction.getCurrentHour() * 60 + UtilsFunction.getCurrentMinute();`

## 延迟公式（结果单位：秒）

```java
int cur = UtilsFunction.getCurrentHour() * 60 + UtilsFunction.getCurrentMinute();  // 当前时刻（分钟制）
int delayToNext930 = (570 + 1440 - cur) * 60;                  // 固定次日 9:30（延迟变量必须 int，不能 long）
int delayToNext10  = (600 + 1440 - cur) * 60;                  // 固定次日 10:00（延迟变量必须 int，不能 long）
int delayTo2Days10 = (600 + 2880 - cur) * 60;                  // 后天 10:00（延迟变量必须 int，不能 long）
// 最近的下一个10:00（10点前=今天，10点后=明天）：
int delayTo1000 = (cur < 600) ? (600 - cur) * 60 : (600 + 1440 - cur) * 60;
```

*   1440 = 一天的分钟数；N 天后 = 加 N×1440
*   分钟项永远是减号：`(24-h)*3600 + 570*60 + m*60` 是错的，正确为 `(24-h)*3600 - m*60 + 570*60`

## 延迟轮次打标顺序（先挂延时、再打标）

延迟轮次中，`appendDelayReach` 必须在 `setDecisionTag` **之前**调用。因为 appendDelayReach 内部有 `delayTimes > 0` 守卫，延迟值算错（≤0/null）时不会真正挂上延时；若先打 `_已延时到XX` 标，延时没挂上时落库 tag 会谎报“已延时”，导致回收数据误判。

正确写法（先挂延时、再打标）：
```java
// 纯延迟轮次
int delayTo930 = (cur < 570) ? (570 - cur) * 60 : (570 + 1440 - cur) * 60;
ActionFunction.appendDelayReach($response, delayTo930);          // ① 先挂延时
$response.setDecisionTag(decisionTag + "_已延时到930");          // ② 再打标
return;

// 触达+延迟轮次（buildXxx 内部已打触达 tag，手动延时 tag 覆盖为最终态）
ActionFunction.buildSms($response, mobile, smsTemplate, emptyMap, decisionTag + "_发送短信");
int delayTo1030 = (cur < 630) ? (630 - cur) * 60 : (630 + 1440 - cur) * 60;
ActionFunction.appendDelayReach($response, delayTo1030);         // ① 先挂延时
$response.setDecisionTag(decisionTag + "_已延时到1030");         // ② 再打标
return;
```

错误写法（先打标、后挂延时）：
```java
$response.setDecisionTag(decisionTag + "_已延时到930");          // ✗ 先打标
ActionFunction.appendDelayReach($response, delayTo930);          // ✗ 后挂延时——若 delayTo930≤0 延时没挂上，tag 已谎报
```

## RequestDto 常用字段（请求对象字段速查）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| getCategoryCode | String | 必须与圈人侧配置一致 |
| getCallTimes | Integer | 链路轮次，可为 null（判空后默认1） |
| getUserId | Long | 拼字符串用 `+ ""` |
| getUserInfo().getMobile() | String | UserInfo 可能为 null |
| getEmailAddr | String | 邮箱（buildMail 用）；需判空，搭配 emailBlackUserTag 黑名单检查 |
| getRegScoreModel | float | 注册分（基本类型，缺失靠哨兵值 -1 识别，绝不能落入发券分组） |
| getLastLimitApplyInfo() | LastLimitApply | **戳额**记录（额度申请），null 判断用 `!= null && getLimitApplyTime() != null` |
| getLastLoanListInfo() | LastLoanApply | **发标**记录（借款/进件），null 判断用 `!= null && getLoanListApplyTime() != null` |
| getWabaBlackUserTag | int | WABA黑名单（基本类型，0=非黑名单，**1=在黑名单中**） |
| getEmailBlackUserTag | int | 邮箱黑名单（基本类型，0=非黑名单，**1=在黑名单中**） |
| getAvailableAmount | BigDecimal | 当前可用额度 |
| getNowRiskLevelList | List | 风控等级，S1/S2 来自 riskKey=demo\_risk\_level\_v1 |
| getLastMarketingInsertTime | Date | 最近一次案件进件时间（进件拦截检查用） |
| getAttachment() | Map | 圈人侧传入的变量 Map（如 userId/accountName/hasFromSource） |

> **⚠️ 严禁混用戳额与发标 getter**：`getLastLimitApplyInfo()` 是**戳额**（额度申请）状态，`getLastLoanListInfo()` 是**发标**（借款/进件）状态，语义完全不同。混用会导致中途转化停触达失效（已转化用户被反复触达）。发标判断标准范式：
> 
> ```java
> // 已发标 → 停止触达；未发标 = null 或 时间为 null
> boolean noBid = request.getLastLoanListInfo() == null
>         || request.getLastLoanListInfo().getLoanListApplyTime() == null;
> if (!noBid) {
>     response.setDecisionTag(baseDecisionTag + "_has_bid_stop");
>     response.setResultObject(1);
>     return;
> }
> ```

## ResponseDto 常用方法

| 方法 | 语义 |
| --- | --- |
| setDecision(1) | **1=可营销、有指令；9=暂不营销、无指令**（defaultRule 初始值 9，命中营销由 buildXxx 内部置 1）；decision 生命周期 = 整条营销链路，从本次决策持续到营销结束（用户转化或轮次走完） |
| setDecisionTag(tag) | 覆盖式打标，随 response 返回 PMS 落库供 SQL 分析（不是日志） |
| setResultObject(1) | 对照组惯例：打标 + 标记后直接 return |
| setResultObject(2) | 不合格排除惯例（如无风控等级、非目标分层，见 S1/S2 规则）；**超出轮次/异常退出兜底也用它** |

**标准稳定退出模式（所有轮次结束/异常分支强制）**：打明确 tag + setResultObject(2) + 直接 return，防止规则引擎循环或状态不一致。标准范式：

```java
if (callTimes > max_rounds) {
    $response.setDecisionTag(decisionTag + "_超出轮次");
    $response.setResultObject(2); // 不合格排除
    return;
}
```

## 灰度三级泳道全表

| 一级泳道（业务阶段） | 二级泳道（节点） | 三级泳道（工具） |
| --- | --- | --- |
| overall\_new\_user（新客全局） | register\_unstamped 注册未戳额 | \_telemarketing 电销 / \_com SMS·AI外呼·IVR·WABA / \_coupon 优惠券 / \_push |
| 同上 | grant\_success\_nobid 给额未发标 | \_telemarketing / \_com / \_coupon / \_push |
| 同上 | credit\_expiration\_recall 额度过期召回 | \_telemarketing / \_com / \_push |
| 同上 | unpass 戳额未过件 | \_com / \_push |
| 同上 | 无（活跃） | active\_user 活跃用户 user\_id 分组 |
| 同上 | grant\_uplimit\_new 新客提额 | 无三级 |
| 无（激活未注册） | 全部激活未注册 | install\_unregister\_device 未注册设备id分组 |
| 无（激活未注册） | 已记录手机号 | install\_unregister\_mobile 未注册手机号分组 |

用法：一级→二级→三级依次 get\_hashed\_isin；同名泳道跨策略复用是设计本意。  
**某次版本泳道变更**：`active_user`（活跃）从独立一级泳道移入新客全局一级下（二级节点“活跃”）。影响：**活跃用户现在也先过一级全局 9:1 分流，一级就切 10% 对照组**，再走 active\_user 自身分流；写活跃类策略时一级检查不能漏。
**默认分流比例**：用户未指定时，一级、二级、三级依次 9:1、9:1、5:5，即 `get_hashed_isin(桶名, userId+"", 0, 0.9)` / `(0, 0.9)` / `(0, 0.5)`（**区间 0\~1 浮点数，左闭右开，绝不是 0\~100 整数**）。

## 多实验组对照组打标范式

策略含多个实验组（如 95折/99折）时，对照组 decisionTag 必须携带分组标识，确保回收 SQL 能按组精确匹配「某组实验组 vs 同组对照组」。

核心原则：
- **一二级灰度**（全局/节点层）排除统一前置处理，**不带组标识**（这些人与实验组不可比，不应纳入 AB 对比）
- **三级灰度**（实验/对照 5:5 分割）检查**下沉到实验动作所在轮次的分组 if 分支内**，对照组标签追加组标识
- 只有三级灰度切出来的对照组与实验组同源随机分割，AB 对比才有效

```java
// === 一二级灰度：统一前置拦截，不带分组标识 ===
Boolean hasHit  = UtilsFunction.get_hashed_isin("overall_new_user", $request.getUserId()+"", 0, 0.9);
Boolean hasHit1 = UtilsFunction.get_hashed_isin("register_unstamped", $request.getUserId()+"", 0, 0.9);
Boolean hasHit2 = UtilsFunction.get_hashed_isin("register_unstamped_coupon", $request.getUserId()+"", 0, 0.5);

if (!hasHit || !hasHit1) {
    if (!hasHit) {
        decisionTag += "_not_in_global_gray";
    } else if (!hasHit1) {
        decisionTag += "_not_in_Unstamped_gray";
    }
    $response.setDecisionTag(decisionTag);
    $response.setResultObject(1);
    return;
}

// === 三级灰度：下沉到实验动作所在轮次，按分组打对照组标签 ===
if (callTimes == 2) {  // T0 发券轮次
    if (is95Group) {
        if (!hasHit2) {
            decisionTag += "_not_in_Unstamped_95coupon_gray";  // 95折对照组
            $response.setDecisionTag(decisionTag);
            $response.setResultObject(1);
            return;
        }
        ActionFunction.buildCoupon($response, coupon95, 1, decisionTag + "_95折券");
        ActionFunction.buildWaba($response, wabaCoupon, mobile, emptyMap, decisionTag + "_95折券_waba");
    } else if (is99Group) {
        if (!hasHit2) {
            decisionTag += "_not_in_Unstamped_99coupon_gray";  // 99折对照组
            $response.setDecisionTag(decisionTag);
            $response.setResultObject(1);
            return;
        }
        ActionFunction.buildCoupon($response, coupon99, 1, decisionTag + "_99折券");
        ActionFunction.buildWaba($response, wabaCoupon, mobile, emptyMap, decisionTag + "_99折券_waba");
    }
    ActionFunction.appendDelayReach($response, delayToNext10);
    return;
}
```

回收 SQL 分组匹配示例：
```sql
WHEN decision_tag LIKE '%_95折券%'          THEN '95折实验组'
WHEN decision_tag LIKE '%_99折券%'          THEN '99折实验组'
WHEN decision_tag LIKE '%95coupon_gray%'    THEN '95折对照组'
WHEN decision_tag LIKE '%99coupon_gray%'    THEN '99折对照组'
```