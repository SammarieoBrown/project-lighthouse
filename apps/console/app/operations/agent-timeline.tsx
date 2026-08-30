"use client";

import { useEffect, useState } from "react";

import { jsonOrDetail } from "./credential";
import styles from "./operations.module.css";

export type TimelineEvent = {
  at: string | null;
  source: "job" | "verification" | "damage_assessment" | "ledger";
  actor: string;
  title: string;
  state: string | null;
  detail: string | null;
  data: Record<string, unknown>;
};

const at = new Intl.DateTimeFormat("en-JM", {
  timeStyle: "medium",
  timeZone: "America/Jamaica",
});

function figures(event: TimelineEvent): string | null {
  const parts: string[] = [];
  const data = event.data ?? {};
  if (typeof data.confidence === "number") {
    parts.push(`confidence ${data.confidence.toFixed(2)}`);
  }
  if (typeof data.signals_scored === "number") {
    parts.push(`${data.signals_scored} of 5 signals`);
  }
  if (typeof data.estimate_high === "number" && data.estimate_high > 0) {
    const low = typeof data.estimate_low === "number" ? data.estimate_low : 0;
    parts.push(`J$${low.toLocaleString()}–${data.estimate_high.toLocaleString()}`);
  }
  if (typeof data.band === "string") parts.push(String(data.band).toLowerCase());
  if (typeof data.photos_read === "number" && data.photos_read > 0) {
    parts.push(`${data.photos_read} photo${data.photos_read === 1 ? "" : "s"} read`);
  }
  if (typeof data.amount === "string") parts.push(`J$${Number(data.amount).toLocaleString()}`);
  if (typeof data.severity === "string") parts.push(String(data.severity).toLowerCase());
  if (typeof data.attempts === "number" && data.attempts > 1) {
    parts.push(`${data.attempts} attempts`);
  }
  return parts.length ? parts.join(" · ") : null;
}

/* Four sources, one thread: the jobs that ran, what each agent concluded, and
 * what the ledger recorded it as meaning. Read-only by construction — this
 * panel explains a claim, it never changes one. */
export function AgentTimeline({ claimId }: { claimId: string }) {
  const [events, setEvents] = useState<TimelineEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setEvents(null);
    setError(null);
    if (!open) return;
    const controller = new AbortController();
    fetch(`/api/lighthouse/api/claims/${encodeURIComponent(claimId)}/timeline`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(jsonOrDetail)
      .then((body) => {
        if (controller.signal.aborted) return;
        const next = (body as { events?: TimelineEvent[] }).events;
        setEvents(Array.isArray(next) ? next : []);
      })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        setError(failure instanceof Error ? failure.message : "Timeline is unavailable.");
      });
    return () => controller.abort();
  }, [claimId, open]);

  return (
    <div className={styles.timeline}>
      <button
        type="button"
        className={styles.timelineToggle}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "Hide agent record" : "Show agent record"}
      </button>
      {open ? (
        error ? (
          <p className={styles.error} role="alert">{error}</p>
        ) : events === null ? (
          <p className={styles.empty}>Reading the agent record…</p>
        ) : events.length === 0 ? (
          <p className={styles.empty}>No agent has acted on this claim yet.</p>
        ) : (
          <ol className={styles.timelineList}>
            {events.map((event, index) => {
              const summary = figures(event);
              return (
                <li key={`${event.source}-${index}`} data-source={event.source}>
                  <span className={styles.timelineWhen}>
                    {event.at ? at.format(new Date(event.at)) : "—"}
                  </span>
                  <span className={styles.timelineBody}>
                    <b>{event.title}</b>
                    <small className={styles.timelineActor}>
                      {event.actor.replaceAll("_", " ")}
                      {event.state ? ` · ${event.state.replaceAll("_", " ").toLowerCase()}` : ""}
                      {summary ? ` · ${summary}` : ""}
                    </small>
                    {event.detail ? <small>{event.detail}</small> : null}
                    {Array.isArray(event.data?.findings) && event.data.findings.length > 0 ? (
                      <small className={styles.timelineFindings}>
                        {(event.data.findings as Array<Record<string, unknown>>).map(
                          (finding, findingIndex) => (
                            <span key={findingIndex}>
                              Photo {findingIndex + 1}: {String(finding.observed_damage ?? "")}
                              {finding.band ? ` (${String(finding.band).toLowerCase()})` : ""}
                            </span>
                          ),
                        )}
                      </small>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ol>
        )
      ) : null}
    </div>
  );
}
