export type ApiErrorCategory = "auth" | "business" | "validation" | "system";

export interface ApiErrorCodeDefinition {
  readonly category: ApiErrorCategory;
  readonly httpStatus: number;
  readonly message: string;
}

export const API_ERROR_CODES = {
  CHAT_CONTEXT_LIMIT_REACHED: {
    category: "business",
    httpStatus: 409,
    message: "上下文已达到 95%，请执行手动压缩后再继续对话。"
  },
  AUTH_INVALID_CREDENTIALS: {
    category: "auth",
    httpStatus: 401,
    message: "Invalid credentials."
  },
  AUTH_FORBIDDEN: {
    category: "auth",
    httpStatus: 403,
    message: "You do not have permission to access this resource."
  },
  AUTH_SESSION_REVOKED: {
    category: "auth",
    httpStatus: 401,
    message: "The authentication session has been revoked."
  },
  AUTH_UNAUTHENTICATED: {
    category: "auth",
    httpStatus: 401,
    message: "Authentication is required."
  },
  BUSINESS_CONFLICT: {
    category: "business",
    httpStatus: 409,
    message: "The requested operation conflicts with the current resource state."
  },
  BUSINESS_NOT_FOUND: {
    category: "business",
    httpStatus: 404,
    message: "The requested resource was not found."
  },
  RECOVERY_DISABLED: {
    category: "business",
    httpStatus: 409,
    message: "Production recovery is disabled."
  },
  RECOVERY_NOT_ELIGIBLE: {
    category: "business",
    httpStatus: 409,
    message: "The diagnostic is not eligible for governed recovery."
  },
  RECOVERY_APPROVAL_REQUIRED: {
    category: "business",
    httpStatus: 409,
    message: "A current incident owner approval is required."
  },
  RECOVERY_APPROVAL_EXPIRED: {
    category: "business",
    httpStatus: 409,
    message: "The recovery approval has expired."
  },
  RECOVERY_CONFIRMATION_MISMATCH: {
    category: "validation",
    httpStatus: 400,
    message: "The incident confirmation does not match."
  },
  RECOVERY_INVALID_TRANSITION: {
    category: "business",
    httpStatus: 409,
    message: "The recovery state does not allow this operation."
  },
  RECOVERY_EXECUTION_UNCERTAIN: {
    category: "business",
    httpStatus: 409,
    message: "The recovery result is uncertain and requires manual intervention."
  },
  RECOVERY_TARGET_CHANGED: {
    category: "business",
    httpStatus: 409,
    message: "Trusted recovery target facts changed before execution."
  },
  VALIDATION_INVALID_ARGUMENT: {
    category: "validation",
    httpStatus: 400,
    message: "The request parameters are invalid."
  },
  VALIDATION_MISSING_FIELD: {
    category: "validation",
    httpStatus: 422,
    message: "A required field is missing."
  },
  SYSTEM_INTERNAL_ERROR: {
    category: "system",
    httpStatus: 500,
    message: "The system could not complete the request."
  },
  SYSTEM_UNAVAILABLE: {
    category: "system",
    httpStatus: 503,
    message: "The system is temporarily unavailable."
  }
} as const satisfies Record<string, ApiErrorCodeDefinition>;

export type ApiErrorCode = keyof typeof API_ERROR_CODES;
