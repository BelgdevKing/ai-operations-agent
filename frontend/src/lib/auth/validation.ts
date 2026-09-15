/**
 * Form validation that mirrors `app/schemas/auth.py`.
 *
 * Here to save a round trip and to put the message next to the input that
 * caused it. The backend validates the same things again and its answer is the
 * one that counts - so these rules may be no stricter than the backend's, or
 * the form would refuse input the server would have accepted.
 */

/** `DEFAULT_MIN_PASSWORD_LENGTH` in the backend schema. */
export const MIN_PASSWORD_LENGTH = 12;
/** `MAX_PASSWORD_LENGTH`: an unbounded password is a cheap attack on the hasher. */
export const MAX_PASSWORD_LENGTH = 128;
/** The `email` limit on `LoginRequest`. */
export const MAX_EMAIL_LENGTH = 320;
export const MAX_NAME_LENGTH = 100;
export const MAX_ORGANIZATION_NAME_LENGTH = 200;

/** Field name to message. Empty means valid. */
export type FieldErrors = Record<string, string>;

/**
 * Lowercase and trim, as the backend does before storing or comparing.
 *
 * Applied to what gets sent so the address the user sees and the one the
 * backend matches on cannot differ by case alone.
 */
export function normalizeEmail(value: string): string {
  return value.trim().toLowerCase();
}

/**
 * A deliberately loose shape check: something, an @, something with a dot.
 *
 * Addresses that are legal but unusual are common enough that a strict pattern
 * rejects real users. The backend's validator is the real gate.
 */
function looksLikeEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

/**
 * Validate sign-in input.
 *
 * Only presence and length. Address *format* is not checked, deliberately:
 * the backend does not check it on login either, because refusing a malformed
 * address differently from a wrong password would answer "does this address
 * have an account?".
 */
export function validateLogin(input: { email: string; password: string }): FieldErrors {
  const errors: FieldErrors = {};

  const email = input.email.trim();
  if (!email) errors.email = "Enter your email address.";
  else if (email.length > MAX_EMAIL_LENGTH) errors.email = "That address is too long.";

  if (!input.password) errors.password = "Enter your password.";
  else if (input.password.length > MAX_PASSWORD_LENGTH) {
    errors.password = `Passwords are at most ${MAX_PASSWORD_LENGTH} characters.`;
  }

  return errors;
}

export interface RegistrationInput {
  email: string;
  password: string;
  organizationName: string;
  firstName: string;
  lastName: string;
}

/** Validate registration input against the same rules the backend applies. */
export function validateRegistration(input: RegistrationInput): FieldErrors {
  const errors: FieldErrors = {};

  const email = normalizeEmail(input.email);
  if (!email) errors.email = "Enter your email address.";
  else if (email.length > MAX_EMAIL_LENGTH) errors.email = "That address is too long.";
  else if (!looksLikeEmail(email)) errors.email = "Enter a valid email address.";

  if (!input.password) {
    errors.password = "Choose a password.";
  } else if (input.password.length < MIN_PASSWORD_LENGTH) {
    errors.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
  } else if (input.password.length > MAX_PASSWORD_LENGTH) {
    errors.password = `Use at most ${MAX_PASSWORD_LENGTH} characters.`;
  } else if (containsEmailLocalPart(email, input.password)) {
    // The backend refuses this too: a password built from the address is
    // guessable from the address.
    errors.password = "Your password must not contain your email address.";
  }

  const organizationName = input.organizationName.trim();
  if (!organizationName) errors.organizationName = "Name your organization.";
  else if (organizationName.length > MAX_ORGANIZATION_NAME_LENGTH) {
    errors.organizationName = `Use at most ${MAX_ORGANIZATION_NAME_LENGTH} characters.`;
  }

  if (input.firstName.trim().length > MAX_NAME_LENGTH) {
    errors.firstName = `Use at most ${MAX_NAME_LENGTH} characters.`;
  }
  if (input.lastName.trim().length > MAX_NAME_LENGTH) {
    errors.lastName = `Use at most ${MAX_NAME_LENGTH} characters.`;
  }

  return errors;
}

function containsEmailLocalPart(email: string, password: string): boolean {
  const localPart = email.split("@", 1)[0] ?? "";
  return localPart.length >= 4 && password.toLowerCase().includes(localPart);
}

export function hasErrors(errors: FieldErrors): boolean {
  return Object.keys(errors).length > 0;
}
