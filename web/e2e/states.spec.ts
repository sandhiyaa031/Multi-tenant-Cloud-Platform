// What the console says for each verdict and each state a proposal can be in. These are pure functions of
// the proposal's recorded fields (src/lifecycle.ts), so they are tested directly, with no browser: the
// point is that an INCONCLUSIVE verdict can never be worded as an approval, and that APPROVE, REJECT and
// INCONCLUSIVE each mean what the gate means by them.
//
// No twin APPROVE has ever happened on real measurements in this project, so the console's treatment of
// one is checked here, on the wording, and not by showing a made-up verdict on a page.
import { expect, test } from "@playwright/test";
// web/src is mounted read-only at e2e/src by docker-compose.yml, inside this package, so it is transpiled like the tests.
import { lifecycle, rejectedAt, STAGES, TIER_WORDS, verdictMeaning } from "./src/lifecycle";

const proposal = (state: string, extra: Record<string, unknown> = {}) =>
  ({ state, state_reason: "", verification: "full", steps: [], twin_runs: [], canary: null, ...extra }) as any;

test("each gate verdict is worded as what it means", () => {
  const approve = verdictMeaning("APPROVE");
  expect(approve.tone).toBe("good");
  expect(approve.text).toMatch(/shown to benefit/);
  expect(approve.text).toMatch(/not to be harmed/);
  expect(approve.text).toMatch(/a person still confirms/);                       // APPROVE is not "applied"

  const reject = verdictMeaning("REJECT");
  expect(reject.tone).toBe("critical");
  expect(reject.text).toMatch(/not applied/);
  expect(reject.text).toMatch(/never touched/);

  const inconclusive = verdictMeaning("INCONCLUSIVE");
  expect(inconclusive.tone).toBe("warning");
  expect(inconclusive.text).toMatch(/Uncertain is treated as not safe/);
  expect(inconclusive.text).toMatch(/will not apply it by itself/);
  expect(inconclusive.text).not.toMatch(/approved|may go to a canary/i);          // never reads as an approval

  expect(verdictMeaning(undefined).text).toBe("There is no verdict yet.");
  expect(new Set([approve.text, reject.text, inconclusive.text]).size).toBe(3);
});

test("an inconclusive proposal waits for a person and is not applied", () => {
  const l = lifecycle(proposal("INCONCLUSIVE"));
  expect(STAGES[l.at]).toBe("Decision");
  expect(l.tone).toBe("warning");
  expect(l.label).toBe("Inconclusive");
  expect(l.meaning).toMatch(/not safe and is not applied/);
  expect(l.next).toMatch(/admin may override/);
  expect(l.next).toMatch(/operator may reject/);
});

test("a verified proposal waits for approval, and says why it passed", () => {
  const l = lifecycle(proposal("AWAITING_APPROVAL"));
  expect(STAGES[l.at]).toBe("Decision");
  expect(l.meaning).toMatch(/target was shown to benefit and no tenant was shown or suspected to be harmed/);
  expect(l.next).toMatch(/approves it for a canary/);
});

test("a rejection says which side of production it happened on", () => {
  const byTier = lifecycle(proposal("REJECTED", { state_reason: "T1: planner would not use the index" }));
  expect(STAGES[byTier.at]).toBe("Verification");
  expect(byTier.label).toBe("Rejected in verification");
  expect(byTier.meaning).toMatch(/Production was never touched/);

  const byPerson = lifecycle(proposal("REJECTED", { state_reason: "rejected by a member" }));
  expect(STAGES[byPerson.at]).toBe("Decision");
  expect(byPerson.label).toBe("Rejected");
  expect(byPerson.meaning).toMatch(/Production was never touched/);
});

test("the canary's own rollback is told apart from a person's", () => {
  const canary = lifecycle(proposal("ROLLED_BACK", { state_reason: "T3: contract breached in 2 of the last 3 windows" }));
  expect(canary.label).toBe("Rolled back by the canary");
  expect(STAGES[canary.at]).toBe("Canary");
  expect(canary.meaning).toMatch(/undid the change automatically/);

  const person = lifecycle(proposal("ROLLED_BACK", { state_reason: "rolled back on request: tidy up" }));
  expect(person.label).toBe("Rolled back");
  expect(STAGES[person.at]).toBe("In production");
});

test("applied, in canary, failed and advisory each say what they are", () => {
  expect(lifecycle(proposal("APPLIED")).tone).toBe("good");
  expect(lifecycle(proposal("APPLIED")).meaning).toMatch(/canary held for every tenant/);
  expect(lifecycle(proposal("APPLIED", { verification: "none" })).meaning).toMatch(/without enforcement/);
  expect(lifecycle(proposal("CANARY")).meaning).toMatch(/live in production/);
  const failed = lifecycle(proposal("FAILED"));
  expect(failed.tone).toBe("critical");
  expect(failed.meaning).toMatch(/machinery failed, not the change/);
  expect(lifecycle(proposal("ADVISORY")).meaning).toMatch(/cannot be executed or measured automatically/);
});

test("every state of the proposal state machine has an explanation and a stage", () => {
  const states = ["PROPOSED", "VERIFYING", "REJECTED", "INCONCLUSIVE", "AWAITING_APPROVAL", "APPROVED", "ADVISORY", "CANARY",
    "APPLIED", "ROLLBACK_REQUESTED", "ROLLED_BACK", "FAILED"];
  for (const state of states) {
    const l = lifecycle(proposal(state));
    expect(l.meaning.length, `${state} has an explanation`).toBeGreaterThan(20);
    expect(l.at, `${state} sits on a stage`).toBeGreaterThanOrEqual(0);
    expect(l.at).toBeLessThan(STAGES.length);
  }
});

test("the tier that stopped a proposal is read from its recorded steps", () => {
  expect(rejectedAt({ steps: [{ tier: "T0", decision: "APPROVE" }, { tier: "T1", decision: "REJECT" }] } as any)).toBe("T1");
  expect(rejectedAt({ steps: [{ tier: "T0", decision: "APPROVE" }, { tier: "T2", decision: "INCONCLUSIVE" }] } as any)).toBeNull();
  expect(rejectedAt({ steps: [] } as any)).toBeNull();
  expect(TIER_WORDS.T1).toBe("planner what-if");
});
