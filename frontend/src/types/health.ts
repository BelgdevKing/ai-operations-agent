/**
 * Health contracts, mirroring `app/schemas/health.py`.
 */

/** Response of `GET /health`. */
export interface HealthResponse {
  /** Always "ok" when the process is serving. */
  status: string;
  service: string;
  version: string;
  environment: string;
}

/** One dependency's verdict. An unavailable optional dependency is not fatal. */
export interface DependencyCheck {
  /** "ok" or "unavailable". */
  status: string;
  required: boolean;
}

/**
 * Response of `GET /health/ready`.
 *
 * Returned with 503 when the verdict is "degraded", so the payload is present
 * on both the success and the failure status.
 */
export interface ReadinessResponse {
  /** "ready" or "degraded". */
  status: string;
  dependencies: Record<string, DependencyCheck>;
}
