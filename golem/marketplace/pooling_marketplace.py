import logging
from typing import ClassVar, Dict, List

from golem.marketplace.marketplace import Offer, RequestorMarketStrategy

logger = logging.getLogger(__name__)


class RequestorPoolingMarketStrategy(RequestorMarketStrategy):

    _pools: ClassVar[Dict[str, List[Offer]]] = dict()

    @classmethod
    def add(cls, offer: Offer):
        if offer.task_id not in cls._pools:
            cls._pools[offer.task_id] = []
        cls._pools[offer.task_id].append(offer)

        logger.debug(
            "Offer accepted & added to pool. offer=%s",
            offer,
        )

    @classmethod
    def get_task_offer_count(cls, task_id: str) -> int:
        return len(cls._pools[task_id]) if task_id in cls._pools else 0

    @classmethod
    def clear_offers_for_task(cls, task_id) -> None:
        if task_id in cls._pools:
            _ = cls._pools.pop(task_id)

    @classmethod
    def reset(cls) -> None:
        cls._pools = dict()