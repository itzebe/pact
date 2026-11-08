# Judging strategy — PayPal AI Hackathon

This document maps PACT to the official rules and judging criteria of the competition
it is being submitted to. Every criterion is quoted from the official source, and every
PACT claim is tied to a concrete implementation or piece of evidence.

> **Note on demo timestamps.** The timestamps in the mapping table refer to the planned
> structure of the final demo video (see
> [`judge-demo/FINAL_VIDEO_SCRIPT.md`](judge-demo/FINAL_VIDEO_SCRIPT.md)). They describe
> where in the video the evidence appears. The final narrated cut is recorded and public on
> YouTube: <https://www.youtube.com/watch?v=CMGsmqTr6FQ> (2:17). The repository also carries
> the same cut as [`judge-demo/pact-demo-final-narrated.mp4`](judge-demo/pact-demo-final-narrated.mp4),
> a **silent** 0:58 walkthrough at
> [`judge-demo/pact-walkthrough-silent.mp4`](judge-demo/pact-walkthrough-silent.mp4), and an
> earlier narrated demo at [`demo/pact-demo-final.mp4`](demo/pact-demo-final.mp4) (2:52).
> See [`judge-demo/FINAL_VIDEO_CHECKLIST.md`](judge-demo/FINAL_VIDEO_CHECKLIST.md).

---

## 1. Competition overview

| | |
| --- | --- |
| Competition | **PayPal AI Hackathon** — "Build what's next with PayPal and AI" |
| Official site | <https://paypalaihackathon.devpost.com/> |
| Official rules | <https://paypalaihackathon.devpost.com/rules> |
| Sponsor / Administrator | PayPal Inc. / Devpost |
| Format | Global, online, public |
| Submission period | Thu **Oct 1, 2026 9:15 am PT** → Thu **Nov 12, 2026 12:00 pm PT** |
| Judging period | Tue **Dec 1, 2026 2:00 pm PT** → Tue **Dec 15, 2026 8:00 am PT** |
| Winners announced | On or around Mon **Dec 21, 2026** |
| Tracks | **None** — "There are no prescribed problem statements and no set of tracks." |
| Core requirement | "your project must meaningfully use both PayPal and AI" |
| Prize pool | $67,500 in cash, plus credits/subscriptions |
| Video limit | Under 3 minutes, public on YouTube |

### Required deliverables (from the official rules)

| Requirement | PACT status |
| --- | --- |
| Integrate the PayPal developer platform (sandbox) | ✅ PayPal Orders v2 + Payments v2 (authorize / capture / void / refund), Sandbox |
| Meaningfully incorporate AI | ✅ Three LLM roles: seller quoting, buyer countering, independent verification |
| Functional demo judges can run or interact with | ✅ Hosted demo URL **and** one-command Docker build |
| Text description of features and functionality | ✅ [`HACKATHON_SUBMISSION.md`](HACKATHON_SUBMISSION.md) |
| Public GitHub repo, all source + assets | ✅ <https://github.com/itzebe/pact> |
| Open source license visible at top of repo | ✅ MIT (`LICENSE`) |
| Demo video < 3 min, public on YouTube | ✅ <https://www.youtube.com/watch?v=CMGsmqTr6FQ> — 2:17, public |
| Tools list (which tools, how used) | ✅ In the submission copy |

---

## 2. Official scoring criteria

From the Official Rules, Stage Two — five criteria, **equally weighted**:

1. **Technological Implementation** — "How thoroughly and skillfully does the project use PayPal Developer Platform and AI tool(s)? Does the project reflect genuine effort and a working, non-trivial implementation?"
2. **Design** — "Does the project deliver a complete, coherent product experience — not just a technical proof of concept?"
3. **Potential Impact** — "Does the project make a credible, specific case for solving a real problem for a real audience — and does the solution actually address that problem based on what's demonstrated?"
4. **Innovation/Idea** — "How creative and novel is the concept and does the project differ from existing concepts?"
5. **Presentation** — "Does the video clearly demonstrate the project working end-to-end? Does the pitch communicate what problem is solved, who it's for, and why it matters? Is the overall presentation easy to follow?"

Stage One is a pass/fail viability gate: the project must fit the theme and reasonably
apply the required APIs/SDKs.

