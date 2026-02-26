#!/usr/bin/env python3
import logging
from typing import TypedDict, Any, Optional

from modules.common.abstract_device import AbstractBat
from modules.common.component_state import BatState
from modules.common.component_type import ComponentDescriptor
from modules.common.fault_state import ComponentInfo, FaultState
from modules.common.modbus import ModbusTcpClient_, ModbusDataType
from modules.common.store import get_bat_value_store
from modules.devices.sma.sma_sunny_boy.config import SmaSunnyBoyBatSetup

log = logging.getLogger(__name__)


class KwargsDict(TypedDict):
    client: ModbusTcpClient_


class SunnyBoyBat(AbstractBat):
    SMA_UINT32_NAN = 0xFFFFFFFF
    SMA_UINT_64_NAN = 0xFFFFFFFFFFFFFFFF

    def __init__(self, component_config: SmaSunnyBoyBatSetup, **kwargs: Any) -> None:
        self.component_config = component_config
        self.kwargs: KwargsDict = kwargs

    def initialize(self) -> None:
        self.__tcp_client: ModbusTcpClient_ = self.kwargs['client']
        self.store = get_bat_value_store(self.component_config.id)
        self.fault_state = FaultState(ComponentInfo.from_component_config(self.component_config))
        self.last_mode = 'Undefined'

    def update(self) -> None:
        unit = self.component_config.configuration.modbus_id

        soc = self.__tcp_client.read_holding_registers(30845, ModbusDataType.UINT_32, unit=unit)
        imp = self.__tcp_client.read_holding_registers(31393, ModbusDataType.INT_32, unit=unit)
        exp = self.__tcp_client.read_holding_registers(31395, ModbusDataType.INT_32, unit=unit)

        if soc == SMA_UINT32_NAN
        # If the storage is empty and nothing is produced on the DC side, the inverter does not supply any values.
            soc = 0
            power = 0
        else
            if imp > 5:
                power = imp
            else:
                power = exp * -1

        exported = self.__tcp_client.read_holding_registers(31401, ModbusDataType.UINT_64, unit=unit)
        imported = self.__tcp_client.read_holding_registers(31397, ModbusDataType.UINT_64, unit=unit)
    
        if exported == self.SMA_UINT_64_NAN or imported == self.SMA_UINT_64_NAN:
            raise ValueError(f'Batterie lieferte nicht plausible Werte. Export: {exported}, Import: {imported}. ',
                             'Sobald die Batterie geladen/entladen wird sollte sich dieser Wert ändern, ',
                             'andernfalls kann ein Defekt vorliegen.')

        bat_state = BatState(
            power=power,
            soc=soc,
            imported=imported,
            exported=exported
        )
        self.store.set(bat_state)

    def set_power_limit(self, power_limit: Optional[int]) -> None:
        unit = self.component_config.configuration.modbus_id
        log.debug(f'last_mode: {self.last_mode}')

        if power_limit is None:
            log.debug("Keine aktive Batteriesteuerung aktiv, Selbstregelung durch Wechselrichter")
            if self.last_mode is not None:
                self.__tcp_client.write_register(40151, 803, data_type=ModbusDataType.UINT_32, unit=unit)
                self.__tcp_client.write_register(40149, 0, data_type=ModbusDataType.UINT_32, unit=unit)
                self.last_mode = None
        else:
            log.debug("Aktive Batteriesteuerung aktiv")
            if self.last_mode != 'limited':
                self.__tcp_client.write_register(40151, 802, data_type=ModbusDataType.UINT_32, unit=unit)
                self.last_mode = 'limited'
            power_value = int(power_limit) * -1
            self.__tcp_client.write_register(40149, power_value, data_type=ModbusDataType.UINT_32, unit=unit)

    def power_limit_controllable(self) -> bool:
        return True


component_descriptor = ComponentDescriptor(configuration_factory=SmaSunnyBoyBatSetup)
