/**
 * Build-time configuration, and the one case that only shows up in production.
 *
 * `NEXT_PUBLIC_*` values are substituted into the bundle while it is built, so
 * a Docker build argument that nobody passed does not arrive as `undefined` -
 * it arrives as the empty string. `??` does not fall back on an empty string,
 * so the naive spelling of this module produces an empty API base URL, and an
 * empty base URL turns every call into a same-origin request to the Next.js
 * server. That server has no API on it, so the failure surfaces as a 404 on a
 * page somewhere, a long way from the missing build argument that caused it.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { apiUrl, appName, configured } from "@/lib/config";

test("a value that was set is the value that is used", () => {
  assert.equal(configured("https://api.example.test"), "https://api.example.test");
});

test("an unpassed build argument is treated as unset, not as a value", () => {
  assert.equal(configured(""), undefined);
  assert.equal(configured(undefined), undefined);
});

test("whitespace is not configuration", () => {
  // `--build-arg NEXT_PUBLIC_API_URL=" "` is the same mistake wearing a hat.
  assert.equal(configured("   "), undefined);
  assert.equal(configured("\t\n"), undefined);
});

test("a configured value is trimmed rather than used as typed", () => {
  assert.equal(configured("  https://api.example.test  "), "https://api.example.test");
});

test("there is always an API base URL and it never ends in a slash", () => {
  // Whatever the environment did or did not supply, the module exports
  // something a URL can be appended to.
  assert.ok(apiUrl.length > 0);
  assert.ok(!apiUrl.endsWith("/"));
});

test("there is always an application name", () => {
  assert.ok(appName.length > 0);
});
