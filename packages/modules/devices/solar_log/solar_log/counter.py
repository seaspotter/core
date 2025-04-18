#!/usr/bin/env python3
import logging
from typing import Dict, TypedDict, Any

from modules.common.abstract_device import AbstractCounter
from modules.common.component_state import CounterState
from modules.common.component_type import ComponentDescriptor
from modules.common.fault_state import ComponentInfo, FaultState
from modules.common.simcount import SimCounter
from modules.common.store import get_counter_value_store
from modules.devices.solar_log.solar_log.config import SolarLogCounterSetup

log = logging.getLogger(__name__)


class KwargsDict(TypedDict):
    device_id: int


class SolarLogCounter(AbstractCounter):
    def __init__(self, component_config: SolarLogCounterSetup, **kwargs: Any) -> None:
        self.component_config = component_config
        self.kwargs: KwargsDict = kwargs

    def initialize(self) -> None:
        self.__device_id: int = self.kwargs['device_id']
        self.sim_counter = SimCounter(self.__device_id, self.component_config.id, prefix="bezug")
        self.store = get_counter_value_store(self.component_config.id)
        self.fault_state = FaultState(ComponentInfo.from_component_config(self.component_config))

    def update(self, response: Dict) -> None:
        self.store_values(self.get_power(response))

    def store_values(self, power) -> None:
        imported, exported = self.sim_counter.sim_count(power)

        self.store.set(CounterState(
            imported=imported,
            exported=exported,
            power=power
        ))

    def get_power(self, response: Dict) -> int:
        return int(float(response["801"]["170"]["110"]))


component_descriptor = ComponentDescriptor(configuration_factory=SolarLogCounterSetup)
