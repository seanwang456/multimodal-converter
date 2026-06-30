/** @type {import('next').NextConfig} */

// 服务端反代目标：浏览器相对路径 /api/* 由 Next 转发到 API。
// - 本地 dev 默认 http://localhost:8000
// - docker compose 注入 API_PROXY_TARGET=http://api:8000（容器网络内可达）
const apiProxy = process.env.API_PROXY_TARGET || "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiProxy}/api/:path*` }];
  },
};
export default nextConfig;
