#!/usr/bin/env python3
import logging
from typing import TypedDict, Any, Optional

from modules.common.abstract_device import AbstractBat
from modules.common.component_state import BatState
from modules.common.component_type import ComponentDescriptor
from modules.common.fault_state import ComponentInfo, FaultState
from modules.common.modbus import ModbusTcpClient_, ModbusDataType
from modules.common.store import get_bat_value_store
from modules.devices.sma.sma_sunny_island.config import SmaSunnyIslandBatSetup

log = logging.getLogger(__name__)


class KwargsDict(TypedDict):
    client: ModbusTcpClient_


class SunnyIslandBat(AbstractBat):
    def __init__(self, component_config: SmaSunnyIslandBatSetup, **kwargs: Any) -> None:
        self.component_config = component_config
        self.kwargs: KwargsDict = kwargs

    def initialize(self) -> None:
        self.__tcp_client: modbus.ModbusTcpClient_ = self.kwargs['client']
        self.store = get_bat_value_store(self.component_config.id)
        self.fault_state = FaultState(ComponentInfo.from_component_config(self.component_config))
        self.last_mode = 'Undefined'

    def read(self) -> BatState:
        unit = self.component_config.configuration.modbus_id

        with self.__tcp_client:
            soc = self.__tcp_client.read_holding_registers(30845, ModbusDataType.INT_32, unit=unit)
            power = self.__tcp_client.read_holding_registers(30775, ModbusDataType.INT_32, unit=unit) * -1
            imported, exported = self.__tcp_client.read_holding_registers(30595, [ModbusDataType.INT_32]*2, unit=unit)

        return BatState(
            power=power,
            soc=soc,
            imported=imported,
            exported=exported
        )

    def update(self) -> None:
        self.store.set(self.read())

    def set_power_limit(self, power_limit: Optional[int]) -> None:
        unit = self.component_config.configuration.modbus_id
    
        if power_limit is None:
            log.debug("Keine Batteriesteuerung gefordert, deaktiviere externe Steuerung.")
            if self.last_mode is not None:
                self.__tcp_client.write_registers(40151, [803], data_type=ModbusDataType.UINT_32, unit=unit)
                self.__tcp_client.write_registers(40149, [0], data_type=ModbusDataType.INT_32, unit=unit)
                self.last_mode = None
        else:
            log.debug("Aktive Batteriesteuerung vorhanden. Setze externe Steuerung.")
            self.__tcp_client.write_registers(40151, [802], data_type=ModbusDataType.UINT_32, unit=unit)
            self.__tcp_client.write_registers(40149, [-power_limit], data_type=ModbusDataType.INT_32, unit=unit)
            self.last_mode = 'limited'

    def power_limit_controllable(self) -> bool:
        return True


component_descriptor = ComponentDescriptor(configuration_factory=SmaSunnyIslandBatSetup)
