# Agent usage

## Tools

- **Claude (chat)**: main coding assistant. It drafted the code, tests and documentation; I ran, tested and committed everything.
- **VS Code, Git and GitHub**: editing, small commits and the public repository.
- **Render**: hosting. **Google AI Studio**: free Gemini API key for the in-app AI step.
- **Gemini (`gemini-3.5-flash`)** is the LLM inside the app. It is part of the product, not a development tool.

## How I worked

I pasted the assignment and both problem statements and asked the assistant to read them carefully. It recommended the medium problem (Equipment Maintenance Triage Assistant) because a complete, reliable medium solution can score higher than a half-finished expert one, and I agreed. I then built it one step at a time, with small commits after each step:

1. Project setup, database models and the knowledge base
2. Report form, API and deterministic threshold rules
3. Manual retrieval (BM25) and retrieval failure handling
4. AI analysis with JSON validation and citations
5. Work order edit/approve/reject, confirmed findings, equipment history and AI retry
6. Demo seed data, README and deployment
7. Structured logging, UI states, error pages and documentation

## Representative prompts (paraphrased)

- "Read the whole assignment carefully and tell me which problem to pick and how to plan it."
- "Let's go one step at a time, move fast, follow every instruction and tell me when to commit."
- "Give me everything in a single file so I can open it directly in VS Code."
- "I want multiple small commits with proper messages, not one big commit." (after the first commit block bundled everything)
- I pasted terminal errors and screenshots directly (Python not found, API 404, API 503, missing key) and asked how to fix them.

## What was delegated and what I did

- **Drafted by the assistant**: application code, rules engine, retrieval, AI prompt and validation, templates, tests, README and deployment instructions.
- **Done by me**: choosing the problem and scope, installing and running everything on Windows, getting the API key, debugging environment problems, pushing back on process mistakes, running the tests, committing, deploying to Render and testing the live site.

## Agent mistakes and rejected suggestions

- **Windows setup commands were wrong at first.** The first instructions used `python` and a Command Prompt activation command, but my machine used PowerShell and the Microsoft Store alias blocked `python`. Fixed by using `py` and `Activate.ps1`.
- **Git instructions were incomplete.** The first commit block saved locally but never connected the GitHub remote or pushed, and it bundled everything into one commit. I pointed this out, and the process was changed to a remote push plus several small commits.
- **Wrong default Gemini model.** The default model name returned a 404 for my key. I found a working model and made the model name configurable with `GEMINI_MODEL`.
- **No handling of temporary AI errors.** The first AI version failed outright on a 503 overload. After seeing it happen, retry with short delays for 429, 503 and timeouts was added, with tests.
- **I removed my own API key line from `.env` by accident** while adding the model name. The app showed a clear "GEMINI_API_KEY is not set" error, which confirmed the failure message works, and I restored the key.
- **Rejected the expert problem** (migration workbench): too many moving parts for the time available.
- **Rejected a vector database** for retrieval: BM25 over small sections is simpler, transparent and testable.
- **Rejected trusting the AI's priority and citations as-is**: a deterministic priority floor and code-level citation checks were added instead.
- **Critical limits are not in the manuals.** The manuals only give warning levels, so the critical limits are my own assumptions, and the README says so.

## How I verified the output

- Ran the automated test suite with `pytest` after each step. The AI is mocked in tests, so they cover validation, retries, failures, the approval flow and locking, history, logging and UI states.
- Ran the app locally and checked the key cases by hand: critical threshold, conflicting data, missing readings and a real Gemini analysis with citations and a draft work order.
- Checked that the AI cannot approve work or create confirmed findings: those routes are technician-only, and tests assert that a new work order is always a draft.
- Checked secrets: `.env` is in `.gitignore`, `git status` never listed it, and the key exists only locally and in Render's environment settings.
- Deployed to Render and opened the live site to check the form, history and demo data.
