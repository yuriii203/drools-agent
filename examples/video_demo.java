rule "Demo_Coupon_Recall"
no-loop true
lock-on-active true
salience 9999
when
    $request: RequestDto($request.getCategoryCode().equals("Demo_Coupon_Recall"))
    $response: ResponseDto()
then
    System.out.println("规则触发: " + drools.getRule().getName());

    $response.setDecision(1);
    String decisionTag = "Demo_Coupon_Recall";
    $response.setDecisionTag(decisionTag);

    // 模板/批次配置区（演示占位符）
    String smsTemplate = "TEMPLATE_SMS_001";
    String wabaTemplate = "TEMPLATE_WABA_001";
    String couponBatch = "batch_demo_001";

    // callTimes 判空（Integer 包装类型可为 null）
    Integer callTimes = $request.getCallTimes();
    if (callTimes == null) { callTimes = 1; }

    // 当前时刻（分钟制）
    int cur = UtilsFunction.getCurrentHour() * 60 + UtilsFunction.getCurrentMinute();

    // 手机号判空
    String mobile = $request.getUserInfo() == null ? null : $request.getUserInfo().getMobile();
    if (mobile == null || mobile.trim().isEmpty()) {
        $response.setDecisionTag(decisionTag + "_手机号缺失");
        $response.setResultObject(2);
        return;
    }

    // 一级灰度 9:1（未命中 = 对照组）
    Boolean hasHit = UtilsFunction.get_hashed_isin("overall_new_user", $request.getUserId()+"", 0, 0.9);
    if (!hasHit) {
        $response.setDecisionTag(decisionTag + "_not_in_global_gray");
        $response.setResultObject(1);
        return;
    }

    // 轮次分支
    // 第1轮（进件时刻）：纯延迟轮，挂延迟到 T+1天 10:00
    if (callTimes == 1) {
        int delayToNext10 = (600 + 1440 - cur) * 60;
        ActionFunction.appendDelayReach($response, delayToNext10);
        $response.setDecisionTag(decisionTag + "_已延时到1000");
        return;
    } else if (callTimes == 2) {
        // 第2轮：短信 + 发券，再挂延迟到次日 10:00
        Map<String, Object> emptyMap = new HashMap<String, Object>();
        ActionFunction.buildSms($response, mobile, smsTemplate, emptyMap, decisionTag + "_第2轮_发送短信");
        ActionFunction.buildCoupon($response, couponBatch, 1, decisionTag + "_第2轮_发券");
        int delayToNext10 = (600 + 1440 - cur) * 60;
        ActionFunction.appendDelayReach($response, delayToNext10);
        $response.setDecisionTag(decisionTag + "_已延时到1000");
        return;
    } else if (callTimes == 3) {
        // 第3轮：WABA 收尾（末轮不挂延迟，链路自然结束）
        Map<String, Object> emptyMap = new HashMap<String, Object>();
        ActionFunction.buildWaba($response, wabaTemplate, mobile, emptyMap, decisionTag + "_第3轮_发送WABA");
        $response.setDecisionTag(decisionTag + "_第3轮_发送WABA");
        return;
    } else {
        // 兜底：超出轮次，标准稳定退出
        $response.setDecisionTag(decisionTag + "_超出轮次");
        $response.setResultObject(2);
        return;
    }
end
