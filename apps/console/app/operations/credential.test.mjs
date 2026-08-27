import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ApiError,
  CREDENTIAL_LIFETIME_MS,
  credentialIsDead,
  jsonOrDetail,
  operatorMessage,
} from "./credential.ts";

function source(path) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("a 500 never reaches an operator with the API's own detail", () => {
  // The API's 500 detail can carry bound SQL parameters, and a bound parameter
  // on the intake path is a phone number.
  const message = operatorMessage(500, "psycopg.errors: INSERT INTO claim (phone) VALUES ('8761234567')");
  assert.ok(!message.includes("8761234567"));
  assert.ok(message.includes("Nothing was recorded"));
});

test("a refusal keeps the API's reason, because the API wrote it for a human", () => {
  assert.equal(operatorMessage(409, "That claim is already approved."), "That claim is already approved.");
  assert.match(operatorMessage(404, null), /\(404\)/);
});

test("an expired credential says how to get a new one", () => {
  assert.match(operatorMessage(401, null), /Confirm your password again/);
});

test("the status survives the throw", async () => {
  const response = new Response(JSON.stringify({ detail: "nope" }), { status: 403 });
  await assert.rejects(
    () => jsonOrDetail(response),
    (error) => error instanceof ApiError && error.status === 403,
  );
});

test("a dead credential is told apart from every other refusal", () => {
  assert.equal(credentialIsDead(new ApiError(401, "x")), true);
  assert.equal(credentialIsDead(new ApiError(403, "x")), true);
  assert.equal(credentialIsDead(new ApiError(409, "x")), false);
  assert.equal(credentialIsDead(new ApiError(500, "x")), false);
  // A network failure is not an authorisation answer, and clearing the
  // credential on one would sign an operator out every time the wifi dropped.
  assert.equal(credentialIsDead(new TypeError("Failed to fetch")), false);
});

test("the console's five minutes is the server's five minutes", () => {
  // The console prints an expiry time computed from this constant. If the API
  // ever changes _TOKEN_LIFETIME, that printed time becomes a lie, and the
  // only thing that would notice is this assertion.
  const python = readFileSync(
    new URL("../../../api/app/approval_credentials.py", import.meta.url),
    "utf8",
  );
  const declared = python.match(/^_TOKEN_LIFETIME = timedelta\(minutes=(\d+)\)$/m);
  assert.ok(declared, "could not find _TOKEN_LIFETIME in approval_credentials.py");
  assert.equal(CREDENTIAL_LIFETIME_MS, Number(declared[1]) * 60 * 1000);
});

test("the way back in is on screen whenever the credential is not", () => {
  // The regression this guards: the credential line replaced the password
  // field, nothing cleared the token when the API stopped accepting it, and an
  // operator six minutes into a shift read "Credential active" above a queue
  // that had locked, with the field the error message named nowhere on screen.
  const rail = source("./relief-operations.tsx");

  assert.ok(
    rail.includes("const closeCredential = useCallback("),
    "relief-operations must have one way out of an open credential",
  );

  // Every catch that handles a request carrying the credential has to answer a
  // dead one by closing it. Count the token-bearing fetches against the calls.
  const bearers = rail.match(/authorization: `Bearer \$\{/g) ?? [];
  const closes = rail.match(/credentialIsDead\(error\)/g) ?? [];
  assert.ok(bearers.length >= 5, `expected the rail to carry the credential, saw ${bearers.length}`);
  assert.ok(
    closes.length >= 5,
    `every credentialled path must answer a dead credential; saw ${closes.length} for ${bearers.length} requests`,
  );

  // And the timer has to fire on its own, so an idle screen locks rather than
  // waiting for a click to discover the credential died.
  assert.match(rail, /window\.setTimeout\(close, remaining\)/);
});
