"""Outbound call orchestrator — drives Sam-to-supplier conversations.

This is the agentic core of the demo. When Sam (mid-conversation with a customer)
decides to source a part, he calls `source_parts(...)`, which:

  1. Creates a parts_request row.
  2. Spawns a parallel "outbound call" to every supplier.
  3. Each call is a multi-turn conversation: a small "caller" LLM persona plays
     Sam-on-the-phone, talking to a SupplierBot until it has price + stock or
     hits the turn cap.
  4. Persists each call's full transcript to the DB so the dashboard can stream
     it live.
  5. Ranks the offers (in-stock first, then lowest price, then shortest lead time)
     and reports the winner back to Sam.

The "caller" LLM is intentionally separate from the main Sam pipeline so the
voice conversation with the customer isn't blocked while Sam is "on the phone"
with suppliers. Sam's UX is: "Let me call around, hold on a sec." → tool runs in
the background → "OK, ABC Supply has it for eleven fifty, in stock."
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

from loguru import logger
from openai import AsyncOpenAI

from . import db
from .bot_supplier import SupplierBot


MAX_TURNS_PER_CALL = 6  # Caller + supplier alternating; 6 = 3 exchanges, enough for price+stock.


@dataclass
class SupplierOffer:
    supplier_id: int
    supplier_name: str
    in_stock: bool
    price_dollars: float | None
    lead_time_days: int | None
    transcript: list[dict]
    call_id: int
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "supplier_id": self.supplier_id,
            "supplier_name": self.supplier_name,
            "in_stock": self.in_stock,
            "price_dollars": self.price_dollars,
            "lead_time_days": self.lead_time_days,
            "notes": self.notes,
            "call_id": self.call_id,
        }


def _caller_client() -> tuple[AsyncOpenAI, str]:
    """Return an OpenAI-compatible client + model for the 'caller' persona."""
    nemotron_url = os.environ.get("NEMOTRON_LLM_URL")
    if nemotron_url:
        return (
            AsyncOpenAI(
                api_key=os.environ.get("NEMOTRON_LLM_API_KEY", "EMPTY"),
                base_url=nemotron_url,
            ),
            os.environ.get("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
        )
    return AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"]), "gpt-4o-mini"


def _caller_system_prompt(part_query: str, max_price_dollars: float | None, quantity: int) -> str:
    price_clause = (
        f"under {max_price_dollars:.0f} dollars" if max_price_dollars else "competitively priced"
    )
    return (
        "You are an assistant calling a plumbing supply house on behalf of Rivera "
        "Plumbing's dispatcher. Your only goal on this call: find out (a) whether "
        f"they have {quantity}x '{part_query}' in stock and (b) the price.\n\n"
        f"You want it {price_clause}. If they don't have it in stock, ask the lead "
        "time and whether they can get it within 3 days.\n\n"
        "Conversation style:\n"
        "- This is a real phone call. Keep turns SHORT (one or two sentences max).\n"
        "- Sound like a busy contractor's office, not a sales pitch.\n"
        "- Don't volunteer extra context. Don't repeat their answer back.\n"
        "- When you have stock status AND price, thank them and say goodbye — output "
        "exactly the token '<<HANGUP>>' on its own line right after your goodbye to end the call.\n"
        "- If they say they don't carry it OR don't have it and can't get it in time, "
        "thank them and hang up too.\n"
        "- Never make small talk or chat. Stay on task."
    )


async def _run_one_call(
    supplier_id: int,
    part_query: str,
    quantity: int,
    max_price_dollars: float | None,
    parts_request_id: int,
) -> SupplierOffer:
    """Drive one outbound call. Returns the offer + full transcript."""
    supplier = db.get_supplier(supplier_id)
    assert supplier is not None
    supplier_bot = SupplierBot(supplier_id=supplier_id)

    client, model = _caller_client()
    caller_history: list[dict] = [
        {
            "role": "system",
            "content": _caller_system_prompt(part_query, max_price_dollars, quantity),
        }
    ]

    call_id = db.start_call(
        direction="outbound",
        counterpart=f"supplier:{supplier['name']}",
        counterpart_id=supplier_id,
        parts_request_id=parts_request_id,
    )

    transcript_for_offer: list[dict] = []

    # The supplier picks up first.
    supplier_line = supplier_bot.greeting
    transcript_for_offer.append({"role": "supplier", "text": supplier_line})
    db.append_call_turn(call_id, "supplier", supplier_line)
    logger.info(f"[call:{call_id}|{supplier['name']}] SUPPLIER: {supplier_line}")

    hangup = False
    for turn in range(MAX_TURNS_PER_CALL):
        caller_history.append({"role": "user", "content": supplier_line})
        resp = await client.chat.completions.create(
            model=model,
            messages=caller_history,
            temperature=0.4,
        )
        caller_line = (resp.choices[0].message.content or "").strip()

        if "<<HANGUP>>" in caller_line:
            spoken, _, _ = caller_line.partition("<<HANGUP>>")
            spoken = spoken.strip()
            if spoken:
                transcript_for_offer.append({"role": "caller", "text": spoken})
                db.append_call_turn(call_id, "caller", spoken)
                logger.info(f"[call:{call_id}|{supplier['name']}] CALLER: {spoken}")
            hangup = True
            break

        caller_history.append({"role": "assistant", "content": caller_line})
        transcript_for_offer.append({"role": "caller", "text": caller_line})
        db.append_call_turn(call_id, "caller", caller_line)
        logger.info(f"[call:{call_id}|{supplier['name']}] CALLER: {caller_line}")

        supplier_line = await supplier_bot.reply(caller_line)
        transcript_for_offer.append({"role": "supplier", "text": supplier_line})
        db.append_call_turn(call_id, "supplier", supplier_line)
        logger.info(f"[call:{call_id}|{supplier['name']}] SUPPLIER: {supplier_line}")

    # Always close the row, even if we hit the turn cap.
    if not hangup:
        db.append_call_turn(call_id, "system", "(timed out — caller hung up after 6 turns)")

    # Read the actual answer from the DB (source of truth), not from what the bot said.
    part = db.find_part(part_query)
    offer = SupplierOffer(
        supplier_id=supplier_id,
        supplier_name=supplier["name"],
        in_stock=False,
        price_dollars=None,
        lead_time_days=None,
        transcript=transcript_for_offer,
        call_id=call_id,
    )
    if part:
        inv = db.get_inventory(supplier_id, part["id"])
        if inv:
            offer.in_stock = bool(inv["in_stock"]) and inv["quantity"] >= quantity
            offer.price_dollars = round(inv["price_cents"] / 100, 2)
            offer.lead_time_days = inv["lead_time_days"]
            if not offer.in_stock and inv["lead_time_days"] > 0:
                offer.notes = f"back-ordered, ~{inv['lead_time_days']}d lead time"
    else:
        offer.notes = "supplier doesn't recognize the part"

    summary = (
        f"{offer.supplier_name}: "
        + ("in stock" if offer.in_stock else "no stock")
        + (f", ${offer.price_dollars:.2f}" if offer.price_dollars is not None else "")
        + (f", {offer.lead_time_days}d lead" if offer.lead_time_days else "")
    )
    db.end_call(call_id, summary=summary, outcome="completed")
    return offer


def _rank_offers(
    offers: list[SupplierOffer], max_price_dollars: float | None
) -> SupplierOffer | None:
    """In-stock + under budget wins. Then lowest in-stock price. Then shortest lead."""
    in_stock_under = [
        o
        for o in offers
        if o.in_stock
        and o.price_dollars is not None
        and (max_price_dollars is None or o.price_dollars <= max_price_dollars)
    ]
    if in_stock_under:
        return min(in_stock_under, key=lambda o: o.price_dollars or float("inf"))

    in_stock = [o for o in offers if o.in_stock and o.price_dollars is not None]
    if in_stock:
        return min(in_stock, key=lambda o: o.price_dollars or float("inf"))

    backordered = [
        o
        for o in offers
        if not o.in_stock and o.lead_time_days is not None and o.lead_time_days > 0
    ]
    if backordered:
        return min(backordered, key=lambda o: (o.lead_time_days or 99, o.price_dollars or 9e9))

    return None


async def source_parts(
    part_query: str,
    quantity: int = 1,
    max_price_dollars: float | None = None,
    needed_by: str | None = None,
) -> dict:
    """The headline tool. Sam calls this; it returns a structured ranked result.

    Returns a dict with: request_id, offers (one per supplier), recommended (the winner
    or None), and a short spoken_summary Sam can read aloud.
    """
    started = time.time()
    part = db.find_part(part_query)
    if not part:
        return {
            "ok": False,
            "spoken_summary": (
                f"I couldn't find a part matching '{part_query}' in our catalog. "
                "Do you have a different part number or description?"
            ),
            "offers": [],
        }

    parts_request_id = db.create_parts_request(
        part_id=part["id"],
        quantity=quantity,
        max_price_cents=int(max_price_dollars * 100) if max_price_dollars else None,
        needed_by=needed_by,
    )

    suppliers = db.list_suppliers()
    logger.info(
        f"[parts_request:{parts_request_id}] sourcing '{part['name']}' x{quantity} "
        f"from {len(suppliers)} suppliers in parallel"
    )

    # Parallel outbound calls.
    offers: list[SupplierOffer] = await asyncio.gather(
        *(
            _run_one_call(s["id"], part_query, quantity, max_price_dollars, parts_request_id)
            for s in suppliers
        )
    )

    winner = _rank_offers(offers, max_price_dollars)
    if winner:
        db.update_parts_request(
            parts_request_id,
            status="recommended",
            best_supplier_id=winner.supplier_id,
            best_price_cents=int((winner.price_dollars or 0) * 100),
        )
    else:
        db.update_parts_request(parts_request_id, status="no_match")

    elapsed = round(time.time() - started, 1)
    logger.info(
        f"[parts_request:{parts_request_id}] done in {elapsed}s "
        f"winner={winner.supplier_name if winner else 'none'}"
    )

    spoken = _spoken_summary(part["name"], offers, winner, quantity, max_price_dollars)
    return {
        "ok": True,
        "request_id": parts_request_id,
        "part_name": part["name"],
        "elapsed_seconds": elapsed,
        "offers": [o.to_dict() for o in offers],
        "recommended": winner.to_dict() if winner else None,
        "spoken_summary": spoken,
    }


def _spoken_summary(
    part_name: str,
    offers: list[SupplierOffer],
    winner: SupplierOffer | None,
    quantity: int,
    max_price_dollars: float | None,
) -> str:
    if winner is None:
        backordered = [o for o in offers if o.lead_time_days]
        if backordered:
            soonest = min(backordered, key=lambda o: o.lead_time_days or 99)
            return (
                f"Nobody has the {part_name} in stock right now. "
                f"{soonest.supplier_name} can get it in {soonest.lead_time_days} days. "
                "Want me to put it on order?"
            )
        return f"I struck out — none of our suppliers carry the {part_name}."

    qty_str = f"{quantity} " if quantity > 1 else ""
    base = (
        f"{winner.supplier_name} has {qty_str}{part_name} in stock at "
        f"${winner.price_dollars:.0f}"
    )
    others_in_stock = [
        o for o in offers if o.supplier_id != winner.supplier_id and o.in_stock and o.price_dollars
    ]
    if others_in_stock:
        runner_up = min(others_in_stock, key=lambda o: o.price_dollars or 9e9)
        base += (
            f". {runner_up.supplier_name} also has it but at ${runner_up.price_dollars:.0f}"
        )
    base += ". Want me to call them back and put one on hold?"
    return base


async def _smoke_test() -> None:
    db.init_db()
    result = await source_parts("Rinnai RU199i", quantity=1, max_price_dollars=1200)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(override=True)
    asyncio.run(_smoke_test())
