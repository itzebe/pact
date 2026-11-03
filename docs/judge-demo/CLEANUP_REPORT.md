# Cleanup report — temporary verification jobs

Prepared **after** documentation and screenshot capture, per the required order
(documentation → screenshots → evidence → cleanup → final verification). Evidence
preservation took priority over cleanup at every step.

**Outcome in one line:** no records were deleted. The verification artifacts are
linked to preserved payment evidence, their ownership cannot be established, and the
application exposes **no guest-facing cleanup workflow** — so cleanup was skipped for
every candidate, and the records were left untouched.

---

## 1. What cleanup functionality actually exists

The application's own surfaces were inspected before deciding anything:

| Surface | Behaviour | Usable for cleanup here? |
| --- | --- | --- |
| `DELETE /api/jobs/{id}` | **Requires an admin credential** (`require_admin`); also refuses (`409`) unless the escrow is in a terminal state. | No — admin-only, and this task must not provision or use operator/admin credentials. |
| `POST /api/jobs/{id}/cancel` | Owning buyer (or admin). **Voids** an outstanding hold and confirms it with PayPal; a job whose money has moved, or whose outcome is unknown, is refused (`409`). | No — the verification jobs already have a *terminal* financial outcome (settled/refunded/voided); cancelling does not remove them, and one cannot "un-capture". |
| `POST /api/auth/guest/logout` | Clears the guest cookie. Idempotent. | No — it ends a *session*, not the workspace's data. Jobs persist. |
| "New workspace" (UI) | Calls `endGuestSession()` (logout) then provisions a new guest. | No — same as above; the old workspace's jobs remain. |
| Guest session TTL (`PACT_GUEST_SESSION_TTL_HOURS`, default 12) | Expires the *session*. | No — there is **no retention sweep** that deletes guest jobs when a session expires. Jobs persist. |
| Admin/operator tooling | Reconciliation, deletion, global views. | Out of scope for this task; not used. |

Confirmed by reading the code and the frontend:

- `backend/pact/api/routes.py` — `delete_job` depends on `require_admin`; `cancel_job`
  depends on `require_buyer` and voids holds rather than deleting records.
- `frontend/src/lib/api.js` defines `cancelJob` and `deleteJob`, but **no component
  calls them** — the UI offers no delete or cancel affordance to a guest.
- `backend/pact/core/guests.py` and `backend/pact/config.py` define provisioning,
  quota (`guest_max_jobs`), per-IP limits, and a session TTL — but **no deletion or
  retention mechanism**.

There is therefore **no supported application workflow** by which a guest can remove
its own verification jobs. Direct SQL deletion and undocumented admin endpoints are
prohibited by this task, so they were not used.

---

## 2. Candidates identified

Because no durable record of the verification guests' identifiers exists (the guest
cookie is a session artifact, and guest ids are ephemeral), candidates are grouped
into the classes that can actually be reasoned about.

| # | Candidate class | Created by verification? | Ownership established? | Linked to a payment? | Part of preserved evidence? |
| --- | --- | --- | --- | --- | --- |
| C1 | The **refunded** job from the earlier verification (job id recorded as `job_0cd3d0b02139`) | Yes | No (guest workspace id not durably recorded) | Yes — order `60E54463RB1559702`, authorization `0N5736387U1180942`, capture `2YX75520SD675715C`, refund `3UB477660D466340R` | **Yes** — this is the core lifecycle evidence |
| C2 | The **captured** jobs from the earlier verification (`job_6249f90453c2`, `job_ffdaf05f5a29`) | Yes | No | Yes — capture `0MA99158MK626023P`, order `3FE25151GL963315G` | Yes — corroborates refund cap / partial refunds |
| C3 | The jobs created during **screenshot capture** (this session) — one settled+refunded, one settled+refunded | Yes | No | Yes — real Sandbox capture and refund | Yes — they underpin screenshots `06`–`08` |
| C4 | The **guest identities/workspaces** created by verification (including ones whose sessions expired) | Yes | No — ephemeral, not durably enumerated | Indirectly | No |

---

## 3. Classification

| Class | Classification | Reason |
| --- | --- | --- |
| C1 refunded job | **PRESERVE FOR JUDGING** | Directly linked to the preserved lifecycle identifiers. Deleting it would destroy the evidence the package points at. |
| C2 captured jobs | **PRESERVE FOR JUDGING** | Linked to real capture/refund objects; corroborate the refund-cap and partial-refund protections. |
| C3 screenshot jobs | **PRESERVE FOR JUDGING** | Linked to real Sandbox captures/refunds and to screenshots `06`–`08`; removing them would make the screenshots unverifiable against a live record. |
| C4 guest identities | **OWNERSHIP OR PROVENANCE UNCERTAIN** | The guest ids were never durably recorded; the workspaces cannot be enumerated without global/admin access, and some sessions have already expired. Deleting a workspace that might own a preserved job is unsafe. |
| All classes (deletion mechanism) | **CLEANUP NOT SUPPORTED** | No guest-facing deletion workflow exists; the only deletion path is admin-only, which this task must not use. |

---

## 4. Decision

| Metric | Count |
| --- | --- |
| Candidate classes identified | 4 |
| Safely cleaned up | **0** |
| Preserved for judging | **3** (C1, C2, C3) |
| Skipped — ownership/provenance uncertain | **1** (C4) |
| Skipped — cleanup not supported | **All classes** (no guest-facing workflow) |

### Reason for every skipped cleanup

- **C1, C2, C3 (preserved):** these records *are* the evidence. Deleting them would
  remove the only live records behind the preserved provider identifiers and the
  settlement/refund screenshots. Evidence preservation takes priority over cleanup.
- **C4 (uncertain):** the guest workspaces cannot be enumerated safely, and some may
  own preserved jobs. Deleting on a guess risks destroying evidence and touching
  records whose provenance cannot be established. The task's rule applies directly:
  *if ownership or provenance cannot be established, leave it untouched.*
- **All classes (not supported):** even where a record were safe to remove, the
  application provides no guest-facing cleanup workflow. The only deletion route
  requires an admin credential, which this task explicitly must not obtain or use, and
  direct SQL manipulation is prohibited.

### Why this is the correct outcome

- No production data, transaction evidence, ledger/audit entries, or user-created jobs
  were deleted.
- No authorization was bypassed, no admin/operator credential was used, and no direct
  SQL was issued.
- The verification jobs are harmless on a Sandbox deployment: they hold **no real
  money** and their financial exposure is fully resolved (settled, refunded, or
  voided). They remain available as inspectable evidence.
- Guest workspaces continue to age out of *usability* by session TTL, while their
  data is retained for judging — which is exactly the property a demo wants.

---

## 5. If the owner wants the verification jobs removed later

This is a recommendation for the owner, **not** something performed here:

1. The supported path is an **operator/admin** credential, then, per job:
   `POST /api/jobs/{id}/cancel` (void any hold) followed by
   `DELETE /api/jobs/{id}` once the escrow is terminal. Jobs that were captured and
   refunded are already terminal and could be deleted this way.
2. Any such deletion should be **deferred until after judging**, so the records behind
   the preserved identifiers and the screenshots remain inspectable.
3. It should not be attempted for any workspace whose ownership is not first
   established from an authoritative record.
