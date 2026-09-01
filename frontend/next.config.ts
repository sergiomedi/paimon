import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Emits a minimal server bundle for the container image built in Phase 7.
  output: "standalone",
  typedRoutes: true,
};

export default nextConfig;
