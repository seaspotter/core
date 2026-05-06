import logging

from control import data
from helpermodules import compatibility
from modules.common.component_state import InverterState
from modules.common.store import ValueStore
from modules.common.store._api import LoggingValueStore
from modules.common.store._broker import pub_to_broker
from modules.common.store.ramdisk import files

log = logging.getLogger(__name__)


class InverterValueStoreRamdisk(ValueStore[InverterState]):
    def __init__(self, component_num: int) -> None:
        self.__pv = files.pv[component_num - 1]

    def set(self, inverter_state: InverterState):
        if inverter_state.power is not None:
            self.__pv.power.write(int(inverter_state.power))
        self.__pv.energy.write(inverter_state.exported)
        self.__pv.energy_k.write(inverter_state.exported / 1000)
        if inverter_state.currents:
            self.__pv.currents.write(inverter_state.currents)


class InverterValueStoreBroker(ValueStore[InverterState]):
    def __init__(self, component_num: int) -> None:
        self.num = component_num

    def set(self, inverter_state: InverterState):
        self.state = inverter_state

    def update(self):
        if self.state.power is not None:
            pub_to_broker("openWB/set/pv/" + str(self.num) + "/get/power", self.state.power, 2)
        if self.state.exported is not None:
            pub_to_broker("openWB/set/pv/" + str(self.num) + "/get/exported", self.state.exported, 3)
        else:
            log.debug("Kein gültiger Zählerstand. Wert wird nicht aktualisiert.")
        if self.state.currents:
            pub_to_broker("openWB/set/pv/" + str(self.num) + "/get/currents", self.state.currents, 1)
        if self.state.serial_number is not None:
            pub_to_broker("openWB/set/pv/" + str(self.num) + "/get/serial_number", self.state.serial_number)


class PurgeInverterState:
    def __init__(self, delegate: LoggingValueStore) -> None:
        self.delegate = delegate
        self.__last_state_had_power = None  # Track if last state had power value

    def set(self, state: InverterState) -> None:
        # Check if only dc_power is available (power is None but dc_power exists)
        if state.power is None and state.dc_power is not None:
            # Only warn once when we first detect DC-only condition
            if self.__last_state_had_power is not False:
                self.delegate.delegate.fault_state.warning(
                    "Für dieses Modul steht nur die DC Leistung zur Verfügung, "
                    "die Summe der Leistungen kann daher leicht abweichen."
                )
                self.__last_state_had_power = False
        elif state.power is not None:
            # AC power is available again, clear warning if it was previously DC-only
            if self.__last_state_had_power is False:
                self.delegate.delegate.fault_state.no_error()
            self.__last_state_had_power = True
        
        self.delegate.set(state)

    def update(self) -> None:
        state = self.fix_hybrid_values(self.delegate.delegate.state)
        self.delegate.set(state)
        self.delegate.update()

    def fix_hybrid_values(self, state: InverterState) -> InverterState:
        children = data.data.counter_all_data.get_entry_of_element(self.delegate.delegate.num)["children"]
        power = state.power if state.power is not None else 0
        exported = state.exported
        imported = state.imported
        if len(children):
            hybrid = []
            for c in children:
                if c.get("type") == "bat":
                    hybrid.append(f'bat{c["id"]}')
                    break
            if len(hybrid):
                for bat in hybrid:
                    bat_get = data.data.bat_data[bat].data.get
                    power -= bat_get.power

                    exported += bat_get.imported - bat_get.exported - imported

            if state.dc_power is not None:
                # Manche Systeme werden auch aus dem Netz geladen, um einen Mindest-SoC zu halten.
                if state.dc_power == 0:
                    power = 0
        state.power = power
        state.exported = exported
        return state


def get_inverter_value_store(component_num: int) -> PurgeInverterState:
    return PurgeInverterState(LoggingValueStore(
        (InverterValueStoreRamdisk if compatibility.is_ramdisk_in_use() else InverterValueStoreBroker)(component_num)
    ))
