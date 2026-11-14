from dataclasses import dataclass

from modules.electricity_pricing.flexible_tariffs.fixed_hours.config import FixedHoursTariff


@dataclass
class FixedHoursFeedInPrice(FixedHoursTariff):
    name: str = "Fester Einspeisepreis"
