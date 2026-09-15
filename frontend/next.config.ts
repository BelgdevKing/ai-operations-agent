import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  /**
   * Build a self-contained server for the production image.
   *
   * `next build` normally leaves a `.next` directory that still needs the
   * whole `node_modules` tree beside it to run. `standalone` instead traces
   * what the server actually imports and writes it, plus a `server.js`, into
   * `.next/standalone` - so the runtime image copies three directories and
   * carries no npm dependency tree, no build cache and no toolchain.
   *
   * It costs nothing in development: `next dev` ignores it.
   */
  output: "standalone",

  /**
   * Do not announce the framework and its version to every visitor.
   *
   * Small, and not a security boundary - anybody can identify Next.js from the
   * markup. It is here because a version number in a response header is free
   * reconnaissance, and turning it off is one line.
   */
  poweredByHeader: false,
};

export default nextConfig;
