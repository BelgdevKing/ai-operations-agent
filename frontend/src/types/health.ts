/** Shape returned by the backend GET /health endpoint. */
export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  environment: string;
}
