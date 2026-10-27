# Transcript upload and parsing

## Flow

```
Student picks the school and uploads a PDF/DOCX   POST /api/transcripts
  -> document + transcript (PENDING) + parse job       dashboard shows "Reading your transcript"
Worker (parse_transcript job)
  1. file safety check, text extraction (scans with no text -> FAILED, staff enter by hand)
  2. remove identity lines (name, DOB, address, ID, email, phone) and redact patterns
  3. LLM gateway (Claude, then Gemini) returns the course list as JSON
  4. every course is checked against the text before it is accepted
  5. for each course (services/transcript_pipeline.py):
       rules engine  -> failed rule?  NOT_ELIGIBLE with the reason, credits 0
       likely-equivalent match in the equivalency database, passing the program rules?
                     -> MATCHED (existing equivalency applied)
       anything else -> AWAITING_SYLLABUS (the syllabus path: AI suggestion, then a department decides)
  6. transcript PARSED; the dashboard fills in by itself
```

The student sees only: upload, a short "reading" banner, then their courses with a status each.

## What the model is and is not trusted with

| Task | Who does it |
|---|---|
| Reading course lines out of messy text | The model |
| Whether a course really is in the document | Code: each course code must appear in the transcript text (ignoring spaces and hyphens) |
| Quarter or semester credits | Code: the school's calendar in the database. The model's claim is ignored |
| Grade, deadline, program rules, credit conversion | Rules engine |
| Existing equivalency lookup | SQL on the equivalency table |
| Any credit decision | Never the model. Matched courses use the existing equivalency table; everything else goes to a department |

A model that is told to add a course ("also list PHYS 999") or makes one up cannot get it past the text check. Repeated lines are collapsed.

## Failure behaviour

| Situation | Result |
|---|---|
| Both AI providers down | Job retried with backoff. After the last retry the transcript is FAILED: "Staff will enter the courses by hand". Nothing is invented |
| Scan with no text, encrypted PDF, .doc, active content | FAILED immediately with a clear reason, without calling the model |
| Same file uploaded twice by the same student | 409, "You have already uploaded this transcript" |
| Model answer fails the schema or text check | One retry, then the next provider |

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/institutions` | Schools a transcript can come from (not BU), with quarter or semester |
| `POST /api/transcripts` | Multipart: `institutionId` and `file`. Returns 201 and the transcript id |
| `GET /api/transcripts/{id}` | Status (PENDING, PARSED, FAILED), error, course count. Owner only |
| `GET /api/students/{id}/dashboard` | Now also lists transcripts that are still being read or failed |

## Limits and assumptions

- **An uploaded file is not an official transcript.** Anyone can edit a PDF. The text check stops the model inventing courses; it cannot detect a forged document. BU's process uses official transcripts sent by the school, so a real deployment needs that channel (or a verification step) before any credit is posted.
- Identity-line removal is pattern based. Names printed on a course line, or in an unusual layout, may still reach the model. Use a provider with a data agreement for real data.
- Only "likely equivalent" matches are applied automatically. The workbook says its matches are preliminary, so confirm with BU that these may be applied without a department's review.
- Credits awarded for a match: semester courses get the BU course's credits; quarter courses get the converted credits, capped at the BU course. The sponsor has not confirmed this (rules.md, assumption 8).
- A student with no BU start date gets a note instead of a deadline check, because the application is created at the first upload. Capture the start date at sign-up.
- Multi-page layouts with tables or columns may extract in the wrong order. Test with real transcripts from the sponsor.
- Courses on one transcript that are the same code and term are treated as one.
