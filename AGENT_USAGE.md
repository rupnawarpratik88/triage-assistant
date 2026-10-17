# Agent Usage

This document describes how AI tools were used to build the Equipment Maintenance Triage Assistant, what was delegated, where the agent was wrong, and how the output was verified.

## 1. Tools

| Tool | Purpose |
|---|---|
| Claude (chat assistant) | Main coding agent: design proposals, code, tests, documentation, debugging help |
| VS Code, Git, GitHub | Development, small descriptive commits, public repository |
| Render | Hosting of the deployed application |
| Google AI Studio | Free Gemini API key |
| Gemini (`gemini-3.5-flash`) | The LLM **inside the product** (triage analysis). It is part of the application, not a development tool |

## 2. Division of responsibility

**Delegated to the agent (draft):** architecture proposal, database models, rules engine, retrieval module, AI prompt and output validation, HTML templates, test suite, README, deployment instructions.

**Owned by me:** choosing the problem and scope, setting up the environment, running every step, reading and running the tests, debugging environment and deployment problems, making the commits, deploying, and testing the live application. I am responsible for everything submitted and can explain every module.

## 3. Representative prompts

Prompts are condensed and edited for clarity. Each one produced or shaped a part of the submitted code.

| Area | Prompt | Output used |
|---|---|---|
| Scoping and architecture | "Given two problem statements and a limited time budget, assess scope, risk and scoring weight, recommend one problem to submit, and propose an architecture and stack. Prefer a complete, deployable solution over broader scope." | Chose the medium problem; FastAPI, SQLite, BM25 retrieval, Gemini |
| Data model | "Design SQLAlchemy models for a maintenance triage application: equipment, reports (description, recent events, optional JSON sensor readings, AI status and error), findings with a kind (observation, possible cause, confirmed finding) and an origin (rule, ai, technician), work orders with a draft, approved or rejected status, and an audit log. Only a technician may create a confirmed finding." | `models.py` |
| Deterministic rules | "Implement a deterministic threshold engine configured from JSON per equipment type. Return ok, warning or critical for each reading with the manual section reference. Detect missing, non-numeric, non-finite and implausible readings, and conflicts between the issue description and a normal sensor value, and report them as data quality flags instead of ignoring them. Add unit tests for boundaries and each data quality case." | `rules.py`, `thresholds.json`, `test_rules.py` |
| Retrieval | "Chunk the markdown manuals by numbered section into stable ids such as pump:2. Implement BM25 retrieval limited to the equipment type's manual, always include the safety section, and raise a typed error when nothing relevant is found. Persist the retrieved sections or the error on the report, and show failures in the UI while keeping the threshold checks visible." | `retrieval.py`, failure banner |
| AI workflow | "Integrate the Gemini REST API. Treat report text as untrusted data and supply only the retrieved manual sections and rule results as evidence. Require strict JSON with possible causes, follow-up questions, inspection steps, a priority and a draft work order, each with citations. Validate with Pydantic, reject output that cites evidence which does not exist, retry once with the rejection reason, and otherwise raise a readable error." | `ai.py` |
| Priority and work order | "Apply a deterministic priority floor: a critical rule result forces at least High and a warning at least Medium. Show when the rules raised the AI's priority. Create a draft work order whose tasks carry their citations and which is clearly marked as not approved." | `resolve_priority`, work order draft |
| Human review | "Implement technician-only routes to edit, approve and reject a draft work order. Validate input, lock approved and rejected orders with a 409, record every change with before and after values in an audit log, and add a route for technicians to record confirmed findings. Add per-equipment history pages." | `workflow.py`, work order and history pages |
| Reliability and logging | "Retry temporary AI errors (429, 503, timeouts) with short delays. Add a JSON log formatter and structured events for report creation, retrieval, each AI HTTP attempt with status and latency, output rejection, and technician decisions, without logging secrets or report text." | Retry in `ai.py`, `logging_setup.py` |
| UI states | "Add loading states on form submit, whitelisted success notices after redirects, empty states, validation messages, failure banners and friendly HTML error pages, while keeping JSON errors on API routes." | Templates, `main.py` error handlers |
| Testing | "Write pytest tests with the LLM mocked: citation validation, invalid JSON, retry, rate limit and timeout handling, approval flow and locking, retrieval failure, structured logging and UI states. Use an in-memory SQLite database through a dependency override." | `tests/` |
| Delivery | "Provide an idempotent seed script with demo reports covering critical, conflicting, partial and no-sensor cases, a README with architecture, scope and limitations, and Render deployment configuration." | `scripts/seed.py`, README, `render.yaml` |
| Debugging | "Review this stack trace and test output, identify the root cause, and propose the smallest fix with a regression test." | Fixes for model name, API overload, redirect test failures |

