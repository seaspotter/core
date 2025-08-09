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
    client: ModbusTcpClient_

class SaxpowerCounter(AbstractCounter):
    def __init__(self, component_config: SaxpowerCounterSetup, **kwargs: Any) -> None:
        self.component_config = component_config
        self.kwargs: KwargsDict = kwargs

    def initialize(self) -> None:
        self.__device_id: int = self.kwargs['device_id']
        self.__tcp_client: ModbusTcpClient_ = self.kwargs['client']
        self.__modbus_id: int = self.kwargs['modbus_id']
        self.sim_counter = SimCounter(self.__device_id, self.component_config.id, prefix="evu")
        self.store = get_counter_value_store(self.component_config.id)
        self.fault_state = FaultState(ComponentInfo.from_component_config(self.component_config))

    def update(self) -> None:
        # Hardcoded Modbus slave ID for counter
        UNIT_ID = 40

        power = self.__tcp_client.read_holding_registers(40110, ModbusDataType.INT_16,
                                                         wordorder=Endian.Little, unit=UNIT_ID)
        currents = self.__tcp_client.read_holding_registers(40100, [ModbusDataType.INT_16] * 3,
                                                            wordorder=Endian.Little, unit=UNIT_ID)
        powers = self.__tcp_client.read_holding_registers(40103, [ModbusDataType.INT_16] * 3,
                                                          wordorder=Endian.Little, unit=UNIT_ID)
        power_factor = self.__tcp_client.read_holding_registers(40106, ModbusDataType.INT_16,
                                                                wordorder=Endian.Little, unit=UNIT_ID)
        voltages = self.__tcp_client.read_holding_registers(40107, [ModbusDataType.INT_16] * 3,
                                                            wordorder=Endian.Little, unit=UNIT_ID)
        frequency = self.__tcp_client.read_holding_registers(40087, ModbusDataType.UINT_16,
                                                             wordorder=Endian.Little, unit=UNIT_ID)

        imported, exported = self.sim_counter.sim_count(power)

        currents = [value / 100 for value in currents]
        voltages = [value / 10 for value in voltages]
    
        counter_state = CounterState(
            imported=imported,
            exported=exported,
            power=power,
            powers=powers,
            voltages=voltages,
            currents=currents,
            frequency=frequency,
            power_factors=[power_factor] * 3
        )
        self.store.set(counter_state)


component_descriptor = ComponentDescriptor(configuration_factory=SungrowCounterSetup)
