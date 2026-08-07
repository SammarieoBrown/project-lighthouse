"use client";

import { useCallback, useEffect, useState } from "react";

/* The operator's two tiers, on the console side.
 *
 * A signed cookie carries the shift and lets an operator read the queues their
 * role permits. Approving an allocation or signing a disbursement still demands
 * the password again, and still produces the same five-minute credential the
 * issuing CLI produces — the console holds it in memory for one action and
 * never stores it.
 *
 * The console previously asked an operator to paste a token they had to obtain
 * from a terminal. The guarantee behind that token was real and worth keeping;
 * needing shell access to read a claim queue was not.
 */

export type Operator = {
  email: string;
  display_name: string;
  role: string;
};

export type SessionState =
  | { status: "loading" }
  | { status: "out" }
  | { status: "in"; operator: Operator };

const SESSION = "/api/lighthouse/v1/auth/session";
const STEP_UP = "/api/lighthouse/v1/auth/step-up";

async function detail(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      return String((body as { detail: unknown }).detail);
    }
  } catch {
    // A proxy or gateway failure need not be JSON.
  }
  return fallback;
}

export function useOperatorSession() {
  const [state, setState] = useState<SessionState>({ status: "loading" });

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(SESSION, { cache: "no-store" });
      if (!response.ok) {
        setState({ status: "out" });
        return;
      }
      setState({ status: "in", operator: (await response.json()) as Operator });
    } catch {
      setState({ status: "out" });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signIn = useCallback(async (email: string, password: string) => {
    const response = await fetch(SESSION, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
      throw new Error(await detail(response, "Sign-in failed."));
    }
    setState({ status: "in", operator: (await response.json()) as Operator });
  }, []);

  const signOut = useCallback(async () => {
    try {
      await fetch(SESSION, { method: "DELETE" });
    } finally {
      setState({ status: "out" });
    }
  }, []);

  return { state, signIn, signOut, refresh };
}

/** Re-prove the password and mint one five-minute approval credential. */
export async function stepUp(password: string): Promise<string> {
  const response = await fetch(STEP_UP, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    throw new Error(
      await detail(response, "Could not confirm your password. Try again."),
    );
  }
  const body = (await response.json()) as { token: string };
  return body.token;
}