## 4. Design prompts

Detailed prompts written during design of the system. The submitted application is a simplified, server-rendered implementation (see README).

### Prompt 1: data models, schemas and sensor data handling

```text
Act as a Principal Software Engineer. I am building an "Equipment Maintenance Triage Assistant" with a Python/FastAPI backend and a React frontend. The app lets technicians report equipment issues, combines sensor data with a RAG-based knowledge base (manuals/fault codes), and drafts a work order.

Provide the complete SQL Alchemy data models and Pydantic schemas for the following entities, ensuring strict type-safety:
1. Equipment: ID, Type (HVAC, Pump, Motor), Model, Location, Status.
2. TriageSession: ID, EquipmentID, ReportedIssueDescription, RecentOperatingEvents (text), SensorReadings (JSON blob for flexible metrics), CreatedAt, Status (Open, Draft_Generated, Approved, Rejected).
3. WorkOrderDraft: ID, TriageSessionID, SuggestedPriority (Low, Medium, High, Critical), TechnicalFindings (JSON containing separate fields for Observations, PossibleCauses, ConfirmedFindings), InspectionSteps (List), DraftText, EditedByTechnician (Boolean), Logs (JSON for AI/Retrieval failure tracking).
4. MaintenanceHistory: Historical log linking Equipment to past approved work orders.

Include a database migration setup outline and write a utility function to handle missing or conflicting sensor data (e.g., if a temperature reading is null, or if pressure is conflicting, log a warning and mark it as 'untrusted' in the schema). 

Provide clean, modular code following standard backend best practices. No placeholders.
```

### Prompt 2: deterministic rule engine

```text
Write a deterministic rule engine module in Python for our Triage Assistant. The application requires strict threshold checks that override or supplement AI logic.

Create a function `evaluate_equipment_thresholds(equipment_type: str, sensor_readings: dict) -> dict` that checks incoming data against these strict rules:
1. Pumps: If 'vibration_mm_s' > 7.5 OR 'bearing_temp_c' > 90 -> Set Priority to 'Critical', add strict instruction: "Immediate physical lockout/tagout (LOTO) required. Inspect bearing seals."
2. Motors: If 'current_draw_amps' > 150% of nominal OR 'winding_temp_c' > 130 -> Set Priority to 'Critical', add instruction: "Check for phase imbalance and insulation breakdown."
3. HVAC: If 'refrigerant_pressure_psi' < 30 -> Set Priority to 'High', add instruction: "Check for refrigerant leaks."

The output must return a dictionary containing:
- `is_critical_trigger` (bool)
- `deterministic_priority` (str)
- `mandatory_inspection_steps` (list of strings)
- `rule_violations` (list of strings explaining which threshold was crossed)

Include comprehensive unit tests using pytest to verify boundary conditions and handling of missing fields.
```

### Prompt 3: triage system prompt and guardrails

```text
You are an expert Equipment Maintenance Triage System. Your job is to analyze a technician's problem report, evaluated sensor data, and relevant manual context to generate a structured maintenance triage report.

Strict Guardrails:
1. Distinguish between 'Observations' (raw data/facts), 'Possible Causes' (hypotheses), and 'Confirmed Findings' (only what is explicitly verified). 
2. NEVER present a cause as definitive or confirmed. Use language like "Possible cause under investigation:..."
3. You have NO remote control capabilities. You cannot automatically approve work orders.
4. You must cite specific sections of the manual context or specific sensor events for every single suggestion.

Input Data:
- Equipment Type: {equipment_type}
- Issue Description: {issue_description}
- Recent Operating Events: {recent_events}
- Sensor Status: {sensor_analysis}
- Retextual Manual Context: {manual_chunks}

Generate a JSON response matching this schema precisely:
{
  "observations": ["List of clear, objective facts from the input"],
  "possible_causes": [
    {
      "cause": "Description of possible cause",
      "probability": "Low/Medium/High",
      "evidence_citation": "Quote or source from manual/events"
    }
  ],
  "targeted_follow_up_questions": ["2-3 specific questions the tech should investigate to narrow down the cause"],
  "suggested_inspection_steps": ["Step-by-step checklist for physical inspection, citing the manual where applicable"],
  "proposed_priority": "Low/Medium/High/Critical",
  "priority_justification": "Why this priority was chosen based on evidence",
  "draft_work_order": {
    "title": "Standardized short title",
    "summary": "Technical summary of what needs to be edited/approved by the technician"
  }
}

If the manual context is missing, conflicting, or the retrieval system failed, populate an "ai_retrieval_failure" key inside the JSON explaining the gap, but still provide safe, standard safety-first triage steps based on the issue description.
```

### Prompt 4: React workspace component

