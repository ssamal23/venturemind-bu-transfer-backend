# Transfer credit rules engine

Code: [`app/rules/`](../app/rules/). Pure functions, no database and no AI.
Tests: [`tests/test_rules.py`](../tests/test_rules.py) (65 cases) and the decision-time checks in `test_api_flow.py`.
Rules version: `1.0.0` (stored on each course in `transcript_courses.rules_version`).

## Why rules are code, not AI

The AI can suggest anything. These checks decide what can be awarded. They run again at decision time on live data, so a stale stored result or a persuasive suggestion cannot get credit past them.

## Result types

| Type | Meaning | Blocks credit? |
|---|---|---|
| Pass | Rule satisfied | No |
| Note | Information or a warning for the evaluator | No |
| Fail | Rule broken | **Yes** (for BU equivalent and elective) |

"No credit" and "Request more info" are never blocked by rules.

## Course-level rules (`evaluate_course`)

| Rule | File | Behaviour |
|---|---|---|
| Quarter conversion | `credit_conversion.py` | Quarter credits x 2/3, truncated to 2 decimals (4 becomes 2.66). Semester unchanged |
| Minimum grade | `grade_rule.py` | C or higher passes. C-, D range, F fail. W, I, NP, AU earn no credit and fail. P, S, CR, blank or unknown grades produce a note for the evaluator |
| Transfer window | `deadline_rule.py` | All credits within one year of the BU start date. Fails after the deadline, warns in the last 30 days. A 29 February start falls back to 28 February |
| Hub and GPA | `hub_rule.py` | Always notes: no Hub units, posts as T, no BU GPA effect |

## Program rules (`evaluate_target`), checked against the proposed BU course

| Rule | Source in `program_requirements` | Behaviour |
|---|---|---|
| Non-transferable | `NON_TRANSFERABLE` (CS Group C: 320, 332, 350) | Fail |
| Must take at BU | equivalency database match level `MUST_TAKE_AT_BU` | Fail |
| Transfer cap | `MAX_TRANSFER` (Economics: 4 courses at 200+) | Fail once the student already has 4 credited at that level. Otherwise a note showing how many are used |
| Residency count | `MUST_TAKE_AT_BU` (Biology: 5 BI courses beyond 107/108) | Note only |

## Where it runs

1. Demo seed: `seed_demo.py` computes each course's rule results with the engine.
2. Decision endpoint: `POST /api/transcript-courses/{id}/decision` recomputes course and program rules before recording a BU equivalent or elective. Any failure returns 409 with the reasons.
3. Next: the transcript processing step will call `evaluate_course` when a transcript is parsed, to set `NOT_ELIGIBLE` and `converted_credits`.

## Assumptions to confirm with BU

The sponsor's description is their account, not verified fact. Each of these is a choice made in code.

| # | Assumption | Why it matters | How to test it |
|---|---|---|---|
| 1 | Quarter credits truncate (4 gives 2.66), not round (2.67) | Matches the sponsor's example, but one example cannot prove the rule | Ask for two more worked examples, such as 5 and 1 quarter credits |
| 2 | "C or higher" means C- fails | BU may treat C- differently by school | Ask which schools accept C- |
| 3 | One minimum grade (C) for every school | The brief says "many" schools, not all | Ask for the list of schools with a different minimum. The engine already takes a minimum per call |
| 4 | Pass/fail marks need a person | BU may accept P for some courses | Ask the policy for P, S and CR |
| 5 | The one-year window runs from the BU start date to the date of the decision | It could mean the date the courses were submitted | Ask what date the clock stops on |
| 6 | The 4 "must be taken at BU" courses in the workbook apply to every source school | The workbook lists them against one peer course each | Confirm with the Credit Evaluation Department |
| 7 | Residency counts for Biology are advisory notes only | Enforcing them needs the student's full plan | Decide if a note is enough for the demo |

| 8 | A matched semester course earns the BU course's credits (a 5-credit Northeastern course matched to a 4-credit BU course earns 4). A matched quarter course earns the converted credits, capped at the BU course | The brief only gives the quarter conversion, not what an equivalent earns | Ask for worked examples: a 5-credit lab course, and a 4-credit quarter course matched to a 4-credit BU course |
| 9 | Only "likely equivalent" matches in the workbook are applied automatically | The workbook calls its matches preliminary | Ask whether the equivalency database holds approved matches, and which of them may be applied without review |

## Not covered yet

- Per-school minimum grades (see assumption 3).
- Required-course rules (`REQUIRED_COURSE`) are stored but not used by the engine.
- Equivalencies that combine two source courses into one BU course.
