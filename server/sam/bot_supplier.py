"""Supplier bot — role-plays a parts desk employee at one supply house.

Lightweight async LLM agent (not a full Pipecat pipeline). Each instance is bound
to one supplier and only sees that supplier's inventory. Sam's outbound caller
spins one of these up per outbound call and drives a multi-turn conversation
against it.

The LLM is Nemotron-3-Super-120B (same as Sam) for the eyes-on-Cekura demo, with
a fallback to OpenAI if Nemotron isn't reachable so local dev never blocks. Tool
calls go through OpenAI's tool-call schema since vLLM supports it.

Personality matters for the demo: ABC is friendly and chatty, Ferguson is busy
and curt, Western is gruff and impatient. This gives Sam something to actually
navigate, and gives Cekura real edge cases to score on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from . import db


@dataclass
class SupplierBot:
    """A stateful conversation with one supplier's parts desk."""

    supplier_id: int
    history: list[dict] = field(default_factory=list)
    _supplier: dict | None = None
    _client: AsyncOpenAI | None = None
    _model: str = ""

    def __post_init__(self) -> None:
        self._supplier = db.get_supplier(self.supplier_id)
        if not self._supplier:
            raise ValueError(f"Unknown supplier id: {self.supplier_id}")

        nemotron_url = os.environ.get("NEMOTRON_LLM_URL")
        if nemotron_url:
            self._client = AsyncOpenAI(
                api_key=os.environ.get("NEMOTRON_LLM_API_KEY", "EMPTY"),
                base_url=nemotron_url,
            )
            self._model = os.environ.get("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super")
        else:
            self._client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
            self._model = "gpt-4o-mini"

        self.history.append({"role": "system", "content": self._system_prompt()})

    def _system_prompt(self) -> str:
        s = self._supplier
        personality_lines = {
            "friendly": (
                "Personality: warm, helpful, willing to chat. Confirm part numbers back. "
                "Offer alternatives if asked. Use first names if given."
            ),
            "busy": (
                "Personality: friendly but rushed. Keep answers SHORT — 1-2 sentences max. "
                "Don't volunteer extra info. Skip pleasantries."
            ),
            "gruff": (
                "Personality: impatient, no-nonsense, slightly annoyed at being interrupted. "
                "Answer the question literally. Don't elaborate. May ask 'anything else?' "
                "tersely when done."
            ),
        }
        return (
            f"You are a parts desk employee at {s['name']}, a plumbing supply house. "
            f"You answer the phone when contractors call asking about parts.\n\n"
            f"{personality_lines.get(s['personality'], personality_lines['friendly'])}\n\n"
            "Use the tools to look up real inventory and prices for YOUR store. "
            "Never make up prices or stock levels — always call check_part first. "
            "If they ask about a part you don't recognize, say so plainly.\n\n"
            "Conversation style:\n"
            "- This is a phone call. Talk like a human on the phone, not a chatbot.\n"
            "- Short turns. Fragments are fine.\n"
            "- Read prices like a person: 'eleven fifty' or 'a thousand one fifty', not '$1150.00'.\n"
            "- If the caller wants to hold a unit, confirm with their name and say you'll "
            "set one aside.\n"
            "- When the conversation is clearly over, say goodbye briefly."
        )

    @property
    def greeting(self) -> str:
        return self._supplier["greeting"]

    def _tool_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "check_part",
                    "description": (
                        "Look up a part in YOUR store's inventory. Returns whether it's "
                        "in stock, the price, quantity available, and lead time if "
                        "back-ordered. Use whenever the caller asks about a specific part."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "part_query": {
                                "type": "string",
                                "description": (
                                    "Part name, SKU, or description the caller mentioned. "
                                    "E.g. 'Rinnai RU199i' or 'Moen 1225 cartridge'."
                                ),
                            }
                        },
                        "required": ["part_query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hold_part",
                    "description": (
                        "Set aside one or more units for the caller to pick up. Call this "
                        "only after the caller has explicitly asked to hold/reserve the "
                        "part AND given a name to hold it under."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "part_query": {"type": "string"},
                            "quantity": {"type": "integer", "default": 1},
                            "hold_for_name": {"type": "string"},
                        },
                        "required": ["part_query", "hold_for_name"],
                    },
                },
            },
        ]

    async def _run_tool(self, name: str, args: dict) -> dict:
        if name == "check_part":
            part = db.find_part(args["part_query"])
            if not part:
                return {"found": False, "message": "Don't carry that, or not under that name."}
            inv = db.get_inventory(self.supplier_id, part["id"])
            if not inv:
                return {"found": False, "message": "Not in our system."}
            return {
                "found": True,
                "part_name": part["name"],
                "sku": part["sku"],
                "in_stock": bool(inv["in_stock"]),
                "quantity_available": inv["quantity"],
                "price_dollars": round(inv["price_cents"] / 100, 2),
                "lead_time_days": inv["lead_time_days"],
            }
        if name == "hold_part":
            part = db.find_part(args["part_query"])
            if not part:
                return {"ok": False, "reason": "Can't hold what we don't carry."}
            inv = db.get_inventory(self.supplier_id, part["id"])
            qty = int(args.get("quantity", 1))
            if not inv or not inv["in_stock"] or inv["quantity"] < qty:
                return {"ok": False, "reason": "Not enough on hand to hold."}
            return {
                "ok": True,
                "part_name": part["name"],
                "quantity": qty,
                "held_for": args["hold_for_name"],
                "pickup_window_hours": 24,
            }
        return {"error": f"unknown tool {name}"}

    async def reply(self, caller_text: str) -> str:
        """Take what Sam said, run the LLM (with tool calls), return what the supplier says."""
        self.history.append({"role": "user", "content": caller_text})

        for _ in range(4):
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=self.history,
                tools=self._tool_schemas(),
                temperature=0.4,
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                self.history.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await self._run_tool(tc.function.name, args)
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result),
                        }
                    )
                continue

            text = msg.content or ""
            self.history.append({"role": "assistant", "content": text})
            return text

        # Tool-call loop ran away — return a graceful fallback so the call doesn't hang.
        fallback = "Sorry, hit a snag looking that up. Can you say the part one more time?"
        self.history.append({"role": "assistant", "content": fallback})
        return fallback


async def _smoke_test() -> None:
    """Quick local sanity check: ask each supplier about a Rinnai RU199i."""
    import asyncio

    db.init_db()
    for supplier in db.list_suppliers():
        print(f"\n=== {supplier['name']} ===")
        bot = SupplierBot(supplier_id=supplier["id"])
        print(f"BOT: {bot.greeting}")
        for caller_line in [
            "Hi, calling about a Rinnai RU199i tankless water heater. Got any in stock?",
            "What's your price on it?",
            "Thanks, that's all I needed.",
        ]:
            print(f"SAM: {caller_line}")
            response = await bot.reply(caller_line)
            print(f"BOT: {response}")


if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv

    load_dotenv(override=True)
    asyncio.run(_smoke_test())
