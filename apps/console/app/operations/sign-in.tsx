"use client";

import { useState } from "react";

import styles from "./operations.module.css";

/* The sign-in gate.
 *
 * Deliberately says almost nothing. A failed sign-in reports one sentence
 * whether the account is missing, inactive or the password is wrong, because
 * the API answers identically in all three cases and a helpful console would
 * undo that.
 */
export function SignIn({
  onSignIn,
}: {
  onSignIn: (email: string, password: string) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ready = email.trim().length > 2 && password.length > 0 && !busy;

  return (
    <form
      className={styles.signIn}
      onSubmit={async (event) => {
        event.preventDefault();
        if (!ready) return;
        setBusy(true);
        setError(null);
        try {
          await onSignIn(email.trim(), password);
        } catch (failure) {
          setError(failure instanceof Error ? failure.message : "Sign-in failed.");
          setPassword("");
        } finally {
          setBusy(false);
        }
      }}
    >
      <span className={styles.eyebrow}>Relief operations</span>
      <h2>Operator sign-in</h2>

      <label className={styles.field}>
        <span>Email</span>
        <input
          type="email"
          value={email}
          autoComplete="username"
          spellCheck={false}
          disabled={busy}
          onChange={(event) => setEmail(event.target.value)}
        />
      </label>

      <label className={styles.field}>
        <span>Password</span>
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          disabled={busy}
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>

      <button type="submit" className={styles.approveButton} disabled={!ready}>
        {busy ? "Signing in…" : "Sign in"}
      </button>

      {error ? (
        <p className={styles.signInError} role="alert">
          {error}
        </p>
      ) : null}

      <p className={styles.noMovement}>
        Signing in opens the queues your role permits. Approving an allocation or
        signing a disbursement asks for your password again.
      </p>
    </form>
  );
}