### Track alignment

There are no tracks to align to. PACT is eligible for the Grand Prizes and the Honorable
Mention prizes, and it is a strong fit for two in particular:

- **Best Use of PayPal + AI** — PayPal escrow is the settlement rail and AI runs three
  agent roles in the same loop.
- **Best Use of Agentic Commerce** — the product *is* an agent-to-agent commerce flow:
  seller agents bid, a buyer agent negotiates, an independent verifier agent decides, and
  the payment moves only on that decision.
- **Best Use of AG Grid** (sponsor, $5,000 / $2,000 / $1,000) — the settlement ledger is a
  real AG Grid (`ag-grid-community` + `ag-grid-react` v36, Quartz theme) rendering
  sortable, resizable audit rows with live hash-chain status.

---

## 3. Rubric mapping

| Judging criterion | What judges reward | PACT implementation | Evidence | Demo timestamp | Remaining gap |
| --- | --- | --- | --- | --- | --- |
| **Technological Implementation** | Skillful, non-trivial use of PayPal + AI | PayPal Orders v2 `intent=AUTHORIZE` → approve → authorize → capture/void, plus refunds; idempotent via `PayPal-Request-Id`; webhook signature verification; three LLM roles (quote / counter / verify) | [`backend/pact/paypal/`](../backend/pact/paypal), `tests/test_paypal_client.py`, `tests/test_escrow.py`, `tests/test_refund.py`; live `/api/health` reports `paypal.mode: sandbox`, `settlement_is_real: true` | 0:45–1:10 | The video must show the real Sandbox approval and the capture id on screen |
| **Design** | Complete, coherent product, not a PoC | One continuous product: guest onboarding → composer → negotiation → payment panel → verification → AG Grid ledger; responsive to 390 px; WCAG-minded contrast and focus | [`judge-demo/screenshots/`](judge-demo/screenshots), live product | 0:10–0:25, 1:10–1:30 | Show mobile briefly so "complete product" is visible |
| **Potential Impact** | Credible case for a real problem and audience | The problem is stated concretely: two strangers, two fears, and the friction of a new account per transaction; the demo shows the workflow actually completing | Problem statement + live journey; [`PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md) | 0:00–0:10, 1:45–2:00 | Name the audience explicitly in narration (agent-to-agent work, freelancer/agent marketplaces) |
| **Innovation/Idea** | Creative, differs from existing concepts | Escrow *conditional on an independent verification verdict*, with the buyer-approval step surfaced honestly; reputation feeds seller selection so the market self-corrects; guest-first, account-less onboarding | README thesis; `backend/pact/core/settlement.py` (verdict-gated capture), seller selection by price/trust | 1:10–1:30 | Avoid "unique in the world" phrasing; state the *combination* |
| **Presentation** | Clear end-to-end video; problem, who it's for, why it matters | The demo script follows the real product states, and the evidence documents separate fresh checks from historical ones | [`judge-demo/FINAL_VIDEO_SCRIPT.md`](judge-demo/FINAL_VIDEO_SCRIPT.md), [`judge-demo/FINAL_VIDEO_TRANSCRIPT.md`](judge-demo/FINAL_VIDEO_TRANSCRIPT.md) | whole video | The video is recorded and public (2:17); keep it under three minutes |

### 3.1 Criterion audit — smallest change that raises the score

Each row is the smallest improvement that would materially raise the score for that
criterion. Anything already done is marked **done**.

| Criterion | Where PACT stands | Actual weakness | Smallest score-raising improvement |
| --- | --- | --- | --- |
| **Technological Implementation** | Real PayPal Orders v2 `intent=AUTHORIZE` → approve → authorize → capture/void + refunds, idempotent; three LLM roles; hash-chained ledger; single-capture under a real DB lock | The strongest proof — a real Sandbox approval and capture id on screen — is only convincing if the judge watches the video | **Done:** the 2:17 video shows the Sandbox approval, the captured settlement, the ledger, and the refund. Keep the evidence pack linked so a judge can click straight to the provider ids. |
| **Design** | One continuous product: guest onboarding → composer → negotiation → payment panel → verification → AG Grid ledger; responsive to 390 px | A judge on desktop may never see the mobile layout | **Done:** screenshots `09`/`10` capture the 390 px layout; the walkthrough names the product states. No further UI change is warranted. |
| **Potential Impact** | Concrete problem (two fears), real workflow that completes, reputation that routes around unreliable sellers | The audience is not named in one place a judge will read | **Done:** the submission copy names agent-to-agent work and small agent/freelancer markets. Do not add impact numbers that are not measured. |
| **Innovation/Idea** | Settlement gated on an independent verdict, buyer-approval surfaced honestly, reputation feeds selection, account-less onboarding | Risk of overclaiming ("first"/"only") | **Done:** the copy states the *combination* and never claims to be first. |
| **Presentation** | A recorded, public 2:17 demo; problem → who → why → pass/fail → why it matters; evidence separated fresh vs historical | The public Devpost page has a few stale facts (see below) | **Owner action:** apply [`devpost-submission-copy.md`](devpost-submission-copy.md) to the Devpost page — it fixes the backend stack ("Node.js" → FastAPI), adds the public video URL, and corrects the reputation status. |

---

## 4. PACT's strongest competitive advantages

1. **Escrow is conditional on a verdict, not on a timer.** Funds are held *before* work
   starts and released only when an independent verifier passes the delivery. A failure
   voids the hold and charges the buyer nothing — shown in code and in the UI.
2. **The buyer-approval step is handled honestly.** PayPal requires the buyer to approve
   an order; PACT surfaces that step rather than pretending a created order is a hold.
   Most "escrow on PayPal" demos skip this and are therefore wrong.
3. **Reputation is load-bearing.** Seller selection divides price by verified-delivery
   trust, so an unreliable seller has to be materially cheaper to win. The market routes
   around failure with no human in the loop.
4. **Guest-first, with real isolation.** A judge uses the real product with no account,
   and guests cannot see each other's records (enforced server-side).
5. **Financial-grade honesty.** Unknown outcomes become `unresolved` for reconciliation,
   never an automatic retry; captures are serialised behind a lock; refunds are capped
   and idempotent; the ledger is hash-chained.

## 5. Weaknesses and limitations (stated plainly)

- **Sandbox only.** No real money moves. The rules *require* the sandbox, so this is
  compliance, not a shortfall — but the video must not imply live payments.
- **Verification is model-backed and non-deterministic.** A delivery can occasionally
  fail verification and end `voided`. The video should show a captured run; the script
  notes this.
- **No dispute/arbitration flow yet.** A disagreement beyond the verdict has no path.
- **The demo video is recorded and public on YouTube** (2:17, under the three-minute limit);
  see §1. If a re-record is ever needed, the script/storyboard/transcript/checklist are
  complete and the recording pipeline is committed under `judge-demo/tools/`.
- **Partial-release is not implemented** (full release or void only).

## 6. Recommended emphasis for the final submission

- **Lead with the tension, not the tech.** Open on the two fears, then reveal the product.
- **Show the real Sandbox approval.** The moment the video shows `www.sandbox.paypal.com`
  and then a real capture id in PACT is the strongest single proof.
- **Spend words on the verdict-gated release.** That is the differentiator, not "escrow".
- **Name the audience out loud:** agent-to-agent work and small agent/freelancer markets
  where opening an account per transaction is disproportionate.
- **Point judges at the evidence pack.** A judge who can click straight to preserved
  identifiers and a passing test suite scores Presentation higher.
- **Submit for the AG Grid prize too.** The ledger is a genuine AG Grid; that is a
  separate, winnable prize.

## 7. Evidence index for judges

- [`judge-demo/JUDGE_WALKTHROUGH.md`](judge-demo/JUDGE_WALKTHROUGH.md) — how to use PACT
- [`judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md`](judge-demo/PRESERVED_VERIFICATION_EVIDENCE.md) — provider ids, tests, CI
- [`judge-demo/screenshots/README.md`](judge-demo/screenshots/README.md) — production captures
- [`FINAL_JUDGE_PACKAGE.md`](FINAL_JUDGE_PACKAGE.md) — single entry point
- [`HACKATHON_SUBMISSION.md`](HACKATHON_SUBMISSION.md) — submission copy
