# Credit office: posting results

Code: [`services/posting.py`](../app/services/posting.py), [`api/postings.py`](../app/api/postings.py), and the screen, [`CreditOfficePosting.tsx`](https://github.com/ssamal23/venturemind-bu-transfer-frontend/blob/main/src/pages/CreditOfficePosting.tsx) in the frontend repository.
Tests: [`tests/test_posting.py`](../tests/test_posting.py) (32 cases).

## Where it fits

Deciding and posting are two steps by two roles. A department decides what a course is worth. The credit office posts that result to the academic record as grade **T** (BU's step 7). The AI never reaches this step.

```
decided by a department (DECIDED) ─┐
                                   ├─> credit office checks and posts ─> POSTED
existing equivalency (MATCHED) ────┘
```

Courses that are not eligible, still waiting for a syllabus, or in review are never postable.

## What the screen does

- **Waiting to be posted:** every decided or matched course with its result, credits, and basis (the department and date, with the evaluator's reason, or "existing equivalency").
- **Ready or Blocked:** a course is Blocked if a course-level rule no longer passes (grade, one-year window). Blocked rows cannot be selected.
- **Post one or many:** tick courses, then "Post selected". A confirmation lists each course and the total credits before anything changes.
- **Result banner:** how many were posted, and the reason for each that was not.
- **Recently posted:** the last 50 postings, who posted them, and when.

## Safeguards

| Safeguard | How |
|---|---|
| Only the credit office can post | Role check on every endpoint. Students and evaluators get 403 |
| Rules run again at posting | Grade, quarter conversion, one-year window, and the program rules (Group C, caps, residency), on live data. A result that was valid when decided can stop being valid |
| Posting twice is harmless | One posting per course (unique). A second request returns the first posting with `alreadyPosted: true` |
| No double posting under a race | The course row is locked while posting |
| One bad course never blocks a batch | Each course posts in its own savepoint. Failures are reported per course |
| Retry-safe | Optional `Idempotency-Key` header, stored on the posting. A key reused for another course is refused |
| A posting is never edited or deleted | Database trigger on `postings`, like the audit log and decisions |
| A posting is a snapshot | It stores the outcome, BU course, credits and grade at that moment |
| Everything is audited | `posting.created` with the result, credits, grade and basis, plus the status change |
| Students see it | Status becomes "Posted to record", with a timeline entry such as "Posted to your academic record: CAS CS 111, 4 credits, grade T" |

## Endpoints (credit office only)

| Endpoint | Purpose |
|---|---|
| `GET /api/postings/queue` | Courses waiting to be posted, with blockers |
| `POST /api/transcript-courses/{id}/post` | Post one course. Optional `Idempotency-Key` header |
| `POST /api/postings/batch` | Body `{"courseIds": [...]}`, 1 to 100 ids. Returns per-course results and totals |
| `GET /api/postings?limit=50` | Posting history, newest first |

## Database

Migration `0003` makes `postings` a self-contained snapshot: it adds `transcript_course_id` (unique), `outcome` and `bu_course_id`, makes `decision_id` optional (an existing equivalency has no decision), and adds the append-only trigger. `0004` renames a check constraint in databases that ran the first version of `0003`. Both migrations check what already exists, so they work on a brand-new database and on this one (verified: upgrade, downgrade to 0001, upgrade again, on a scratch database).

## Assumptions to confirm with BU

| # | Assumption | How to test it |
|---|---|---|
| 10 | Existing equivalencies are posted by the credit office without a department decision | Ask whether applied equivalencies need a second person's check before posting |
| 11 | A "no credit" result is posted as a T record with 0 credits, so the course closes | Ask whether zero-credit results are recorded in MyBU Student or only communicated |
| 12 | The one-year window applies on the date of posting, not only the date of the decision | Ask which date stops the clock |
| 13 | There is no separate approver for batch posting | Ask whether large batches need a second sign-off |

## Limits

- **The posting is recorded here, not sent anywhere.** There is no integration with MyBU Student or the registrar. "Posted" means our record says it was posted. A real deployment needs that connection, and the posting should happen only after it succeeds.
- **No reversal.** A posting can't be edited or removed. A correction process (a reversing entry) is not built.
- **Speed over a distant database.** Each course re-checks the rules with about a dozen queries. From a laptop to Azure this took roughly 3 seconds per course; a batch of 100 could approach several minutes. With the API and database in the same region it is far faster, but add background processing before using large batches in production.
- **Queue blockers are partial.** The list shows course-level blockers only (grade, one-year window) to stay fast. Program rules (Group C, caps) are checked when you post, and a failure is reported then.
- **Separation of duties** is by role only. Nothing stops one person from holding both the evaluator and credit office roles.
- **Display:** students see one "Posted to record" status for matched and decided courses alike.
