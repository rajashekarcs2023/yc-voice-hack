# Sam — AI Dispatcher for a Plumbing Business

YC Voice Agents Hackathon submission.

---

## 1. What is this?

**Sam is a voice agent that does the dispatcher's job for a small home-services
business** — answers the after-hours line, triages emergencies, books jobs, pages
the on-call tech, and (the killer move) **sources plumbing parts in real time by
placing parallel phone calls to multiple supply houses.**

The hero flow is **agent-to-agent calls**. A customer dials in and says *"my
Rinnai tankless water heater just died, can you find one under twelve hundred
today?"* Sam says *"let me call around"* and then **simultaneously calls three
supply houses**. Each supply house is its own voice agent with its own
personality (friendly Mike at ABC Supply, busy Ferguson, gruff Western) and its
own real inventory in SQLite. ~8 seconds later, Sam comes back to the customer:
*"ABC Supply has it in stock at eleven fifty. Ferguson is cheaper at ten ninety
but back-ordered five days. Want me to put one on hold at ABC?"*

The live dashboard shows all four conversations side-by-side with streaming
transcripts so the demo audience watches the agents talk to each other in
real time.

**Why this matters:** small plumbing/HVAC/electrical shops lose an hour a day of
owner-time on phone calls like this. A working autonomous dispatcher is
genuinely valuable, not a toy demo.

---

## 2. Demo video

**[< 60s video link here — record after the demo runs cleanly]**

If you're reading before the video is up: see the **Quick start** section to run
the demo yourself in 2 commands. The dashboard alone — no voice required —
demonstrates the agent-to-agent flow.

---

## 3. How we used Cekura, Nemotron, and Pipecat

### Pipecat (orchestration)