```text
Create a Tailwind-styled React component named `TriageWorkspace.jsx` for an equipment maintenance app. 

The UI must be split into two main sections:
1. Left Panel (The Input & Context):
   - Displays Equipment ID and Type.
   - Displays "AI Diagnostics" grouped clearly into visual blocks using functional icons/colors:
     - Observations (Gray background, factual list)
     - Possible Causes (Yellow alert-style background, explicitly labeled "Unconfirmed Hypotheses")
     - Targeted Follow-up Questions (Blue background)
   - If there is an AI or Retrieval failure, show a prominent red warning banner: "System Retrieval Warning: Manual context unavailable. Displaying generic safety triage."

2. Right Panel (The Actionable Work Order):
   - A fully editable form pre-populated with the `draft_work_order.title`, `proposed_priority`, and `suggested_inspection_steps` (as an editable list or text area).
   - A checkbox for the technician to explicitly assert: "I have verified these findings on-site."
   - Three distinct buttons at the bottom: "Save Progress", "Reject Draft", and "Approve & Create Work Order".

Implement state handling using standard React hooks so the technician can modify any field in the work order before clicking "Approve". Ensure a clean, highly readable, industrial UI layout suitable for a tablet interface used by a field tech.
```

### Prompt 5: finalize endpoint

```text
Write a FastAPI router endpoint `/api/triage/{session_id}/finalize` that processes the technician's final action on a work order draft.

The endpoint must handle three actions submitted via the payload: 'APPROVE', 'REJECT', or 'SAVE_DRAFT'.

Implement the following backend logic:
1. Fetch the existing `TriageSession` and `WorkOrderDraft`.
2. If the action is 'APPROVE':
   - Update `TriageSession.status` to 'Approved'.
   - Save the edited text, priority, and verified findings exactly as submitted by the technician into `WorkOrderDraft`.
   - Append a new record to the `MaintenanceHistory` table documenting the snapshot of this equipment's state, what the AI suggested vs. what the human technician finally approved (to maintain an audit trail).
3. If the action is 'REJECT':
   - Update `TriageSession.status` to 'Rejected'.
   - Log the technician's mandatory rejection reason into the `WorkOrderDraft.logs`.
4. Ensure the entire operation runs within a database transaction block so that any failure rolls back the state completely. Include explicit error logging for any database exceptions.
```

## 5. Agent mistakes, corrections and rejected suggestions

| Issue | What went wrong | Resolution |
|---|---|---|
| Platform commands | Early setup instructions did not match Windows PowerShell and the Python launcher on the development machine | Commands corrected after they failed |
| Git workflow | First commit instructions did not connect the GitHub remote or push, and grouped all changes in one commit | Requested small, descriptive commits and a pushed remote; process changed |
| Gemini model name | The default model name returned HTTP 404 for the API key | Located a working model, set it as default, and made it configurable with `GEMINI_MODEL` |
| API overload | The first AI version failed the whole analysis on a 503 | Added retry with short delays for 429, 503 and timeouts, plus tests |
| UI change broke tests | Adding success notices changed redirect URLs; 11 tests that built URLs from the redirect failed | `pytest` caught it; the test helper was fixed |
| Critical limits | The manuals define only warning levels | Critical limits are my own documented assumptions (see README limitations) |
| Rejected: expert problem | Data migration workbench was too large for the time available | Submitted the medium problem completely |
| Rejected: vector database | Unnecessary for small manuals | BM25 over sections: transparent and testable |
| Rejected: React frontend and extra tables | More code and more risk than the time allowed | Server-rendered pages and a smaller schema |
| Rejected: trusting model output | A model could give an unsafe priority or invent citations | Deterministic priority floor and code-level citation validation |

## 6. Verification

- **Automated tests:** 66 tests pass with `pytest`. The LLM is mocked, so tests need no key. They cover threshold rules, missing, invalid and conflicting data, retrieval and retrieval failure, AI output validation (bad JSON, missing or unknown citations), retries, priority floor, approval flow and locking, confirmed findings, history, structured logging, UI states and error pages.
- **Manual checks:** ran the application locally for the main cases (critical threshold, conflicting data, missing readings, no sensor data) and a real Gemini analysis returning cited causes, inspection steps and a draft work order.
- **Safety constraints:** confirmed by tests that a new work order is always a draft and that the AI path creates no confirmed findings. Approve, reject and confirm routes are technician-only.
- **Logs:** checked that report creation, retrieval, each AI call (status, latency), validation rejections and technician decisions are logged as JSON lines.
- **Secrets:** `.env` is in `.gitignore`; the API key exists only locally and in Render environment settings.
- **Deployment:** deployed to Render and opened the live site to check the form, history and seeded demo data.
