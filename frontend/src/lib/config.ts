/**
 * Configuration this build was compiled with.
 *
 * Only `NEXT_PUBLIC_*` values are available client-side, and the word to hold
 * on to is **compiled**: Next.js substitutes these at build time, so they are
 * literals in the JavaScript the browser downloads. Two consequences, both of
 * which matter in production.
 *
 * A secret here is not a secret. Anyone who opens the bundle can read it, so
 * nothing but addresses and labels belongs under `NEXT_PUBLIC_`.
 *
 * And an image cannot be re-pointed by setting an environment variable on the
 * container: the value was fixed when the image was built. Which is why
 * `NEXT_PUBLIC_API_URL` is a **build argument** in
 * infrastructure/docker/frontend/Dockerfile, and why a deployment that changes
 * the API address rebuilds the frontend image.
 */

/**
 * A value that was set to something, rather than set to nothing.
 *
 * Exported because this is the whole of the logic worth testing here, and the
 * case it exists for - an unset build argument - cannot be reproduced by
 * importing this module a second time with a different environment.
 */
export function configured(value: string | undefined): string | undefined {
  // `??` alone would not do: an unset build argument arrives as the empty
  // string, not as undefined, and an empty API base URL turns every call into
  // a same-origin request to the Next.js server - which serves no API, so the
  // failure arrives later and somewhere else.
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

export const apiUrl =
  configured(process.env.NEXT_PUBLIC_API_URL)?.replace(/\/$/, "") ??
  "http://localhost:8000";

export const appName =
  configured(process.env.NEXT_PUBLIC_APP_NAME) ?? "AI Operations Agent Platform";
