import { CircleAlert, CircleCheck, PlugZap } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ReadinessResult } from "@/lib/api";

/**
 * Renders the platform's readiness.
 *
 * Three states, not two. "Every dependency is healthy", "some dependency is
 * not", and "the API did not answer at all" are different situations, and
 * collapsing the third into the second would tell an operator the backend
 * reported a problem when in fact it said nothing.
 */
export function PlatformStatus({ result }: { result: ReadinessResult }) {
  if (result.kind === "unreachable") {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <PlugZap className="text-ink-muted size-4" aria-hidden />
            API unreachable
          </CardTitle>
          <CardDescription>
            The platform did not answer: {result.reason}. Start the backend with{" "}
            <code className="font-mono text-xs">uv run uvicorn …</code> and reload.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const { report } = result;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-4">
          <CardTitle>Platform readiness</CardTitle>
          <Badge tone={report.ready ? "healthy" : "failing"}>
            {report.ready ? (
              <CircleCheck className="size-3.5" aria-hidden />
            ) : (
              <CircleAlert className="size-3.5" aria-hidden />
            )}
            {report.ready ? "Ready" : "Not ready"}
          </Badge>
        </div>
        <CardDescription>
          Every dependency the platform needs before it can serve traffic.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-border divide-y">
          {report.components.map((component) => (
            <li key={component.component} className="flex flex-col gap-1 py-3 first:pt-0">
              <div className="flex items-center justify-between gap-4">
                <span className="font-mono text-sm">{component.component}</span>
                <span className="flex items-center gap-3">
                  <span className="text-ink-muted text-xs tabular-nums">
                    {component.latency_ms.toFixed(1)} ms
                  </span>
                  <Badge tone={component.healthy ? "healthy" : "failing"}>
                    {component.healthy ? "healthy" : "failing"}
                  </Badge>
                </span>
              </div>
              {component.error !== null && (
                <p className="text-failing font-mono text-xs">{component.error}</p>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
