from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Product:
    platform: str
    product_id: str
    title: str
    price: float
    mrp: Optional[float]
    url: str
    in_stock: bool = True
    image: str = ""
    source: str = ""  # "json" | "dom" (debugging aid)
    extra: dict = field(default_factory=dict)

    @property
    def discount_pct(self) -> float:
        if not self.mrp or self.mrp <= 0:
            return 0.0
        return round((self.mrp - self.price) / self.mrp * 100, 1)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.product_id}"
