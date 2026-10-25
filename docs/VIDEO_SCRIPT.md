# Demo video script

> **Status: superseded.** The video has been produced — see
> [`docs/demo/pact-demo.mp4`](demo/pact-demo.mp4), with the recorded narration in
> [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) and the toolchain in
> [`DEMO_RECORDING.md`](DEMO_RECORDING.md). The plan below is kept for reference;
> the "shot list" at the end remains a good list of future additions.

Target: **2:40** (limit is 3:00). Record at 1080p, browser zoom ~100%, window 1600px wide.
Have the app already running with **two settlements completed** so the reputation panel has history.

Record with OBS or `ffmpeg -f x11grab`. Record the narration separately and lay it over the
screen capture — it is far easier to get a clean take that way.

---

## 0:00–0:15 — The problem

**On screen:** the Pact UI, idle. Cursor resting on the "Post a job" panel.

> "Agents can already negotiate and do work. What they can't do is *pay each other safely*.
> If I let an agent hire another agent, I have no recourse when the work never shows up.
>
> Every attempt to fix this — Circle, ERC-8004, x402 — settles in crypto. That leaves out almost
> all real commerce, which runs on fiat."

---

## 0:15–0:30 — The insight

**On screen:** scroll slowly to the README section on PayPal, or show the escrow record panel.

> "But PayPal already ships the primitive this needs. An order with `intent=AUTHORIZE` *reserves*
> funds instead of taking them. Then you get a real two-way branch: capture to release, or void to
> return. That is escrow.
>
> Pact is the protocol built on top of it."

---

## 0:30–1:50 — The demo (the core of the video)

**On screen:** press **Post job and settle**. Let the ledger stream live.

Narrate as it happens, pointing at the specific numbers:

> "A buyer posts a job with a brief, acceptance criteria, and a hard two-hundred dollar ceiling."

*(sellers begin quoting)*

> "Three seller agents compete. Each quotes, the buyer counters, and the price converges over rounds.
> The ceiling is enforced in *code* — not requested of the model. If a seller agrees to a price over
> budget, the engine rejects it."

*(escrow holds)*

> "Now watch the escrow. Funds are held *before* the work exists — that's the whole point. An
> authorization, not a capture. The seller has committed funds to deliver against. The buyer has
> parted with nothing."

*(delivery, verification)*

> "The seller delivers. An independent verifier checks the artifact against every acceptance
> criterion, and records evidence for each one. Deterministic checks and model judgement are combined
> with AND, so a confident-sounding but empty delivery can't get through."

*(settlement)*

> "Passed — so the authorization is captured. The seller is paid. Every step is in the ledger,
> hash-chained, and the chain verifies live."

**If it fails instead,** that is a *better* clip — do not cut it:

> "This one failed verification. So the hold is voided, and the buyer is charged nothing. Look at
> the escrow record — there's an authorization and no capture."

---

## 1:50–2:25 — The differentiator

**On screen:** point at the **Seller reputation** panel. Then press **Post job and settle** once more
and let it complete, so the audience sees the winner change.

> "Here's the part that makes this more than a payment wrapper.
>
> That seller that just failed verification — its reputation dropped. And reputation feeds back into
> who wins the next job: selection divides price by trust score, so a seller with a bad record has to
> be *substantially* cheaper to compete.
>
> Watch. Same three sellers, same budget. A different seller wins — because the market routed around
> the one that failed. No human touched anything. The failed verification became an economic penalty."

*(this is the single most important 20 seconds of the video — give it room)*

---

## 2:25–2:40 — Close

**On screen:** the ledger tab, scrolled to show chain-verified status. Then cut to the repo.

> "Every settlement decision is in a tamper-evident record a finance team could reconcile against.
> It runs with zero setup — no credentials needed to see the whole thing work — and settles on real
> PayPal sandbox the moment you add them.
>
> That's Pact: settlement infrastructure for the agent economy, on PayPal."

---

## Checklist before uploading

- [ ] Under 3:00. Judges are not required to watch past that.
- [ ] The escrow lifecycle is shown working on screen, not described.
- [ ] The reputation feedback loop is shown changing a *future* outcome — this is the differentiator.
- [ ] At least one settlement visible end to end, with the authorization and capture ids on screen.
- [ ] No third-party trademarks or copyrighted music. Use a royalty-free bed or no music at all.
- [ ] Uploaded to YouTube, set **Public** (not unlisted), link on the submission form.

## Shot list (for a second pass if you have time)

| Beat | Why it earns points |
|---|---|
| Budget guardrail: set the ceiling to $20 and show the engine refuse an over-budget agreement | Technological implementation — proves the guardrail is real, not prompt-hoping |
| The `intent=AUTHORIZE` line in the ledger entry | Shows genuine PayPal API depth |
| Tamper demo: edit a ledger row, chain shows broken | Makes the audit claim concrete |
| The `/docs` OpenAPI page | Signals a real API, not a mock-up |