Sam's voice pipeline is Pipecat top-to-bottom: `NVidiaWebSocketSTTService →
LLMContextAggregatorPair → VLLMOpenAILLMService (Nemotron) → GradiumTTSService`,
with six direct-function tools registered on the LLM and a custom
`TranscriptLogger` FrameProcessor spliced in to mirror finalized customer turns
+ Sam's full responses into SQLite for the live dashboard.

Pipecat made it trivial to:
- Swap LLMs (we kept the structure from `bot-nemotron.py` and swapped out every
  flower-shop tool for dispatcher tools).
- Wire Twilio inbound + SmallWebRTC into the same `run_bot` entry point —
  switch by `RunnerArguments` match.
- Use the Twilio frame serializer + Krisp filter for production telephony.

### NVIDIA Nemotron-3-Super-120B (LLM)

Nemotron is the brain in **two places**:

1. **Sam himself** (the dispatcher) — agent triage logic, severity assessment,
   tool selection (when to call `source_part_for_job` vs. just `book_job`).
2. **Each supplier bot** (ABC, Ferguson, Western) — three independent LLM agents
   with distinct personalities, each making tool calls against their own
   supplier-scoped inventory in SQLite.

So a single parts-sourcing run = **4 simultaneous Nemotron conversations**
(1 dispatcher caller persona + 3 supplier personas), all making tool calls,
ranked into a winner.

### Nemotron Speech Streaming (STT)

Used as-is from the hackathon endpoint via the starter's `NVidiaWebSocketSTTService`.
Reasoning kept off (`NEMOTRON_ENABLE_THINKING=false`) for low-latency voice.

### Cekura (evaluation)

**What we tested:** 20 adversarial scenarios in
`server/sam/cekura_scenarios.md`, grouped into:

- **Emergency triage accuracy** (flood, gas smell, no heat in winter, sewage)
- **Tool-call discipline** (does Sam follow record_caller_info → assess_severity
  → source_part → book_job → page_on_call_tech in order?)
- **Parts-sourcing edge cases** (unknown part, no stock anywhere, vague description)
- **Adversarial honesty** ("are you a robot?", "I want a human")
- **Repeat customer recognition** (caller ID matches a row in DB)

**What we'd love to share once we run the full eval:**
*[Plug in actual scores from `/cekura-report` here before submission — we plan
to run the suite, identify the failing scenarios, tighten the system prompt,
and re-run to show the lift. Expected: triage accuracy 70% → 95%+, parts-sourcing
discipline 60% → 90%+ after one prompt iteration.]*

**Honest caveat:** we wrote the scenarios but didn't get a chance to run a
full before/after cycle within the day. The scenarios are tight and the test
harness is wired — happy to run live for the judges.

---

## 4. What's new vs. borrowed

### Built during the hackathon (new):
- `server/bot-sam.py` — entire Sam voice agent, system prompt, six tools, the
  `TranscriptLogger` FrameProcessor
- `server/sam/db.py` — SQLite schema for suppliers, parts, inventory, jobs,
  calls, customers, techs + seed data + read/write helpers
- `server/sam/bot_supplier.py` — parameterized supplier parts-desk agent with
  three distinct personalities
- `server/sam/outbound_caller.py` — the agentic core: parallel agent-to-agent
  call orchestrator with ranking logic
- `server/sam/dashboard.py` — FastAPI + single-page live ops dashboard with
  manual demo-trigger form (so the agentic flow can be demoed without a mic)
- `server/sam/cekura_scenarios.md` — 20 evaluation scenarios

### Borrowed (and unmodified):
- `server/nemotron_llm.py` — vLLM-compatible LLM service with corrected TTFB
  metric. Copied verbatim from the [Field & Flower
  starter](https://github.com/pipecat-ai/yc-voice-agents-hackathon) (BSD-2,
  copyright Daily).
- `server/nvidia_stt.py` — Nemotron Speech Streaming STT service. Same source,
  same license.
- The Pipecat pipeline pattern in `bot-sam.py` — the high-level shape (transport
  → STT → user_aggregator → LLM → TTS → transport) follows the starter's
  `bot-nemotron.py`. **Everything else** — tools, system prompt, state, agent
  logic, transcript logging — is original.

Nothing borrowed from pre-existing personal work — Sam was built from a blank
sheet today.

---

## 5. Feedback on the tools

### NVIDIA Nemotron-3-Super-120B

**What it did well:**
- **Personality adherence was excellent.** Three distinct supplier prompts
  (friendly/busy/gruff) produced consistently distinct voices across many turns.
  Mike at ABC reliably offered to hold parts and used the caller's name. Western
  reliably stayed gruff with one-line answers. Ferguson reliably stayed busy and
  didn't over-explain. We didn't have to keep tightening prompts to get
  personality separation — it just worked.
- **Tool-calling reliability was high.** Across many parallel runs, we didn't
  see malformed tool calls or hallucinated tool names. The model picked
  `check_part` vs. `hold_part` correctly almost every time.
- **Speed in the voice loop is great.** Per-turn supplier responses ~1-2s,
  3 parallel calls finishing the whole sourcing in **5-9s end to end**.

**Where it could be better:**
- **Stop-token discipline.** We used `<<HANGUP>>` as a sentinel to end agent-to-
  agent calls. The model would *sometimes* leak the literal token into the
  spoken text instead of using it as a terminator. Defensive `partition()` in
  client code worked around it, but it's a footgun for agentic loops.
- **Brevity drift.** Even with explicit "1-2 short sentences" instructions,
  responses occasionally bloomed to 3-4 sentences with greetings ("Hey Mike!")
  the prompt told it to skip. Phone-call brevity is hard to lock in.
- **No native reasoning-content separation on this endpoint.** With
  `enable_thinking=true` the chain-of-thought would inline into spoken output
  (no reasoning parser configured). Documented in `nemotron_llm.py`, but
  surprising the first time. A note about which served endpoints have a
  reasoning parser would help.

### Cekura

We didn't get to a full evaluation cycle within the day — but the scenario file
is concrete (20 cases with explicit pass criteria per scenario). Specific
feedback on the **idea of self-improvement loops with Cekura**:

- The pattern we wanted: write scenarios → run → get failing cases with
  transcripts → ask the model "given these failures, what 3 changes would
  improve the system prompt?" → apply → re-run → measure. If Cekura's CLI/MCP
  surfaced failing transcripts in a format you can pipe straight into another
  LLM as "fix these," the loop would close in minutes instead of hours. We
  didn't have time to confirm whether the existing CLI already supports this.
- **Multi-agent scoring is interesting.** Sam's behavior is partly determined
  by **three other agents** (the suppliers). A Cekura scenario that says
  *"verify Sam recommends the in-stock supplier even when a cheaper one is
  back-ordered"* depends on the supplier bots responding correctly first. A
  way to score *the system* end-to-end vs. *the lead agent* in isolation would
  be valuable for agent-to-agent setups.

### Pipecat

**What it did well:**
- **Transport polymorphism is great.** The same `run_bot` works on SmallWebRTC
  (local dev) and Twilio (production) just by branching on `RunnerArguments`.
  No code duplication for the bot logic itself.
- **`register_direct_function` + `ToolsSchema`** is clean. Adding a 7th tool is
  ~30 lines including the schema.
- **Frame processors are composable.** Our `TranscriptLogger` slotted in twice
  in the pipeline (one to catch finalized customer turns, one for Sam's
  responses) with no glue code beyond inheriting `FrameProcessor`.

**Where it could be better:**
- **Patterns for agent-to-agent calls aren't documented.** We bypassed the
  Pipecat pipeline entirely for the bot-to-bot calls (used raw OpenAI-compatible
  client + tool calls). A first-class pattern for "agent A calls agent B as if
  they were on the phone" — sharing audio between two Pipecat pipelines —
  would be huge for the kind of agentic workflows that won this hackathon.
- **Deploying with a custom subpackage required Dockerfile gymnastics.** The
  starter Dockerfile copies `bot.py` and `mock_backend.py` flat into the image.
  Adding `sam/` as a subpackage worked, but a pattern doc for non-flat layouts
  would save a debugging round.
- The `on_event("startup")` pattern in our FastAPI dashboard is deprecated by
  FastAPI in favor of lifespan handlers — minor, but worth updating in any
  examples that surface this pattern.

---

## Quick start

```bash
cd server
uv sync
uv run python -m sam.db --reset           # seeds 3 suppliers, 5 parts, 3 techs
uv run python -m sam.dashboard            # → http://localhost:7861
# In a second terminal:
uv run bot-sam.py                         # → http://localhost:7860 (voice)
```

**Demo without voice (works with zero keys beyond the hackathon defaults):**
on the dashboard, type a part in the "Source now" form (try `Rinnai RU199i`,
qty 1, max $1200), click Source, and watch three "supplier calls" light up
side-by-side with streaming transcripts.

See `server/.env.example` for required environment variables. The Nemotron and
Twilio endpoints come pre-configured from the hackathon README. You only need
to add a `GRADIUM_API_KEY` for the voice path.

---

## Architecture

```
                         ┌──────────────┐
   customer phone ──────►│  Sam (voice) │   ← Pipecat: Nemotron ASR
                         │  bot-sam.py  │     → Nemotron-3-Super LLM
                         └──────┬───────┘     → Gradium TTS
                                │
                                │ source_part_for_job(...)
                                ▼
                  ┌─────────────────────────────────┐
                  │ outbound_caller.source_parts(…) │
                  └──┬────────────┬──────────────┬──┘
                     │            │              │
                     ▼            ▼              ▼
                 ┌────────┐  ┌─────────┐  ┌─────────┐
                 │  ABC   │  │Ferguson │  │ Western │     ← SupplierBot
                 │ (Mike) │  │ (busy)  │  │ (gruff) │       (Nemotron LLM)
                 └───┬────┘  └────┬────┘  └────┬────┘
                     │            │            │
                     └────────────┴────────────┘
                                  ▼
                          ┌───────────────┐
                          │ SQLite (DB)   │ ← inventory, jobs, calls, parts
                          └───────┬───────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │  Live dashboard     │ ← FastAPI, polls /api/state
                       │  (port 7861)        │
                       └─────────────────────┘
```

### Files

| Path | Purpose |
|---|---|
| `server/bot-sam.py` | Sam's Pipecat voice pipeline (6 tools) |
| `server/sam/db.py` | SQLite schema + seed data |
| `server/sam/bot_supplier.py` | Parameterized supplier parts-desk agent |
| `server/sam/outbound_caller.py` | Parallel agent-to-agent call orchestrator |
| `server/sam/dashboard.py` | FastAPI live ops dashboard |
| `server/sam/cekura_scenarios.md` | 20 Cekura evaluation scenarios |
| `server/nemotron_llm.py` | NVIDIA Nemotron LLM service (borrowed from starter) |
| `server/nvidia_stt.py` | Nemotron Speech Streaming STT (borrowed from starter) |

---

## License

Files reused from the Pipecat starter (`server/nemotron_llm.py`,
`server/nvidia_stt.py`) carry their original BSD-2-Clause headers (Copyright (c)
2024-2026, Daily). All new files in this repo were written during the YC Voice
Agents Hackathon.
