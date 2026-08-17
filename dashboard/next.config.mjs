/** @type {import('next').NextConfig} */
const nextConfig = {
  // Every page reads live data; caching a breakout ranking would defeat the point.
  experimental: { staleTimes: { dynamic: 0, static: 0 } },
};
export default nextConfig;
