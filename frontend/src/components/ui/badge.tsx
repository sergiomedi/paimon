import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

type Tone = "healthy" | "failing" | "neutral";

const TONES: Record<Tone, string> = {
  healthy: "text-healthy border-healthy/30 bg-healthy/10",
  failing: "text-failing border-failing/30 bg-failing/10",
  neutral: "text-ink-muted border-border bg-transparent",
};

interface BadgeProps extends ComponentProps<"span"> {
  tone?: Tone;
}

export function Badge({ className, tone = "neutral", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        TONES[tone],
        className,
      )}
      {...props}
    />
  );
}
