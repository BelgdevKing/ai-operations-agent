/**
 * Makes the project's import style work when the tests run under plain Node.
 *
 * Two gaps to close. Node reads neither `tsconfig.json` nor its `paths`, so
 * `@/lib/...` resolves nowhere; and Node's ESM resolver requires a file
 * extension on relative imports, while TypeScript's bundler resolution - what
 * this project and Next.js use - omits it. A resolver hook closes both in
 * about twenty lines and keeps the test suite dependency-free; the alternative
 * is a bundler-backed test runner and everything it drags in.
 *
 * Node strips the type annotations from `.ts` itself. It cannot compile JSX,
 * so only `.ts` modules are testable this way - which is why the session, the
 * API client, the permission rules and the validators are all plain TypeScript
 * with the React bindings kept in separate `.tsx` files.
 */

import { statSync } from "node:fs";
import { registerHooks } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const SRC = path.resolve(import.meta.dirname, "..", "src");
const CANDIDATES = [".ts", ".tsx", "/index.ts", "/index.tsx"];

/** The first candidate that is a file on disk, as a file URL. */
function firstExisting(base) {
  for (const suffix of ["", ...CANDIDATES]) {
    const candidate = base + suffix;
    // isFile, not merely exists: "@/lib/api" names a directory as well as an
    // index module, and handing Node the directory fails with EISDIR.
    if (statSync(candidate, { throwIfNoEntry: false })?.isFile()) {
      return pathToFileURL(candidate).href;
    }
  }
  return null;
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      const resolved = firstExisting(path.join(SRC, specifier.slice(2)));
      if (resolved) return { url: resolved, shortCircuit: true };
    }

    if (specifier.startsWith(".") && context.parentURL?.startsWith("file:")) {
      const parent = path.dirname(fileURLToPath(context.parentURL));
      const resolved = firstExisting(path.resolve(parent, specifier));
      if (resolved) return { url: resolved, shortCircuit: true };
    }

    return nextResolve(specifier, context);
  },
});
