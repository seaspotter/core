#!/usr/bin/env python3
from typing import TypedDict, Any

from modules.common.abstract_device import AbstractCounter
from modules.common.component_state import CounterState
from modules.common.component_type import ComponentDescriptor
from modules.common.fault_state import ComponentInfo, FaultState
from modules.common.modbus import Endian, ModbusDataType, ModbusTcpClient_
from modules.common.simcount._simcounter import SimCounter
from modules.common.store import get_counter_value_store
from modules.devices.saxpower.saxpower.config import SaxpowerCounterSetup


class KwargsDict(TypedDict):
    device_id: int
    client: modbus.ModbusTcpClient_
    modbus_id: int


class SaXpowerCounter(AbstractCounter):
    def __init__(self, component_config: SaxpowerCounterSetup, **kwargs: Any) -> None:
        self.component_config = component_config
        self.kwargs: KwargsDict = kwargs

    def initialize(self) -> None:
         self.__device_id: int = self.kwargs['device_id']
        self.__tcp_client: modbus.ModbusTcpClient_ = self.kwargs['client']
        self.__modbus_id: int = self.kwargs['modbus_id']
        self.sim_counter = SimCounter(self.device_config.id, self.component_config.id, prefix="evu")
        self.store = get_counter_value_store(self.component_config.id)
        self.fault_state = FaultState(ComponentInfo.from_component_config(self.component_config))

    def update(self, pv_power: float):
        unit = self.device_config.configuration.modbus_id
        power = self.__tcp_client.read_input_registers(13009, ModbusDataType.INT_32, wordorder=Endian.Little, unit=unit) * -1


        imported, exported = self.sim_counter.sim_count(power)

        counter_state = CounterState(
            imported=imported,
            exported=exported,
            power=power,
            powers=powers,
            voltages=voltages,
            frequency=frequency,
            power_factors=[power_factor] * 3
        )
        self.store.set(counter_state)


component_descriptor = ComponentDescriptor(configuration_factory=SungrowCounterSetup)
