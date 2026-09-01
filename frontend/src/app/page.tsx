import { PlatformStatus } from "@/components/platform-status";
import { fetchReadiness } from "@/lib/api";

// Rendered per request: a cached health page is worse than no health page.
export const dynamic = "force-dynamic";

export default async function HomePage() {
  const result = await fetchReadiness();

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-2xl flex-col justify-center gap-6 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Paimon</h1>
        <p className="text-ink-muted text-sm">
          An AI Operations Platform for engineering organizations. Phase 1 — Foundation.
        </p>
      </header>

      <PlatformStatus result={result} />

      <footer className="text-ink-muted text-xs">
        Retrieval, agents and MCP integration arrive in Phases 2 to 4.
      </footer>
    </main>
  );
}
