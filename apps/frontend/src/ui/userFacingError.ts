import type { ApiErrorMessage } from "@agent-py/api-contracts";

const ERROR_MESSAGES: Readonly<Record<ApiErrorMessage["code"], string>> = {
  CHAT_CONTEXT_LIMIT_REACHED: "上下文已达到 95%，请执行手动压缩后再继续对话。",
  AUTH_INVALID_CREDENTIALS: "邮箱或密码不正确，请重新输入。",
  AUTH_FORBIDDEN: "你没有权限访问该数据。",
  AUTH_SESSION_REVOKED: "登录状态已失效，请重新登录。",
  AUTH_UNAUTHENTICATED: "请先登录后再继续。",
  BUSINESS_CONFLICT: "该数据已经存在或与当前状态冲突，请确认后重试。",
  BUSINESS_NOT_FOUND: "未找到请求的数据，它可能已被删除或你没有访问权限。",
  RECOVERY_DISABLED: "生产恢复当前未启用，请检查项目配置。",
  RECOVERY_NOT_ELIGIBLE: "当前诊断不满足受控恢复条件，请人工复核。",
  RECOVERY_APPROVAL_REQUIRED: "该恢复动作需要当前事件负责人批准。",
  RECOVERY_APPROVAL_EXPIRED: "恢复批准已过期，请重新检查当前证据后再决定。",
  RECOVERY_CONFIRMATION_MISMATCH: "事件确认信息不匹配，请重新核对。",
  RECOVERY_INVALID_TRANSITION: "当前恢复状态不允许执行该操作。",
  RECOVERY_EXECUTION_UNCERTAIN: "恢复结果无法确认，系统已转入人工介入。",
  RECOVERY_TARGET_CHANGED: "恢复目标事实已经变化，系统已阻止执行。",
  VALIDATION_INVALID_ARGUMENT: "提交的信息不符合要求，请检查后重试。",
  VALIDATION_MISSING_FIELD: "请补全必填信息后再试。",
  SYSTEM_INTERNAL_ERROR: "系统暂时无法完成该请求，请稍后重试。",
  SYSTEM_UNAVAILABLE: "服务暂时不可用，请稍后重试。"
};

function isApiErrorMessage(value: unknown): value is ApiErrorMessage {
  return value !== null && typeof value === "object" && "code" in value &&
    typeof value.code === "string" && value.code in ERROR_MESSAGES;
}

export function toUserFacingError(error: unknown): string {
  if (error !== null && typeof error === "object" && "error" in error && isApiErrorMessage(error.error)) {
    if (
      typeof error.error.message === "string" &&
      /[\u3400-\u9fff]/u.test(error.error.message)
    ) {
      return error.error.message;
    }
    return ERROR_MESSAGES[error.error.code];
  }
  return "操作未能完成，请稍后重试。";
}
