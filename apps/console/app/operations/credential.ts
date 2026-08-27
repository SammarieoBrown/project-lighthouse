/* The short-lived operator credential, and the sentences a screen shows when
 * the API refuses one.
 *
 * This lives in its own module for two reasons. The approval rail and the
 * settlement workbench were each carrying their own copy of the 500 sentence,
 * and two copies of the same operator-facing wording is one copy too many.
 * And the five-minute lifetime is a fact about the server that the console
 * asserts on screen — a test can only hold the two in agreement if the number
 * is importable.
 */

/** The lifetime the API mints a step-up credential with. Mirrors
 *  ``_TOKEN_LIFETIME`` in apps/api/app/approval_credentials.py, and
 *  credential.test.mjs reads that file to keep the two honest. */
export const CREDENTIAL_LIFETIME_MS = 5 * 60 * 1000;

/* The API answers a 500 with "internal error" and no more, deliberately: a
 * SQLAlchemy exception carries its bound parameters, so the detail of an
 * intake failure can contain a phone number. That is the right call there and
 * the wrong thing to show an operator, who needs to know what to do rather
 * than that something was internal. */
export function operatorMessage(status: number, detail: string | null): string {
  if (status >= 500) {
    return "The API failed to complete that. Nothing was recorded. "
      + "Try again, and if it repeats the server log has the detail this "
      + "screen deliberately does not.";
  }
  if (status === 401 || status === 403) {
    return detail
      ?? "Your credential does not permit that, or it has expired. "
        + "Confirm your password again.";
  }
  return detail ?? `The API refused that request (${status}).`;
}

/* The status has to survive the throw. A 401 here is not one more refusal to
 * print: it is the credential dying, and the screen has to answer it by
 * putting the password field back. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** True when the API refused because the credential is gone, rather than
 *  because the request was wrong. */
export function credentialIsDead(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

export async function jsonOrDetail(response: Response): Promise<unknown> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body
      ? String((body as { detail: unknown }).detail)
      : null;
    throw new ApiError(response.status, operatorMessage(response.status, detail));
  }
  return body;
}
