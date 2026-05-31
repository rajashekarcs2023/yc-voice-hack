#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Sam — Rivera Plumbing's AI dispatcher (hackathon project).

Sam answers the after-hours line for Rivera Plumbing. He's not a fancy
voicemail; he actually does the dispatcher's job:

  1. Greets the caller, captures name + address, and triages the situation.
  2. Looks up known customers by caller ID.
  3. Sources parts in real time by calling multiple supply houses in parallel
     (real LLM-driven phone calls — see sam/outbound_caller.py).
  4. Creates jobs in the DB, books slots, and pages the on-call tech for
     emergencies.

Pipeline: Nemotron Speech Streaming STT → Nemotron-3-Super-120B LLM →
Gradium TTS. Same stack as the flower-shop starter, completely different brain.

Run::

    uv run bot-sam.py
"""

import os
from datetime import date, datetime

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService
from sam import db
from sam.outbound_caller import source_parts

load_dotenv(override=True)
db.init_db()


async def get_call_info(call_sid: str) -> dict:
    """Fetch caller ID from Twilio so we can recognize repeat customers."""
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    try:
        auth = aiohttp.BasicAuth(account_sid, auth_token)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    logger.error(f"Twilio API error ({response.status})")
                    return {}
                data = await response.json()
                return {"from_number": data.get("from"), "to_number": data.get("to")}
    except Exception as e:
        logger.error(f"Twilio API error: {e}")
        return {}


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Sam's main loop. Tools below are closed over per-call state."""
    logger.info("Starting Sam")

    # Per-call state — closed over by tools so each call is isolated.
    call_state: dict = {
        "customer_id": None,
        "customer_name": None,
        "customer_address": None,
        "current_issue": None,
        "severity": None,
        "job_id": None,
        "call_log_id": db.start_call(
            direction="inbound",
            counterpart=f"customer:{from_number}" if from_number else "customer:unknown",
        ),
    }

    # If caller ID matches a known customer, prefill state.
    if from_number:
        existing = db.get_customer_by_phone(from_number)
        if existing:
            call_state["customer_id"] = existing["id"]
            call_state["customer_name"] = existing["name"]
            call_state["customer_address"] = existing["address"]

    # --- Tools ----------------------------------------------------------------

    async def record_caller_info(
        params: FunctionCallParams,
        name: str,
        address: str,
    ) -> None:
        """Save the caller's name and service address.

        Call this once after the caller has given you BOTH their name and
        address. Don't call it before — you'd save partial info.

        Args:
            name: Caller's name as they said it. E.g. "Maria Lopez".
            address: Service address. E.g. "742 Evergreen Terrace, Springfield".
        """
        phone = from_number or f"unknown-{call_state['call_log_id']}"
        customer_id = db.upsert_customer(phone=phone, name=name, address=address)
        call_state["customer_id"] = customer_id
        call_state["customer_name"] = name
        call_state["customer_address"] = address
        logger.info(f"Customer recorded: {name} @ {address} (id={customer_id})")
        await params.result_callback({"ok": True, "customer_id": customer_id})

    async def assess_severity(
        params: FunctionCallParams,
        severity: str,
        reasoning: str,
    ) -> None:
        """Record your assessment of the situation's urgency.

        Call this after asking enough triage questions to know whether this is
        an emergency. Categories:
          - "emergency": active water leak/flooding, no heat in winter, sewage
            backup, gas smell. Needs same-night response.
          - "urgent": no hot water, single fixture broken, slow leak. Within 24h.
          - "routine": maintenance, installation, non-blocking issues. Schedule
            normally.

        Args:
            severity: One of "emergency", "urgent", "routine".
            reasoning: One sentence on WHY (used in the job ticket).
        """
        call_state["severity"] = severity
        call_state["current_issue"] = reasoning
        await params.result_callback({"ok": True, "severity": severity})

    async def source_part_for_job(
        params: FunctionCallParams,
        part_query: str,
        quantity: int = 1,
        max_price_dollars: float | None = None,
    ) -> None:
        """Call multiple supply houses IN PARALLEL to find a plumbing part.

        Use this when the caller mentions a specific part you'd need to install
        or replace (a brand-name water heater, a cartridge, a fixture, etc.) and
        you want to know who has it in stock and for how much, RIGHT NOW.

        This is a real outbound calling operation — say something like "let me
        call around real quick, give me a sec" BEFORE calling this tool so the
        caller knows there'll be a pause. The tool typically takes 10-20 seconds.

        Args:
            part_query: Brand and model in plain words. E.g. "Rinnai RU199i
                tankless water heater" or "Moen 1225 cartridge". Be specific.
            quantity: How many units needed. Defaults to 1.
            max_price_dollars: Optional budget cap. E.g. 1200 means "under
                twelve hundred". Omit if the caller didn't specify.
        """
        logger.info(f"Sam sourcing {quantity}x '{part_query}' (max ${max_price_dollars})")
        result = await source_parts(
            part_query=part_query,
            quantity=quantity,
            max_price_dollars=max_price_dollars,
        )
        # Return only what the LLM needs — keep the spoken_summary so Sam can
        # read it back nearly verbatim, plus the structured offers in case the
        # caller asks follow-ups ("what did Ferguson say?").
        compact = {
            "ok": result.get("ok"),
            "spoken_summary": result.get("spoken_summary"),
            "recommended": result.get("recommended"),
            "all_offers": result.get("offers", []),
            "request_id": result.get("request_id"),
        }
        await params.result_callback(compact)

    async def book_job(
        params: FunctionCallParams,
        when_text: str,
        description: str,
    ) -> None:
        """Create a job in the dispatch system once the caller has confirmed.

        Call this AFTER you've:
          1. Captured name + address (record_caller_info)
          2. Assessed severity (assess_severity)
          3. Agreed on what's being done and when

        For emergencies, also call page_on_call_tech right after this.

        Args:
            when_text: When the job is scheduled, in the caller's own words.
                E.g. "tomorrow at 8am", "this afternoon", "ASAP".
            description: One-line summary of the work. E.g. "Replace failed
                Rinnai RU199i tankless water heater, parts on order from ABC".
        """
        if call_state["customer_id"] is None:
            await params.result_callback(
                {"ok": False, "reason": "Need to call record_caller_info first."}
            )
            return
        if call_state["severity"] is None:
            await params.result_callback(
                {"ok": False, "reason": "Need to assess severity first."}
            )
            return
        job_id = db.create_job(
            customer_id=call_state["customer_id"],
            description=description,
            severity=call_state["severity"],
            scheduled_at=when_text,
        )
        call_state["job_id"] = job_id
        confirmation = f"RV-{job_id:05d}"
        logger.info(f"Job created: {confirmation} — {description}")
        await params.result_callback(
            {"ok": True, "job_id": job_id, "confirmation_number": confirmation}
        )

    async def page_on_call_tech(params: FunctionCallParams) -> None:
        """Alert the on-call tech about an emergency job.

        Use ONLY for emergencies. Returns the tech's name so you can tell the
        caller who'll be calling them back.
        """
        tech = db.on_call_tech()
        if not tech:
            await params.result_callback(
                {"ok": False, "reason": "No tech on call right now — escalate to owner."}
            )
            return
        if call_state["job_id"]:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE jobs SET assigned_tech_id=?, status='dispatched' WHERE id=?",
                    (tech["id"], call_state["job_id"]),
                )
        logger.info(
            f"Paged on-call tech {tech['name']} for job {call_state['job_id']} "
            f"(would SMS {tech['phone']} in production)"
        )
        await params.result_callback(
            {
                "ok": True,
                "tech_name": tech["name"],
                "eta_minutes": "5 to 10",
            }
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Say goodbye in the SAME turn, then call this."""
        logger.info("end_call invoked")
        # Write a summary back to the call log.
        summary_parts = []
        if call_state["customer_name"]:
            summary_parts.append(f"caller: {call_state['customer_name']}")
        if call_state["severity"]:
            summary_parts.append(f"severity: {call_state['severity']}")
        if call_state["current_issue"]:
            summary_parts.append(f"issue: {call_state['current_issue']}")
        if call_state["job_id"]:
            summary_parts.append(f"job_id: {call_state['job_id']}")
        db.end_call(
            call_state["call_log_id"],
            summary=" | ".join(summary_parts) or "no actions taken",
            outcome="handled" if call_state["job_id"] else "no_job",
        )
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        record_caller_info,
        assess_severity,
        source_part_for_job,
        book_job,
        page_on_call_tech,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction ---------------------------------------------------

    caller_context = (
        f"This caller is a known customer: name on file {call_state['customer_name']}, "
        f"address {call_state['customer_address']}. Greet them by their first name "
        "and confirm the address is still right rather than re-asking from scratch."
        if call_state["customer_name"]
        else (
            "Unknown caller. You'll need to capture their name and address before "
            "you can book anything."
        )
    )

    system_instruction = (
        "You are Sam, the after-hours dispatcher for Rivera Plumbing. You're answering "
        "the emergency line. Real people call when something is going wrong in their "
        "home, sometimes at 2am. Your job: figure out what's broken, how urgent it is, "
        "and either page the on-call tech or book a slot.\n\n"
        "Workflow for every call:\n"
        "1. Greet, ask what's going on.\n"
        "2. Capture name and service address (record_caller_info).\n"
        "3. Triage with 1-3 short questions, then assess_severity.\n"
        "4. If they mention a specific part or appliance (brand + model), use "
        "source_part_for_job to find one in real time — tell them you're calling "
        "around BEFORE invoking the tool.\n"
        "5. book_job once you've agreed on time + scope.\n"
        "6. For emergencies, page_on_call_tech right after booking.\n"
        "7. Say goodbye and end_call.\n\n"
        "How to talk:\n"
        "- This is a phone call. Keep turns to 1-2 short sentences.\n"
        "- Ask ONE thing at a time. Don't stack questions.\n"
        '- Skip filler ("Absolutely!", "I\'d be happy to"). Go straight to the point.\n'
        "- Be calm and competent — callers are often stressed.\n"
        '- Read prices in words. "Eleven fifty" not "$1150.00".\n'
        "- Read job confirmation numbers digit by digit ('R-V dash zero zero zero zero one').\n"
        "- Use contractions. Fragments are fine.\n\n"
        "Triage cues:\n"
        "- EMERGENCY: active leak / flooding, no heat in winter, sewage, gas smell.\n"
        "- URGENT: no hot water, broken fixture, slow leak.\n"
        "- ROUTINE: install, maintenance, anything that can wait.\n\n"
        f"Today is {date.today().strftime('%A, %B %d, %Y')}, currently "
        f"{datetime.now().strftime('%I:%M %p')}.\n\n"
        f"Caller context: {caller_context}"
    )

    # --- Services -------------------------------------------------------------

    stt = NVidiaWebSocketSTTService(
        url=os.environ["NVIDIA_ASR_URL"],
        strip_interim_prefix=True,
    )

    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.environ["NEMOTRON_LLM_URL"],
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    for fn in tool_functions:
        llm.register_direct_function(fn)

    # TranscriptLogger mirrors customer + Sam turns into the SQLite call log so the
    # dashboard can render the live transcript without touching the audio path.
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameProcessor

    class TranscriptLogger(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self._buffer: list[str] = []

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMTextFrame):
                self._buffer.append(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame) and self._buffer:
                full = "".join(self._buffer).strip()
                self._buffer.clear()
                if full:
                    db.append_call_turn(call_state["call_log_id"], "sam", full)
            elif isinstance(frame, TranscriptionFrame) and getattr(frame, "finalized", False):
                if frame.text:
                    db.append_call_turn(call_state["call_log_id"], "customer", frame.text)
            await self.push_frame(frame, direction)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            TranscriptLogger(),
            user_aggregator,
            llm,
            TranscriptLogger(),
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        if call_state["customer_name"]:
            kickoff = (
                f"A returning customer ({call_state['customer_name']}) just called. "
                "Greet them by first name: 'Rivera Plumbing, this is Sam — hey "
                f"{call_state['customer_name'].split()[0]}, what's going on tonight?'"
            )
        else:
            kickoff = (
                "A customer just called. Greet them: 'Rivera Plumbing after-hours, "
                "this is Sam. What's going on?'"
            )
        context.add_message({"role": "user", "content": kickoff})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        # Make sure the call gets closed even if end_call wasn't invoked.
        db.end_call(call_state["call_log_id"], outcome="disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point: handles SmallWebRTC (local) and Twilio (production)."""

    from_number: str | None = None
    transport_overrides: dict = {}

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            transport = SmallWebRTCTransport(
                webrtc_connection=runner_args.webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case WebSocketRunnerArguments():
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            _, call_data = await parse_telephony_websocket(runner_args.websocket)
            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
                account_sid=os.environ["TWILIO_ACCOUNT_SID"],
                auth_token=os.environ["TWILIO_AUTH_TOKEN"],
            )
            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, from_number=from_number, **transport_overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
