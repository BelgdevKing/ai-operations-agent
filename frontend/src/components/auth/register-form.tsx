"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { describedById, Field, Input } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { Spinner } from "@/components/ui/states";
import { register } from "@/lib/api/endpoints";
import { describeError } from "@/lib/api/presentation";
import { useRedirectWhenAuthenticated } from "@/hooks/use-redirect-when-authenticated";
import { useSessionStore } from "@/lib/auth/session-context";
import {
  MIN_PASSWORD_LENGTH,
  normalizeEmail,
  validateRegistration,
  type FieldErrors,
} from "@/lib/auth/validation";

/**
 * Create an account and the organization that comes with it.
 *
 * Registration makes the user the **owner** of a new organization - the backend
 * does both in one call, because an organization with no owner could never be
 * administered. There is no "join an existing organization" flow yet; an
 * invitation system is not part of the current backend.
 *
 * The response carries an access token, so the new user is signed in without
 * being asked for the password they just chose.
 */
export function RegisterForm() {
  const store = useSessionStore();
  const router = useRouter();

  // Someone already signed in has no use for this page.
  useRedirectWhenAuthenticated();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");

  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) return;

    const local = validateRegistration({
      email,
      password,
      organizationName,
      firstName,
      lastName,
    });
    setFieldErrors(local);
    setError(null);
    if (Object.keys(local).length > 0) return;

    setPending(true);
    try {
      const created = await register({
        email: normalizeEmail(email),
        password,
        organization_name: organizationName.trim(),
        first_name: firstName.trim() || null,
        last_name: lastName.trim() || null,
      });

      setPassword("");
      await store.adoptToken(created.token);
      router.replace("/dashboard");
    } catch (failure) {
      setError(failure);
      // The backend's field names match this form's, except for the
      // organization, so its 422 messages land under the right inputs.
      const { fieldErrors: fromServer } = describeError(failure);
      setFieldErrors({
        ...fromServer,
        ...(fromServer.organization_name
          ? { organizationName: fromServer.organization_name }
          : {}),
      });
    } finally {
      setPending(false);
    }
  }

  return (
    <form method="post" onSubmit={onSubmit} noValidate className="space-y-4">
      {error !== null && <RegistrationError error={error} />}

      <Field
        htmlFor="organization-name"
        label="Organization name"
        error={fieldErrors.organizationName}
      >
        <Input
          id="organization-name"
          name="organization_name"
          autoComplete="organization"
          autoFocus
          required
          maxLength={200}
          placeholder="Acme Operations"
          value={organizationName}
          onChange={(event) => setOrganizationName(event.target.value)}
          aria-invalid={fieldErrors.organizationName ? true : undefined}
          aria-describedby={describedById("organization-name")}
          disabled={pending}
        />
      </Field>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field htmlFor="first-name" label="First name" error={fieldErrors.first_name}>
          <Input
            id="first-name"
            name="first_name"
            autoComplete="given-name"
            maxLength={100}
            value={firstName}
            onChange={(event) => setFirstName(event.target.value)}
            aria-invalid={fieldErrors.first_name ? true : undefined}
            aria-describedby={describedById("first-name")}
            disabled={pending}
          />
        </Field>

        <Field htmlFor="last-name" label="Last name" error={fieldErrors.last_name}>
          <Input
            id="last-name"
            name="last_name"
            autoComplete="family-name"
            maxLength={100}
            value={lastName}
            onChange={(event) => setLastName(event.target.value)}
            aria-invalid={fieldErrors.last_name ? true : undefined}
            aria-describedby={describedById("last-name")}
            disabled={pending}
          />
        </Field>
      </div>

      <Field htmlFor="email" label="Email" error={fieldErrors.email}>
        <Input
          id="email"
          name="email"
          type="email"
          autoComplete="email"
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

      <Field
        htmlFor="password"
        label="Password"
        hint={`At least ${MIN_PASSWORD_LENGTH} characters. Length is the only rule.`}
        error={fieldErrors.password}
      >
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          minLength={MIN_PASSWORD_LENGTH}
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
        {pending ? "Creating your account ..." : "Create account"}
      </Button>

      <p className="text-center text-sm text-ink-muted">
        Already have an account?{" "}
        <Link
          href="/login"
          className="rounded text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
        >
          Sign in
        </Link>
      </p>
    </form>
  );
}

function RegistrationError({ error }: { error: unknown }) {
  const { title, message, requestId } = describeError(error);

  return (
    <Notice tone="danger" title={title} role="alert">
      <p>{message}</p>
      {requestId && <p className="mt-1 font-mono text-xs">request {requestId}</p>}
    </Notice>
  );
}
