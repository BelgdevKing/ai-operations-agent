import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { FlatCompat } from "@eslint/eslintrc";

const __dirname = dirname(fileURLToPath(import.meta.url));

// eslint-config-next is still published as eslintrc-style shareable configs, so
// they are adapted rather than imported directly.
const compat = new FlatCompat({ baseDirectory: __dirname });

const config = [
  {
    ignores: [".next/**", "out/**", "node_modules/**", "next-env.d.ts"],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // Injecting raw HTML is the shortest path to an XSS hole, and nothing
      // here needs it. An exception should be argued for at the call site with
      // an explicit disable comment, not enabled silently.
      "react/no-danger": "error",
    },
  },
];

export default config;
