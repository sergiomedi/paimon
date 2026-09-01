/**
 * Typed client for the Paimon API.
 *
 * The base URL comes from the environment rather than a constant, because the
 * same build runs against a local backend, a staging deployment and production.
 * Baking a host into the bundle means one image per environment, which defeats
 * the point of building it once.
 */

const API_BASE_URL = process.env["NEXT_PUBLIC_API_BASE_URL"] ?? "http://localhost:8000";

export interface ComponentStatus {
  component: string;
  healthy: boolean;
  latency_ms: number;
  error: string | null;
}

export interface ReadinessReport {
  ready: boolean;
  components: ComponentStatus[];
}

/** Either the platform answered, or it did not and we say why. */
export type ReadinessResult =
  | { kind: "answered"; report: ReadinessReport }
  | { kind: "unreachable"; reason: string };

/**
 * Fetch the platform's readiness report.
 *
 * A 503 is a valid answer, not a failure: the endpoint returns the full
 * component list either way, and knowing which dependency is down is the whole
 * point. Only a transport failure counts as unreachable.
 */
export async function fetchReadiness(): Promise<ReadinessResult> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/v1/health/ready`, {
      // Health is never cached: a cached readiness report is a lie with a
      // timestamp.
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });

    if (response.status !== 200 && response.status !== 503) {
      return { kind: "unreachable", reason: `unexpected status ${response.status}` };
    }

    const report = (await response.json()) as ReadinessReport;
    return { kind: "answered", report };
  } catch (error) {
    const reason = error instanceof Error ? error.message : "unknown error";
    return { kind: "unreachable", reason };
  }
}
