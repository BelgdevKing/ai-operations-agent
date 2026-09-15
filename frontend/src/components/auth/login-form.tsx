"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { describedById, Field, Input } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Spinner } from "@/components/ui/states";
import { describeSignInError } from "@/lib/api/presentation";
import { useRedirectWhenAuthenticated } from "@/hooks/use-redirect-when-authenticated";
import { useSession, useSessionStore } from "@/lib/auth/session-context";
import { normalizeEmail, validateLogin, type FieldErrors } from "@/lib/auth/validation";

/**
 * Sign in.
 *
 * `method="post"` matters even though the submit is handled in JavaScript: a
 * form with no method defaults to GET, so if the handler ever failed to run,
 * pressing Enter would put the password in the address bar, the browser
 * history and any proxy log. This way the fallback is a harmless POST.
 */
export function LoginForm() {
  const store = useSessionStore();
  const session = useSession();
  const router = useRouter();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);

  // Already signed in - arriving here from a bookmark, say. Nothing to do but
  // move on; shared with the registration page so the two cannot drift.
  useRedirectWhenAuthenticated();

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) return;

    const local = validateLogin({ email, password });
    setFieldErrors(local);
    setError(null);
    if (Object.keys(local).length > 0) return;

    setPending(true);
    try {
      await store.signIn({ email: normalizeEmail(email), password });
      // The password is not kept around after it has been used.
      setPassword("");
      router.replace("/dashboard");
    } catch (failure) {
      setError(failure);
      setFieldErrors(describeSignInError(failure).fieldErrors);
    } finally {
      setPending(false);
    }
  }

  const expired = session.status === "anonymous" && session.reason === "expired";

  return (
    <form method="post" onSubmit={onSubmit} noValidate className="space-y-4">
      {expired && !error && (
        <Notice tone="warn" title="Your session has ended">
          Sign in again to continue.
        </Notice>
      )}

      {error !== null && <SignInError error={error} />}

      <Field htmlFor="email" label="Email" error={fieldErrors.email}>
        <Input
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          autoFocus
          required
          maxLength={320}
          placeholder="you@example.com"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          aria-invalid={fieldErrors.email ? true : undefined}
          aria-describedby={describedById("email")}
          disabled={pending}
        />
      </Field>

      <Field htmlFor="password" label="Password" error={fieldErrors.password}>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={128}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          aria-invalid={fieldErrors.password ? true : undefined}
          aria-describedby={describedById("password")}
          disabled={pending}
        />
      </Field>

      <Button type="submit" className="w-full" disabled={pending}>
        {pending && <Spinner className="border-white/40 border-t-white" />}
        {pending ? "Signing in ..." : "Sign in"}
      </Button>

      <p className="text-center text-sm text-ink-muted">
        No account yet?{" "}
        <Link
          href="/register"
          className="rounded text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
        >
          Create one
        </Link>
      </p>
    </form>
  );
}

/**
 * What went wrong, in the words of the shared mapping.
 *
 * A wrong password arrives as the backend's own 401 message, which is the same
 * for a wrong password, an unknown address and a suspended account - the
 * backend refusing to say which, and not something to elaborate on here.
 */
function SignInError({ error }: { error: unknown }) {
  const { title, message, requestId } = describeSignInError(error);

  return (
    <Notice tone="danger" title={title} role="alert">
      <p>{message}</p>
      {requestId && <p className="mt-1 font-mono text-xs">request {requestId}</p>}
    </Notice>
  );
}
