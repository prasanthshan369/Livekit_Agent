import asyncio
import logging
import random
import os
import httpx
from datetime import datetime, date, timedelta
from typing import Annotated, List, Optional, Union
from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.agents import llm
from livekit.agents.llm import function_tool
from livekit.plugins import (
    google,
    noise_cancellation,
)
from prompt import AGENT_INSTRUCTIONS, AGENT_RESPONSE

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("roomintel-agent")

# API Base URL from environment or fallback
API_BASE_URL = os.getenv("API_BASE_URL", "https://roomintel-backend.onrender.com/api/v1")

def parse_date(date_str: str) -> Optional[date]:
    """Helper to parse various date formats into a date object."""
    formats = ["%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=AGENT_INSTRUCTIONS)

    @function_tool(description="Search for available rooms based on keywords, price, or guest count.")
    async def search_rooms(
        self,
        query: Annotated[Optional[str], "Search keyword like 'villa', 'ocean', 'suite'"] = None,
        max_price: Annotated[Optional[float], "Maximum price per night"] = None,
        min_guests: Annotated[Optional[int], "Minimum number of guests the room must accommodate"] = None,
    ):
        """Searches for rooms in the live inventory using filters."""
        logging.info(f"Agent Tool: Searching rooms (query={query}, max_price={max_price}, min_guests={min_guests})")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{API_BASE_URL}/site/rooms", timeout=10.0)
                response.raise_for_status()
                rooms = response.json().get("data", [])
                
                filtered = []
                for r in rooms:
                    # Filter by query
                    if query and query.lower() not in (r.get("name", "") + r.get("description", "")).lower():
                        continue
                    # Filter by price
                    if max_price is not None and r.get("price", 0) > max_price:
                        continue
                    # Filter by guests
                    if min_guests is not None and r.get("maxAdults", 0) < min_guests:
                        continue
                        
                    filtered.append({
                        "name": r.get("name"),
                        "price": r.get("price"),
                        "description": r.get("description"),
                        "id": r.get("_id"),
                        "slug": r.get("slug")
                    })
                
                if not filtered:
                    return {"message": "I couldn't find any rooms matching those specific criteria. Would you like to see our general listings?"}
                
                return {"rooms": filtered[:5], "message": f"I found {len(filtered)} rooms that might interest you."}
        except Exception as e:
            logger.error(f"Search error: {e}")
            return {"error": "The search system is briefly offline. I can assist with our standard suites instead."}

    @function_tool(description="Get full details and features for a specific room.")
    async def get_room_details(
        self,
        room_slug: Annotated[str, "The unique slug or ID of the room"],
    ):
        """Fetches comprehensive details for one specific room."""
        logging.info(f"Agent Tool: Getting details for {room_slug}")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{API_BASE_URL}/site/rooms/{room_slug}", timeout=10.0)
                response.raise_for_status()
                data = response.json()
                room = data.get("data", {})
                
                if not room:
                    return {"message": "I couldn't retrieve the details for that specific room right now."}
                
                return {
                    "name": room.get("name"),
                    "description": room.get("description"),
                    "price": room.get("price"),
                    "amenities": [a.get("name") if isinstance(a, dict) else a for a in room.get("amenities", [])],
                    "size": room.get("size"),
                    "max_guests": room.get("maxAdults"),
                    "message": f"The {room.get('name')} is a {room.get('size')} space and offers features like {', '.join([a.get('name') if isinstance(a, dict) else a for a in room.get('amenities', [])][:3])}."
                }
        except Exception as e:
            logger.error(f"Details error: {e}")
            return {"error": "I'm having trouble connecting to the room's feature list."}

    @function_tool(description="Check if a specific room is available for a range of dates.")
    async def check_availability(
        self,
        room_name: Annotated[str, "The name or type of room requested"],
        check_in: Annotated[str, "Check-in date (YYYY-MM-DD)"],
        check_out: Annotated[str, "Check-out date (YYYY-MM-DD)"],
    ):
        """Checks availability by comparing requested dates against the booked-dates roster."""
        logging.info(f"Agent Tool: Checking availability for {room_name} from {check_in} to {check_out}")
        
        cin = parse_date(check_in)
        cout = parse_date(check_out)
        
        if not cin or not cout:
            return {"error": "Please provide dates in YYYY-MM-DD format so I can verify them accurately."}
            
        try:
            async with httpx.AsyncClient() as client:
                # 1. Resolve room name
                rooms_resp = await client.get(f"{API_BASE_URL}/site/rooms", timeout=10.0)
                rooms = rooms_resp.json().get("data", [])
                match = next((r for r in rooms if room_name.lower() in r.get("name", "").lower()), None)
                
                if not match:
                    return {"available": False, "message": f"I don't see a '{room_name}' in our current inventory. I have rooms like the {rooms[0].get('name')} if you're interested." if rooms else "No rooms found."}

                # 2. Get booked dates
                booked_resp = await client.get(f"{API_BASE_URL}/site/bookings/booked-dates", timeout=10.0)
                booked_data = booked_resp.json().get("data", [])
                
                room_busy = next((d for d in booked_data if d.get("roomId") == match["_id"]), None)
                if room_busy:
                    busy_dates = [parse_date(d) for d in room_busy.get("dates", [])]
                    # Check for overlap: Is any date between cin and cout (exclusive of cout usually) busy?
                    current = cin
                    while current < cout:
                        if current in busy_dates:
                            return {
                                "available": False, 
                                "message": f"Our records show the {match['name']} is occupied on {current}. May I suggest another date or room?"
                            }
                        current += timedelta(days=1)
                
                return {
                    "available": True,
                    "details": {
                        "room": match["name"],
                        "price": match["price"],
                        "id": match["_id"]
                    },
                    "message": f"The {match['name']} is fully available from {check_in} to {check_out} at ${match['price']} per night."
                }
        except Exception as e:
            logger.error(f"Availability error: {e}")
            return {"error": "The reservation system is momentarily syncing. I can confirm this for you in just a minute."}

    @function_tool(description="Proceed with a booking for a guest.")
    async def confirm_booking(
        self,
        guest_name: Annotated[str, "Full name of the guest"],
        room_name: Annotated[str, "The name of the room chosen"],
        check_in: Annotated[str, "Check-in date"],
        check_out: Annotated[str, "Check-out date"],
    ):
        """Simulates creation of a booking record."""
        logging.info(f"Agent Tool: Confirming booking for {guest_name}")
        booking_id = f"RI-{random.randint(10000, 99999)}"
        return {
            "success": True,
            "booking_reference": booking_id,
            "message": f"Marvelous, {guest_name}. I have reserved the {room_name} for you from {check_in} to {check_out}. Your booking reference is {booking_id}."
        }

async def entrypoint(ctx: agents.JobContext):
    # Connect to the room
    await ctx.connect()

    # Pre-warm the backend to avoid Render cold-start latency
    async def warm_up():
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"{API_BASE_URL}/site/rooms", timeout=5.0)
                logger.info("Backend pre-warmed successfully.")
        except Exception as e:
            logger.warning(f"Backend warm-up failed: {e}")

    asyncio.create_task(warm_up())

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            voice="Puck",
            temperature=0.6,
            instructions=AGENT_INSTRUCTIONS,
        ),
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )

    @ctx.room.on("data_received")
    def on_data_received(data: rtc.DataPacket):
        if data.participant and data.participant.identity != ctx.room.local_participant.identity:
            try:
                msg = data.data.decode("utf-8")
                if msg.startswith("USER: "):
                    msg = msg[len("USER: "):]
                asyncio.create_task(session.generate_reply(instructions=f"Handle this user request: {msg}"))
            except Exception as e:
                logger.error(f"Sync error: {e}")

    await asyncio.sleep(2)
    try:
        await session.generate_reply(instructions=AGENT_RESPONSE)
    except Exception as e:
        logger.warning(f"Initial greeting skipped: {e}")

if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="my-agent",
        )
    )
