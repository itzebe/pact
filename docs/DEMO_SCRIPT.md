# Pact — demo video

**Final cut:** [`docs/demo/pact-demo.mp4`](demo/pact-demo.mp4) — 2:25, 1600×900, H.264 + AAC, 5.3 MB.

Every frame of application footage is a real capture of the deployed app
(<https://pact-flame.vercel.app/>) and the real PayPal Sandbox checkout page
(`www.sandbox.paypal.com`, test environment). Nothing is mocked, re-enacted, or
generated. The lower-third labels state which environment is on screen.

The video runs against **PayPal Sandbox** with the **live AI provider** and
**Postgres** storage that the production deployment is configured with — the same
stack the health endpoint reports.

## Narration, as recorded

| Time | On screen | Narration |
|---|---|---|
| 0:00–0:10 | Title card | Pact: settlement infrastructure for the agent economy, built on PayPal escrow. This is the live application, running against PayPal Sandbox. |
| 0:10–0:34 | The app, idle; the "Post a job" panel | Agreeing on scope, price, and payment is slow and hard to coordinate, and neither side wants to move first on money. Pact fixes that. A buyer posts a job with a brief, acceptance criteria, and a hard budget ceiling. Seller agents then compete, and an A.I. buyer counters, until they agree inside that budget. The two-hundred-dollar ceiling here is enforced in code. |
| 0:34–0:53 | The completed settlement — negotiation transcript | This is a real, completed settlement. Three seller agents quoted between one hundred and forty and one hundred and fifty dollars. The buyer countered each one, and the price converged to one hundred and twenty-five dollars, under the ceiling, with seller Atlas selected. Every message in the negotiation is recorded. |
| 0:53–1:21 | Escrow record panel, then the real PayPal Sandbox checkout | Now the payment flow. Pact creates a PayPal order with intent authorize, so funds are reserved, not taken. PayPal requires the buyer to approve first, so Pact surfaces that step honestly. This is the real PayPal Sandbox checkout. After approval, Pact authorizes the order, and then captures it. The escrow record shows all three PayPal objects: the order, the authorization, and the capture id. |
| 1:21–1:53 | Verification panel | Before any capture, an independent verifier checks the delivery against every acceptance criterion. The verification panel reports passed, score one point zero zero, with evidence that all acceptance criteria are met. Deterministic checks and model judgement are combined, so a confident-sounding but empty delivery cannot pass. Capture happens only after a passing verdict. A failed verification voids the authorization instead, and duplicate or concurrent requests can never capture the same authorization twice. |
| 1:53–2:15 | Ledger panel — hash chain, "Chain verified" | Every decision lands in a hash-chained ledger. Each entry stores the hash of the one before it, so editing any historical row breaks the chain, and the interface verifies it live. Reputation is what makes this more than a payment wrapper: a seller with a poor record must be substantially cheaper to win the next job, so the market routes around unreliable sellers on its own. |
| 2:15–2:25 | Back to the app home | That is Pact: A.I.-assisted negotiation, conditional PayPal settlement, and a tamper-evident record, running on PayPal Sandbox, with a live A.I. model and durable Postgres storage. |

The closing card points at the live demo and the repository, which is where a judge
should go next.

## Evidence visible in the video

The footage is not narrated over a mock-up. The specific values shown are the
production records served by `GET /api/jobs/job_ca853a58b442`:

- Escrow mode `sandbox`, state `released`
- Order `9BU01766CS027323X`, authorization `7GB0704898318210B`, capture `05962303KN297374V`
- Capture status `COMPLETED`, verification `passed` with score `1.00`
- 28 ledger entries, chain verified
- Seller reputation: `seller_atlas` — 1 settled, 0 voided, $125.00, score 0.67

## Screenshots

The still frames used in the submission are in [`demo/screens/`](demo/screens/).

## How it was produced

See [`DEMO_RECORDING.md`](DEMO_RECORDING.md) for the exact toolchain and commands.
