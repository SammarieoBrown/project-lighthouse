import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/* `cookie` carries the operator session upstream and `set-cookie` carries it
 * back. Both are needed because the session is minted by the API but has to
 * live on the console's origin — the browser never talks to the API directly.
 * Nothing else is forwarded in either direction. */
const REQUEST_HEADERS = ["authorization", "idempotency-key", "cookie"] as const;
const MAX_REQUEST_BYTES = 16 * 1024;
const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}";
const CLAIM_DETAIL = new RegExp(`^/api/claims/${UUID}$`);
const CLAIM_EVIDENCE_MEDIA = new RegExp(`^/api/claims/${UUID}/evidence/${UUID}/media$`);
const CLAIM_TIMELINE = new RegExp(`^/api/claims/${UUID}/timeline$`);
const POLICY_REVOKE = new RegExp(`^/v1/auto-approval/policies/${UUID}/revoke$`);
const APPROVAL = new RegExp(`^/v1/claims/${UUID}/allocations/approve$`);
const REVIEW = new RegExp(`^/v1/claims/${UUID}/verification/review$`);
const SIGN_DISBURSEMENT = new RegExp(`^/v1/allocations/${UUID}/disbursements/sign$`);
const EXECUTE_DISBURSEMENT = new RegExp(`^/v1/disbursements/${UUID}/execute$`);
const DAMAGE_REVIEW = new RegExp(`^/v1/claims/${UUID}/damage-assessment/review$`);
const FNOL_PDF = new RegExp(`^/v1/claims/${UUID}/fnol\\.pdf$`);
const DONOR_JOURNEY = new RegExp(`^/v1/public/donations/${UUID}/journey$`);

function apiBase(): URL | null {
  const configured = process.env.LIGHTHOUSE_API_URL?.trim();
  if (!configured) return null;

  try {
    const parsed = new URL(configured);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
    if (process.env.NODE_ENV === "production" && parsed.protocol !== "https:") return null;
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return null;
    if (parsed.pathname !== "/" && parsed.pathname !== "") return null;
    return parsed;
  } catch {
    return null;
  }
}

function allowedPath(method: string, segments: string[]): string | null {
  const path = `/${segments.join("/")}`;
  if (method === "GET" && (
    path === "/api/claims"
    || path === "/v1/public/ledger"
    // Public by design (DON-02, LGR-02): the portal is read by a donor or an
    // auditor who has no account and should not need one.
    || path === "/v1/public/pools"
    || path === "/v1/settlements"
    // Public by design: the live board carries NHC's public product and a
    // posture level, nothing household-shaped.
    || path === "/v1/hazard/live"
    || path === "/v1/auth/session"
    || CLAIM_DETAIL.test(path)
    || CLAIM_EVIDENCE_MEDIA.test(path)
    || CLAIM_TIMELINE.test(path)
    || path === "/v1/auto-approval/policies"
    || DONOR_JOURNEY.test(path)
    || FNOL_PDF.test(path)
  )) return path;
  if (method === "POST" && (
    // Public by design (DON-01): a donor has no account and needs none. The
    // API validates the body; the proxy only opens the door.
    path === "/v1/public/donations"
    || path === "/v1/auth/session"
    || path === "/v1/auth/step-up"
    || APPROVAL.test(path)
    || REVIEW.test(path)
    || SIGN_DISBURSEMENT.test(path)
    || EXECUTE_DISBURSEMENT.test(path)
    || DAMAGE_REVIEW.test(path)
    || path === "/v1/auto-approval/policies"
    || POLICY_REVOKE.test(path)
  )) return path;
  // Sign-out is the only DELETE the console may make.
  if (method === "DELETE" && path === "/v1/auth/session") return path;
  return null;
}

function upstreamUrl(request: NextRequest, segments: string[]): URL | null {
  const base = apiBase();
  const path = allowedPath(request.method, segments);
  if (!base || !path) {
    return null;
  }

  const target = new URL(base.origin);
  target.pathname = path;
  target.search = request.nextUrl.search;
  return target;
}

class ProxyRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function boundedJsonBody(request: NextRequest): Promise<string | undefined> {
  if (request.method === "GET" || request.method === "HEAD") return undefined;
  const contentType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  // Sign-out is a DELETE with nothing to say. Everything else that carries a
  // body still has to declare it as JSON.
  if (request.method === "DELETE" && !contentType) return undefined;
  if (contentType !== "application/json") {
    throw new ProxyRequestError(415, "The console proxy accepts JSON requests only.");
  }

  const declared = request.headers.get("content-length");
  if (declared) {
    const bytes = Number(declared);
    if (!Number.isSafeInteger(bytes) || bytes < 0) {
      throw new ProxyRequestError(400, "Invalid request length.");
    }
    if (bytes > MAX_REQUEST_BYTES) {
      throw new ProxyRequestError(413, "Request body is too large.");
    }
  }

  if (!request.body) return undefined;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_REQUEST_BYTES) {
      await reader.cancel();
      throw new ProxyRequestError(413, "Request body is too large.");
    }
    chunks.push(value);
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new ProxyRequestError(400, "Request body must be valid UTF-8 JSON.");
  }
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params;
  const target = upstreamUrl(request, path);
  if (!target) {
    const configured = apiBase() !== null;
    return NextResponse.json(
      {
        detail: configured
          ? "This Lighthouse API route is not available through the console."
          : "Lighthouse API is not configured for this console deployment.",
      },
      { status: configured ? 404 : 503 },
    );
  }

  const headers = new Headers({ accept: "application/json" });
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  let body: string | undefined;
  try {
    body = await boundedJsonBody(request);
  } catch (error) {
    if (error instanceof ProxyRequestError) {
      return NextResponse.json({ detail: error.message }, { status: error.status });
    }
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }
  if (body !== undefined) headers.set("content-type", "application/json");

  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
    });
    const responseBody = await response.arrayBuffer();
    const responseHeaders = new Headers({
      "cache-control": "no-store",
      "content-type": response.headers.get("content-type") ?? "application/json",
      "x-content-type-options": "nosniff",
    });
    const disposition = response.headers.get("content-disposition");
    if (disposition) responseHeaders.set("content-disposition", disposition);
    /* getSetCookie rather than get: sign-in sends one and sign-out sends
     * another, and a naive get() would silently drop all but the first if that
     * ever became two. */
    for (const cookie of response.headers.getSetCookie?.() ?? []) {
      responseHeaders.append("set-cookie", cookie);
    }
    return new NextResponse(responseBody, {
      status: response.status,
      headers: responseHeaders,
    });
  } catch {
    return NextResponse.json(
      { detail: "Lighthouse API is temporarily unreachable." },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const DELETE = proxy;
