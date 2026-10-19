import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/preview", destination: "http://127.0.0.1:8000/preview" },
      { source: "/api/health", destination: "http://127.0.0.1:8000/health" },
    ];
  },
};

export default nextConfig;
