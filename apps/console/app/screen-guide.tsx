import styles from "./screen-guide.module.css";

/* A short orientation under each masthead: what the screen is, then what to do.
 * It collapses, and the screen works the same without it. */
export function ScreenGuide({
  what,
  steps,
  defaultOpen = true,
}: {
  what: string;
  steps: readonly string[];
  defaultOpen?: boolean;
}) {
  return (
    <details className={styles.guide} open={defaultOpen}>
      <summary className={styles.summary}>
        <span className={styles.what}>{what}</span>
        <span className={styles.toggle} aria-hidden="true" />
      </summary>
      <ol className={styles.steps}>
        {steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
    </details>
  );
}
