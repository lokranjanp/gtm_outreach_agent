# Signalcraft

A local Streamlit workspace for evidence-led B2B outreach. LangGraph separates company research, signal selection and email drafting, with resumable human review before drafting and before approval.

## Run

Requires Python 3.11+.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
streamlit run app.py
```

The default **Offline demo** needs no API keys. It includes two fictional companies: Aster Labs (small software company expanding internationally) and Harbor Manufacturing (established manufacturer standardizing purchasing across sites). All demo sources and people are fictional; writing is deterministic sample output, not a model call. Use the supplied SpendBridge demo product for these purchasing scenarios.

## Live research

Copy `.env.template` to `.env` and set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) and `TAVILY_API_KEY`. Gemini is the default provider, using `gemini-3.8-flash`. You do not need an OpenAI key for Gemini mode.

```dotenv
OUTREACH_PROVIDER=gemini
OUTREACH_MODEL=gemini-3.8-flash
GEMINI_API_KEY=your-private-gemini-key
TAVILY_API_KEY=your-private-tavily-key
```

Alternatively place the settings in `.streamlit/secrets.toml`. Configuration precedence is Streamlit secrets, nonempty process environment variables, then the project-root `.env`. Within each source, `GOOGLE_API_KEY` takes priority over `GEMINI_API_KEY`. The real `.env` is reread on each interaction; keys are never stored in workflow checkpoints. `OUTREACH_DATA_DIR` defaults to the project's `data/` directory.

For optional OpenAI mode, set `OUTREACH_PROVIDER=openai`, `OUTREACH_MODEL=gpt-5-mini` and `OPENAI_API_KEY` instead of a Gemini key. Keep the same Tavily key. A model from the wrong provider produces a configuration warning before a live run can start. Provider/model settings apply to new and resumed live operations; saved research, approvals and draft history remain compatible.

1. Create a reusable profile in **Product library** with approved benefits, buyer roles and optional proof points.
2. Start **New outreach → Live research**. Enter the company, recipient name/role, product and length. A company website helps disambiguation.
3. Supply follow-up context when applicable, or explicitly skip it. The agent will avoid assuming earlier conversation details.
4. Review proposed signals and their source excerpts, dates, verification status and product/recipient connections. Approve 1–2 or supply more evidence.
5. Edit or regenerate the subject/body, revisit signals, then check and approve. Copy the approved email using the code block's copy control or download the text file.

Tavily's hosted MCP server provides search and extraction over Streamable HTTP through `langchain-mcp-adapters`; no browser extension or local MCP process is required. Public LinkedIn coverage is best effort. An inaccessible post can be supplied as pasted text; it remains labeled user-provided. Research data is sent to Tavily and your selected model provider (Google Gemini or OpenAI) in live mode. Do not paste correspondence you do not want processed by these services.

## Workflow

```text
Collect inputs → Resolve company → Research company → Select signals
                       ↓                                 ↓
                 Confirm ambiguity                 Human signal review
                                                         ↓
                          Draft email ←──────────── Approved signals
                              ↓
                        Quality checks ←────────── Human edits
                              ↓
                       Human draft review → Approved output
                              ↳ Feedback / return to signal selection
```

Each run has a stable thread ID and SQLite checkpoints. Open **Saved outreach** after a refresh or app restart to resume a pending review. Completed research is not repeated when a review resumes. Changing product profiles does not modify existing runs. Failed model/research steps retain earlier checkpoints and can be retried. Local SQLite data contains research, entered correspondence and draft history; it is excluded from git.

## Progress, signal controls and recipient fit

The stage tracker shows company identity, research, signal selection, drafting and review. It updates when steps and tool calls start, shows source counts and timings as operations finish, and explicitly waits for you at review gates. Saved runs restore the current review stage. These are operational activity updates, not hidden model reasoning.

If you provide a company page URL, research reads that exact page first, including its path. It explores useful first-party About, leadership, newsroom and article links before external news and social searches. Public LinkedIn company pages can be starting sources; the official company website is resolved separately. Inaccessible or mismatched pages use the normal clarification flow.

Use **Manage retrieved signals → Remove signal** during signal or draft review. Removal excludes the signal from this run and future research retries. **Removed signals → Restore signal** reverses it. Removing a signal used by the current email clears that draft and its approval and returns to signal selection. Earlier versions remain historical and are labeled outdated. Approved runs can be reopened for editing; reopening clears approval before changes are made.

Company/product fit and recipient fit are assessed separately. A strong company fit can produce an email even if the named person is a weak or uncertain fit. The UI displays a recipient-fit warning outside the email, including after approval. The email pitches the company benefit without inventing the person's responsibility or switching the CTA to an introduction request. Weak company fit and unsupported role claims remain blocking issues.

For appearance, use Streamlit's **⋮ menu → theme selector** to choose Light or Dark. Both native palettes are customized, and app elements inherit the selected theme.

## Local operational logs

INFO logs are written to the console and `<OUTREACH_DATA_DIR>/logs/outreach.log` (the project's `data/logs/outreach.log` by default). Files rotate at 5 MB with three backups. Logs contain run IDs, provider/model, stage transitions, timings, retry attempts, source counts and sanitized failure categories. They exclude API keys, headers, queries, source text, correspondence and email content. Logging handlers are reused across Streamlit reruns. The log directory is excluded from git along with local data.

Research starts with 90 days and expands to 12 months when no useful hook is found. Each retrieval pass allows at most 12 tool attempts, including retries, with a 40-second timeout per attempt. Only extracted or user-supplied source content can support a signal; discovery snippets cannot. Model-proposed quotes must match their source text. Old roles, uncertain company matches, low-confidence claims, undated news and unsupported quotations are excluded. Evergreen website context is allowed, but must not be described as recent. There is no generic-draft fallback when the product connection is weak.

Selection explains industry, employee size and company maturity without treating these attributes as proof of pain. The drafting prompt uses only approved signals and product claims. A separate model check assesses grounding and personalization; deterministic checks enforce word limits and approved evidence IDs. Two automatic repair attempts are allowed. Human edits are checked without being silently rewritten. These checks assist review; they cannot independently guarantee factual truth or email quality.

## Validation

```sh
python -m pytest -q
python -m ruff check .
```

Tests run without network/API keys and exercise the actual LangGraph/SQLite resume flow, both human gates, ambiguity, sparse research, unsupported/stale signals, follow-up context, user edits, bounded repairs, snapshots and the Streamlit UI. Live calls are intentionally not made by the test suite.
They also cover progress before operations finish, reversible exclusions across retries/restarts, outdated drafts, recipient-fit warnings, supplied-URL priority, log privacy/rotation and saved-checkpoint compatibility.

For a live smoke test, use two real companies in different industries with valid keys: inspect source links and current roles, confirm each hook relates to the selected product, approve signals, revise the draft, restart the app at a review gate and confirm it resumes. API access and research coverage depend on your provider accounts.

V1 is single-user, English, one prospect per run, and drafting only. It does not send messages, authenticate users, connect a CRM/mailbox, or retrieve private LinkedIn content.

## Integration references

- [LangGraph interrupts and persistence](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangChain MCP adapters](https://github.com/langchain-ai/langchain-mcp-adapters)
- [Tavily MCP tools and authentication](https://docs.tavily.com/documentation/mcp)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Gemini LangChain integration](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai)
- [Gemini models](https://ai.google.dev/gemini-api/docs/models)
