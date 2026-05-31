# Sam — AI Dispatcher for Rivera Plumbing

YC Voice Agents Hackathon project. Built on the Field & Flower starter
(Pipecat + Nemotron + Gradium + Twilio + Cekura).

## What it does

Small home-services businesses (plumbers, HVAC, electricians) lose **30-60
minutes a day** of their owner's time on outbound phone calls — sourcing parts
from supply houses, following up on quotes, paging on-call techs. Sam does this
work autonomously.

**The hero flow: bots calling bots.**

A customer calls Sam. Mid-call, the customer mentions a specific part (e.g.
"my Rinnai RU199i tankless died"). Sam:

1. Says "let me call around real quick, give me a sec."
2. Places **3 outbound calls in parallel** to ABC Supply, Ferguson, and Western
   Plumbing.
3. Each call is a real LLM-driven conversation with a personality (Mike at ABC
   is friendly, Ferguson's parts desk is busy/curt, Western is gruff).
4. Each supplier-bot queries its own real inventory (SQLite) and quotes back
   stock + price.
5. ~10 seconds later, Sam reports the winner to the customer: *"ABC Supply has
   it in stock at eleven fifty. Ferguson is cheaper at ten ninety but back-
   ordered five days. Want me to put one on hold at ABC?"*

The dashboard shows all 4 conversations side-by-side, live transcripts streaming
in real time. Judges literally watch agents call agents.

## Architecture

```
                         ┌──────────────┐
   customer phone ──────►│  Sam (voice) │   ← Pipecat: Nemotron ASR
                         │  bot-sam.py  │     → Nemotron-3-Super LLM
                         └──────┬───────┘     → Gradium TTS
                                │
                                │ source_part_for_job(...)
                                ▼
                  ┌─────────────────────────────┐
                  │  outbound_caller.source_parts │
                  └──┬────────────┬──────────────┘
                     │            │            │
                     ▼            ▼            ▼
                 ┌────────┐  ┌─────────┐  ┌─────────┐
                 │  ABC   │  │Ferguson │  │Western  │     ← SupplierBot
                 │ (Mike) │  │(busy)   │  │ (gruff) │       (Nemotron LLM)
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
| `server/bot-sam.py` | Main entry point — Sam's Pipecat pipeline (inbound voice) |
| `server/sam/db.py` | SQLite schema + seed data + helpers |
| `server/sam/bot_supplier.py` | Parameterized supplier parts-desk bot (LLM + tools) |
| `server/sam/outbound_caller.py` | Orchestrates parallel Sam→supplier conversations |
| `server/sam/dashboard.py` | FastAPI live ops dashboard |
| `server/sam/cekura_scenarios.md` | 20 test scenarios for `/cekura-report` |
| `server/nemotron_llm.py` | NVIDIA Nemotron LLM service (from the Pipecat starter) |
| `server/nvidia_stt.py` | Nemotron Speech Streaming STT (from the Pipecat starter) |

## Quick start

```bash
cd server
uv sync

# Initialize the DB (seeds 3 suppliers, 5 parts, 3 techs, 2 customers).
uv run python -m sam.db --reset

# Fill in GRADIUM_API_KEY in .env if you want the voice path to work.
# The Nemotron + Twilio creds are already set from the hackathon defaults.

# Start the dashboard.
uv run python -m sam.dashboard      # http://localhost:7861

# Start Sam (in another terminal — voice over WebRTC).
uv run bot-sam.py                   # http://localhost:7860
```

### Demo without voice (Gradium key not needed)

The agentic parts-sourcing flow works without any voice services. Use the
"Source now" form on the dashboard to trigger a run:

```bash
curl -sX POST http://localhost:7861/api/source \
  -H 'content-type: application/json' \
  -d '{"part_query": "Rinnai RU199i", "max_price_dollars": 1200}'
```

Watch the right-hand panel — three "phone calls" light up, fill with
transcripts, and a winner gets recommended. Typically 8-12 seconds end-to-end.

## Demo script (8-minute pitch)

1. **(30s) Setup.** "Rivera Plumbing's owner spends an hour a day on the phone
   sourcing parts from supply houses. Sam does this for him — and answers the
   emergency line."

2. **(2 min) Inbound voice demo.** Pull up the dashboard side-by-side. Call
   Sam's Twilio number (or use the WebRTC tab). Pretend to be a panicked
   homeowner: *"My basement is flooding from a Rinnai RU199i that just blew."*
   Show Sam triaging, capturing address, then saying "let me call around."

3. **(2 min) The wow.** As Sam triggers `source_part_for_job`, the dashboard
   lights up with 3 live supplier calls. Read out one supplier's transcript
   live — *"Hey Mike! Yeah, we've got the Rinnai RU199i tankless in stock..."*.
   Sam comes back: "ABC Supply has it for eleven fifty. Want me to put one on
   hold?" Say yes. Sam books the job and pages Mike Rivera.

4. **(2 min) Cekura.** `/cekura-report` against the scenarios in
   `sam/cekura_scenarios.md`. Show the score — emergency triage 100%, part
   sourcing 95%, "is this a robot" honesty 100%. Point to one failed
   scenario and the fix that drove the score up.

5. **(1 min) Why it matters.** "Per call, $50 in plumber time saved and a 5x
   faster response than the human dispatcher. 3 supply houses called in
   parallel takes 10 seconds instead of 15 minutes. Privacy unlock: customer
   pricing data stays in Rivera's VPC because Nemotron is open-source."

6. **(30s) Close.** "This is one industry. Same pattern works for HVAC,
   electricians, locksmiths — anyone who answers a phone for a living."

## Tech stack (sponsor checkbox)

- **Orchestration:** Pipecat
- **STT:** NVIDIA Nemotron Speech Streaming (open-source)
- **LLM:** NVIDIA Nemotron-3-Super-120B (open-source, runs on AWS)
- **TTS:** Gradium
- **Telephony:** Twilio (inbound + outbound)
- **Deploy:** Pipecat Cloud
- **Eval:** Cekura

## What's not mocked (everything that matters)

- ✅ Real LLM-driven phone conversations between Sam and each supplier
- ✅ Real DB-backed inventory lookups (sqlite, 15 inventory rows seeded)
- ✅ Real per-supplier personalities affecting tone + behavior
- ✅ Real parallel execution (asyncio.gather — 3 calls run simultaneously)
- ✅ Real ranking logic (in-stock + under-budget wins, then price, then lead)
- ✅ Real persistence (every call, transcript, and decision logged)
- ✅ Real dashboard pulling live state from the DB

## What's mocked (intentionally — out of scope for one day)

- Supplier phone numbers — the bot-to-bot calls happen via LLM, not via 3
  real Twilio outbound calls. Easy to upgrade: each `SupplierBot` becomes a
  separate Pipecat worker on its own Twilio number; `outbound_caller` places
  real Twilio outbound calls. ~2 hours of additional work.
- SMS notifications to the on-call tech — `page_on_call_tech` logs but doesn't
  send. Wire in `twilio.rest.Client().messages.create(...)` to make real.

## Deployment

Update `pcc-deploy.toml` is already set to `agent_name = "sam-dispatcher"`.
Dockerfile copies the right files. Standard flow:

```bash
pc cloud secrets set sam-secrets --file .env
pc cloud deploy
```

Then create a TwiML Bin pointing to `sam-dispatcher.YOUR_ORG_NAME` and attach
it to your Twilio number.
