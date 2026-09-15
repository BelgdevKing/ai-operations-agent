/** The API client. Import from here, not from the modules inside. */
export { api, request, DEFAULT_TIMEOUT_MS, ORGANIZATION_HEADER, REQUEST_ID_HEADER } from "./client";
export type { QueryValue, RequestOptions } from "./client";
export { createAuthenticatedApi } from "./authenticated";
export type { ApiCaller, AuthenticatedApiConfig, CallOptions } from "./authenticated";
export {
  AI_TIMEOUT_MS,
  changeMemberRole,
  generate,
  getCurrentUser,
  getHealth,
  getOrganization,
  getReadiness,
  listMembers,
  login,
  register,
  removeMember,
} from "./endpoints";
export type { ListMembersOptions } from "./endpoints";
export { describeError, describeSignInError, fieldErrorsOf } from "./presentation";
export type { ErrorPresentation } from "./presentation";
export {
  ApiError,
  NetworkError,
  errorMessage,
  isAbortError,
  isApiError,
  isNetworkError,
} from "./errors";
export type { ApiErrorInit } from "./errors";
