// What state a proposal is in, and what that means, in words a visitor can follow. Pure functions of the
// proposal's own recorded fields: nothing here reads the system, so every outcome can be tested directly.
import type { ProposalDetail } from "./api";
import type { Tone } from "./ui";

// Where a proposal stands, what that means, and what happens next, in words a visitor can follow.
export const STAGES = ["Proposed", "Verification", "Decision", "Canary", "In production"];
export function lifecycle(d: ProposalDetail): { at: number; tone: Tone; label?: string; meaning: string; next: string } {
  const inCanary = d.state_reason.startsWith("T3");
  const inVerification = /^T[012]/.test(d.state_reason);
  switch (d.state) {
    case "PROPOSED": return { at: 0, tone: "info", meaning: "Queued. A proposal is only data: nothing has been run or changed.",
      next: "The engine takes proposals one at a time and starts with the cheapest checks." };
    case "VERIFYING": return { at: 1, tone: "info", label: "Verifying", meaning: "Being checked: static rules, then the planner, then replays on the digital twin.",
      next: "A twin verdict rests on several replay pairs of about two minutes each, so this takes several minutes." };
    case "ADVISORY": return { at: 1, tone: "", label: "Advisory", meaning: "This kind of action cannot be executed or measured automatically.",
      next: "It is advice for a person; DBPilot will not apply it." };
    case "AWAITING_APPROVAL": return { at: 2, tone: "warning", label: "Awaiting approval", meaning: "Verification passed: the target was shown to benefit and no tenant was shown or suspected to be harmed.",
      next: "An operator approves it for a canary in production, or rejects it." };
    case "INCONCLUSIVE": return { at: 2, tone: "warning", label: "Inconclusive", meaning: "Verification could neither show the change safe nor show it harmful, so it is treated as not safe and is not applied.",
      next: "An admin may override and send it to a canary; an operator may reject it." };
    case "REJECTED": return inVerification
      ? { at: 1, tone: "serious", label: "Rejected in verification", meaning: "A verification tier refused the change. Production was never touched.", next: "Nothing further happens." }
      : { at: 2, tone: "serious", label: "Rejected", meaning: "A member rejected the change. Production was never touched.", next: "Nothing further happens." };
    case "APPROVED": return { at: 3, tone: "info", label: "Queued for canary", meaning: "Cleared to go to production under watch.",
      next: "The engine applies it and starts the canary; one change is in canary per cluster at a time." };
    case "CANARY": return { at: 3, tone: "info", label: "Canary running", meaning: "The change is live in production and each tenant's latency is being compared with its baseline.",
      next: "If a tenant breaches its contract in two of three windows, or telemetry is lost, the change is undone automatically. Otherwise it is kept." };
    case "APPLIED": return { at: 4, tone: "good", label: "Applied", meaning: d.verification === "none" ? "The change was applied and observed without enforcement." : "The canary held for every tenant and the change was kept.",
      next: "It stays in production until an operator rolls it back with the stored inverse." };
    case "ROLLBACK_REQUESTED": return { at: 4, tone: "warning", label: "Rolling back", meaning: "A member asked for the change to be undone.", next: "The engine runs the stored inverse." };
    case "ROLLED_BACK": return inCanary
      ? { at: 3, tone: "serious", label: "Rolled back by the canary", meaning: "The canary undid the change automatically.", next: "Production is back to how it was before the change." }
      : { at: 4, tone: "serious", label: "Rolled back", meaning: "The change was undone with its stored inverse.", next: "Production is back to how it was before the change." };
    case "FAILED": return { at: inCanary ? 3 : 1, tone: "critical", label: "Failed", meaning: "The machinery failed, not the change: see the reason below.",
      next: "Nothing is left applied unless the reason says a rollback failed." };
    default: return { at: 0, tone: "", meaning: "", next: "" };
  }
}

// The tier that refused a proposal, if one did: it never went further, so there is nothing later to show.
export function rejectedAt(d: Pick<ProposalDetail, "steps">): string | null {
  return d.steps.find((s) => s.decision === "REJECT")?.tier ?? null;
}

export const TIER_WORDS: Record<string, string> = { T0: "static rules", T1: "planner what-if", T2: "digital twin", T3: "canary" };

// What a gate verdict means, in words that do not promise more than the gate does.
export function verdictMeaning(decision: string | undefined): { tone: string; text: string } {
  switch (decision) {
    case "APPROVE": return { tone: "good", text: "The target tenant was shown to benefit and every other tenant was shown not to be harmed. The change may go to a canary in production; a person still confirms it unless auto-approve was chosen." };
    case "REJECT": return { tone: "critical", text: "Either a tenant was shown to be harmed, or no benefit could be found. The change is not applied and production was never touched." };
    case "INCONCLUSIVE": return { tone: "warning", text: "The measurements neither show the change safe nor show it harmful. Uncertain is treated as not safe: DBPilot will not apply it by itself. A person may read the evidence and override; that decision is recorded in the audit log." };
    default: return { tone: "", text: "There is no verdict yet." };
  }
}
