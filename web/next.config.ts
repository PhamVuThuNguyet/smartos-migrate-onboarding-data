import type { NextConfig } from "next";

const api = process.env.MIGRATE_API_ORIGIN ?? "https://smartos-migrate-onboarding-data.vercel.app";

const nextConfig: NextConfig = {
  experimental: {
    proxyTimeout: 1_800_000,
  },
  async rewrites() {
    return [
      { source: "/api/preview", destination: `${api}/preview` },
      { source: "/api/import", destination: `${api}/import` },
      { source: "/api/jobs/:id", destination: `${api}/jobs/:id` },
      { source: "/api/jobs/:id/resume", destination: `${api}/jobs/:id/resume` },
      { source: "/api/login", destination: `${api}/login` },
      { source: "/api/logout", destination: `${api}/logout` },
      { source: "/api/health", destination: `${api}/health` },
    ];
  },
};

export default nextConfig;
