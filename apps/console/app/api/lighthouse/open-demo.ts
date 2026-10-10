/* Open demo: the console signs in for the visitor.
 *
 * When the API has LIGHTHOUSE_OPEN_DEMO_OPERATORS set, its /v1/auth/open-demo
 * route signs a demo operator in server-side and hands back the same session
 * and five-minute credential a person typing a password would get. The proxy
 * then ignores whatever the browser sends and acts as one of those operators,
 * so `approved_by` is still a named account and the database still refuses an
 * approval without one. Only the typing of passwords is removed. Unsetting the
 * variable on the API turns this off; the API answers 404 and the console falls
 * back to its normal sign-in.
 *
 * The operators are tried in order. A 403 is the API's role gate answering
 * before anything is written, so the next operator is tried; that is how a
 * Director approval and a Finance signature both work from one screen, with
 * separation of duties still enforced by the API.
 */

const OPERATORS = [
  "director@lighthouse.jm",
  "clerk@lighthouse.jm",
  "finance@lighthouse.jm",
] as const;

type Minted = { cookie: string; token: string; mintedAt: number };

// The API credential lives five minutes; refresh well before that.
const REFRESH_AFTER_MS = 4 * 60 * 1000;
const PROBE_TTL_MS = 30 * 1000;
const cache = new Map<string, Minted>();
let probe: { enabled: boolean; at: number } | null = null;

async function mint(base: URL, email: string): Promise<Minted | null | "off"> {
  const response = await fetch(new URL("/v1/auth/open-demo", base.origin), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ email }),
    cache: "no-store",
  });
  if (response.status === 404) return "off";
  if (!response.ok) return null;
  const body = (await response.json()) as { session_cookie: string; token: string };
  return { cookie: `lh_session=${body.session_cookie}`, token: body.token, mintedAt: Date.now() };
}

async function credentialFor(base: URL, email: string, fresh: boolean): Promise<Minted | null> {
  const held = cache.get(email);
  if (!fresh && held && Date.now() - held.mintedAt < REFRESH_AFTER_MS) return held;
  const minted = await mint(base, email);
  if (minted === "off" || minted === null) {
    cache.delete(email);
    return null;
  }
  cache.set(email, minted);
  return minted;
}

/** Whether the API has open demo switched on. Probed, then cached briefly. */
export async function openDemoEnabled(base: URL): Promise<boolean> {
  if (probe && Date.now() - probe.at < PROBE_TTL_MS) return probe.enabled;
  let enabled = false;
  try {
    const first = await mint(base, OPERATORS[0]);
    enabled = first !== "off";
    if (first && first !== "off") cache.set(OPERATORS[0], first);
  } catch {
    enabled = false;
  }
  probe = { enabled, at: Date.now() };
  return enabled;
}

/** Send one upstream request as the first operator the API accepts. */
export async function fetchAsDemoOperator(
  base: URL,
  target: URL,
  init: { method: string; headers: Headers; body: string | undefined },
): Promise<Response> {
  let last: Response | null = null;
  for (const email of OPERATORS) {
    for (const fresh of [false, true]) {
      const credential = await credentialFor(base, email, fresh);
      if (!credential) break;
      const headers = new Headers(init.headers);
      headers.set("cookie", credential.cookie);
      headers.set("authorization", `Bearer ${credential.token}`);
      const response = await fetch(target, {
        method: init.method,
        headers,
        body: init.body,
        cache: "no-store",
        redirect: "manual",
      });
      // A stale cached credential: mint once more for this operator.
      if (response.status === 401 && !fresh) continue;
      if (response.status === 403) {
        last = response;
        break;
      }
      return response;
    }
  }
  return last ?? new Response(
    JSON.stringify({ detail: "Open demo could not sign in any demo operator." }),
    { status: 503, headers: { "content-type": "application/json" } },
  );
}
