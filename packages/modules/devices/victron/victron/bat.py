#!/usr/bin/env python3
import logging
from typing import Any, Optional, TypedDict

from modules.common import modbus
from modules.common.abstract_device import AbstractBat
from modules.common.component_state import BatState
from modules.common.component_type import ComponentDescriptor
from modules.common.fault_state import ComponentInfo, FaultState
from modules.common.modbus import ModbusDataType
from modules.common.simcount import SimCounter
from modules.common.store import get_bat_value_store
from modules.devices.victron.victron.config import VictronBatSetup
from modules.common.utils.peak_filter import PeakFilter

log = logging.getLogger(__name__)


class KwargsDict(TypedDict):
    device_id: int
    client: modbus.ModbusTcpClient_


class VictronBat(AbstractBat):
    def __init__(self, component_config: VictronBatSetup, **kwargs: Any) -> None:
        self.component_config = component_config
        self.kwargs: KwargsDict = kwargs

    def initialize(self) -> None:
        self.__device_id: int = self.kwargs['device_id']
        self.__tcp_client: modbus.ModbusTcpClient_ = self.kwargs['client']
        self.sim_counter = SimCounter(self.__device_id, self.component_config.id, prefix="speicher")
        self.store = get_bat_value_store(self.component_config.id)
        self.fault_state = FaultState(ComponentInfo.from_component_config(self.component_config))
        self.last_mode = 'Undefined'
        self.peak_filter = PeakFilter("bat", self.component_config.id, self.fault_state)

    def update(self) -> None:
        modbus_id = self.component_config.configuration.modbus_id
        with self.__tcp_client:
            power = self.__tcp_client.read_holding_registers(842, ModbusDataType.INT_16, unit=modbus_id)
            soc = self.__tcp_client.read_holding_registers(843, ModbusDataType.UINT_16, unit=modbus_id)

        self.peak_filter.check_values(power)
        imported, exported = self.sim_counter.sim_count(power)
        bat_state = BatState(
            power=power,
            soc=soc,
            imported=imported,
            exported=exported
        )
        self.store.set(bat_state)

    def set_power_limit(self, power_limit: Optional[int]) -> None:
        modbus_id = self.component_config.configuration.modbus_id
        vebus_id = self.component_config.configuration.vebus_id
        # Wenn Victron Dynamic ESS aktiv, erfolgt keine weitere Regelung in openWB
        dynamic_ess_mode = self.__tcp_client.read_holding_registers(5400, ModbusDataType.UINT_16, unit=modbus_id)
        if dynamic_ess_mode == 1:
            log.debug("Dynamic ESS Mode ist aktiv, daher erfolgt keine Regelung des Speichers durch openWB")
            return

        phases = self.__tcp_client.read_holding_registers(28, ModbusDataType.UINT_16, unit=vebus_id)
        if phases not in (1, 3):
            log.warning(f"Ungültige Phasenzahl: {phases}. Erwartet: 1 oder 3. Batteriesteuerung wird übersprungen.")
            return
        
        if power_limit is None:
            if self.last_mode is not None:
                # ESS Mode 1 für Selbstregelung mit Phasenkompensation setzen
                self.__tcp_client.write_register(2902, 1, data_type=ModbusDataType.UINT_16, unit=modbus_id)
                self.last_mode = None
                log.debug("Keine Batteriesteuerung, Selbstregelung durch Wechselrichter")

        else:
            # ESS Mode 3 für externe Steuerung
            self.__tcp_client.write_register(2902, 3, data_type=ModbusDataType.UINT_16, unit=modbus_id)
            self.last_mode = 'limited'
           
            # Phasenleistung berechnen 
            power_value = int(power_limit / phases)

            self.__tcp_client.write_register(37, power_value,
                                             data_type=ModbusDataType.INT_16, unit=vebus_id)
            if phases == 3:
                self.__tcp_client.write_register(40, power_value,
                                                 data_type=ModbusDataType.INT_16, unit=vebus_id)
                self.__tcp_client.write_register(41, power_value,
                                                 data_type=ModbusDataType.INT_16, unit=vebus_id)

            log.debug(f"Aktive Batteriesteuerung. Victron mit {phases} Phase(n). "
                      f"power_limit: {power_limit} W, pro Phase: {power_value} W")

        phase_l1 = self.__tcp_client.read_holding_registers(37, ModbusDataType.INT_16, unit=vebus_id)
        log.debug(f"Wert rückgelesen Register 37: {phase_l1}")
        phase_l2 = self.__tcp_client.read_holding_registers(40, ModbusDataType.INT_16, unit=vebus_id)
        log.debug(f"Wert rückgelesen Register 40: {phase_l2}")
        phase_l3 = self.__tcp_client.read_holding_registers(41, ModbusDataType.INT_16, unit=vebus_id)
        log.debug(f"Wert rückgelesen Register 41: {phase_l3}")

    def power_limit_controllable(self) -> bool:
        return True


component_descriptor = ComponentDescriptor(configuration_factory=VictronBatSetup)
