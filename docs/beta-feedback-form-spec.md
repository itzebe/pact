# PACT beta feedback form — specification and setup

The controlled beta uses a **private Google Form** as the ordinary feedback channel. No
Google session is available to this documentation pass, so the form has **not** been created
and no form URL is recorded here. This file is the exact specification the operator uses to
create it; the operator then shares the link directly with testers.

Do not publish the form link, and do not make responses publicly visible.

## Form definition

- **Title:** `PACT Controlled Beta Feedback`
- **Description:** "Thanks for testing PACT. Payments are simulated through PayPal Sandbox —
  no real money moves. Please do not submit passwords, payment details, or personal
  information. For anything that looks like a security vulnerability, use the security
  channel in `SECURITY.md` instead."
- **Confirm message:** "Thanks — your report was received. For an urgent payment problem,
  also contact the operator through the private channel you were given."

## Questions

| # | Question | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| 1 | Tester identifier or chosen alias | Short answer | Yes | An alias is fine; do not require a real name |
| 2 | Test journey | Multiple choice | Yes | Create a job / Offers / Agreement / Payment authorization / Sandbox buyer approval / Capture payment / Verify settlement / Refund / Verify refund / Review history / Other |
| 3 | What happened? | Paragraph | Yes | What you did and what you saw |
| 4 | What did you expect? | Paragraph | Yes | |
| 5 | Error message (optional) | Short answer | No | Paste the error text; do not attach anything containing credentials |
| 6 | Transaction reference (only if safe) | Short answer | No | A job id is enough; never a full credential or authorization secret |
| 7 | Severity | Multiple choice | Yes | Low / Medium / High / Critical |

The form deliberately does **not** ask for passwords, payment credentials, financial details,
or sensitive personal information.

## Settings (Google Forms → Settings)

- **Responses → Collect email addresses:** Off. Use the alias in question 1; collecting email
  is unnecessary and adds personal data.
- **Responses → Allow response editing:** Off.
- **Responses → Limit to 1 response:** Off (a tester reports many journeys).
- **Presentation → Show summary of responses to respondents:** Off. Responses must not be
  visible to other testers.
- **Responses tab:** leave the default; the operator is the only viewer. Do not share the
  responses sheet.
- **Share:** send the form link **directly** to each invited tester. Do not post it publicly.

## Urgent payment issue

The form is for ordinary feedback. Any **urgent payment problem** (a capture or refund that
looks wrong, a duplicate charge, or a job stuck in an unexpected state) must be marked
**Critical** in the form **and** raised through the private channel the operator gave the
tester, so the operator sees it immediately.

## Operator setup steps (Android-friendly)

1. Open <https://docs.google.com/forms/> in a browser and sign in.
2. Create a **Blank form**, set the title and description above.
3. Add the seven questions from the table, with the types and required flags shown.
4. Open **Settings** and set the five options above (no email collection; no response
   summary; no response editing).
5. Tap **Send**, choose the link, and share it privately with the testers.
6. Paste that link into the tester invitation before sending it — see
   [`beta-invitation.md`](beta-invitation.md) and
   [`beta-launch-checklist.md`](beta-launch-checklist.md) §6.

## Security vulnerabilities (separate channel)

A **security vulnerability** is not ordinary feedback and must not go in the form. Use the
security channel: see [`SECURITY.md`](../SECURITY.md). The preferred private route is
GitHub **Private Vulnerability Reporting**, which is **currently disabled** on the repository
and must be enabled by an operator (repository Settings → Security → Private vulnerability
reporting).
