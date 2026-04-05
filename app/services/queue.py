import json
import logging
from redis import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

QUEUE_KEY = "crawl_jobs"
EVENTS_CHANNEL = "price_events"


def redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def enqueue_job(payload: dict) -> None:
    r = redis_client()
    r.rpush(QUEUE_KEY, json.dumps(payload))


def enqueue_vendor_crawl(vendor_id: int) -> None:
    enqueue_job({"type": "crawl_vendor", "vendor_id": vendor_id})


def enqueue_listing_crawl(listing_id: int) -> None:
    enqueue_job({"type": "crawl_listing", "listing_id": listing_id})


def publish_event(payload: dict) -> None:
    r = redis_client()
    r.publish(EVENTS_CHANNEL, json.dumps(payload))
