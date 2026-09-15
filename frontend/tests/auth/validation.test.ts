/**
 * Form validation.
 *
 * The rule these tests exist to protect: the form may never be stricter than
 * the backend, or it refuses input the server would have accepted.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PASSWORD_LENGTH,
  MIN_PASSWORD_LENGTH,
  hasErrors,
  normalizeEmail,
  validateLogin,
  validateRegistration,
} from "@/lib/auth/validation";

const GOOD_PASSWORD = "correct-horse-battery-staple";

function registration(overrides: Partial<Parameters<typeof validateRegistration>[0]> = {}) {
  return validateRegistration({
    email: "ada@example.com",
    password: GOOD_PASSWORD,
    organizationName: "Acme Operations",
    firstName: "Ada",
    lastName: "Lovelace",
    ...overrides,
  });
}

test("addresses are lowercased and trimmed, as the backend stores them", () => {
  assert.equal(normalizeEmail("  Ada@Example.COM "), "ada@example.com");
});

// -- Sign in ------------------------------------------------------------------

test("valid credentials pass", () => {
  assert.equal(hasErrors(validateLogin({ email: "ada@example.com", password: "x" })), false);
});

test("both fields are required", () => {
  assert.ok(validateLogin({ email: "", password: GOOD_PASSWORD }).email);
  assert.ok(validateLogin({ email: "ada@example.com", password: "" }).password);
});

test("sign-in does not check the address format", () => {
  // The backend does not either: refusing a malformed address differently from
  // a wrong password would answer "does this address have an account?".
  assert.equal(hasErrors(validateLogin({ email: "not-an-address", password: "x" })), false);
});

test("sign-in does not impose the minimum password length", () => {
  // An account created before the rule changed must still be able to sign in.
  assert.equal(hasErrors(validateLogin({ email: "ada@example.com", password: "short" })), false);
});

test("an over-long password is refused before it reaches the hasher", () => {
  const errors = validateLogin({ email: "ada@example.com", password: "x".repeat(200) });
  assert.ok(errors.password);
});

// -- Registration -------------------------------------------------------------

test("a complete registration passes", () => {
  assert.equal(hasErrors(registration()), false);
});

test("names are optional", () => {
  assert.equal(hasErrors(registration({ firstName: "", lastName: "" })), false);
});

test("the address must look like one", () => {
  assert.ok(registration({ email: "not-an-address" }).email);
});

test("the password minimum matches the backend's", () => {
  assert.ok(registration({ password: "x".repeat(MIN_PASSWORD_LENGTH - 1) }).password);
  assert.equal(hasErrors(registration({ password: "x".repeat(MIN_PASSWORD_LENGTH) })), false);
});

test("the password maximum matches the backend's", () => {
  assert.ok(registration({ password: "x".repeat(MAX_PASSWORD_LENGTH + 1) }).password);
});

test("a password containing the address local part is refused", () => {
  // Mirrors the backend rule: a password built from the address is guessable
  // from the address.
  assert.ok(registration({ email: "ada.lovelace@example.com", password: "ada.lovelace-1234" }).password);
});

test("a short local part does not trigger the containment rule", () => {
  // The backend only applies it from four characters up.
  assert.equal(hasErrors(registration({ email: "ada@example.com", password: "ada-is-in-here-ok" })), false);
});

test("an organization name is required", () => {
  assert.ok(registration({ organizationName: "   " }).organizationName);
});

test("over-long names are refused", () => {
  assert.ok(registration({ organizationName: "x".repeat(201) }).organizationName);
  assert.ok(registration({ firstName: "x".repeat(101) }).firstName);
  assert.ok(registration({ lastName: "x".repeat(101) }).lastName);
});
