
from app.models.card import Card
from app.services.catalog import card_search_service
from sqlalchemy import select
import asyncio

async def get_all_card_ids(session):
    # Returns a list of all card UUIDs in the catalog.
    result = await session.execute(select(Card.id))
    return [str(row[0]) for row in result.fetchall()]

async def refresh_market_data(card_id, session):
    # Calls the same logic as the API to update pricing_summary for card_id.
    # Uses the DB session provided (so worker can batch/commit as needed).
    card = await session.get(Card, card_id)
    if not card:
        return False
    # This will trigger the same upstream fan-out and persist to card_metadata.
    await card_search_service.get_card(str(card_id))
    # Optionally, you could re-fetch the card and check if card_metadata["pricing_summary"] is present.
    return True
