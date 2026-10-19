import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/preview", destination: "https://smartos-migrate-onboarding-data.vercel.app/preview" },
      { source: "/api/health", destination: "https://smartos-migrate-onboarding-data.vercel.app/health" },
    ];
  },
};

export default nextConfig;
