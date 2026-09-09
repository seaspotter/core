"""Hausspeicher-Logik
Der Hausspeicher ist immer bestrebt, den EVU-Überschuss auf 0 zu regeln.
Wenn EVU_Überschuss vorhanden ist, lädt der Speicher. Wenn EVU-Bezug vorhanden wäre,
entlädt der Speicher, sodass kein Netzbezug stattfindet. Wenn das EV Vorrang hat, wird
eine Ladung gestartet und der Speicher hört automatisch auf zu laden, da sonst durch
das Laden des EV Bezug statt finden würde.

Sonderfall Hybrid-Systeme:
Wenn wir ein Hybrid Wechselrichter Speicher system haben das besteht aus:
20 kW PV
15kW Wechselrichter
Batterie DC
Kann es derzeit passieren das die PV 20kW erzeugt, die Batterie mit 5kW geladen wird und 15kW ins Netz gehen.
Zieht die openWB nun Überschuss (15kW Überschuss + 5kW Batterieladung = 20kW) kommt es zu 5kW Bezug weil der
Wechselrichter nur 15kW abgeben kann.

__Wie schnell regelt ein Speicher?
Je nach Speicher 1-4 Sekunden.
"""
from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import List, Optional, Tuple

from control import data
from control.algorithm.chargemodes import CONSIDERED_CHARGE_MODES_CHARGING
from control.algorithm.filter_chargepoints import get_chargepoints_with_required_current_by_chargemode
from helpermodules.constants import NO_ERROR
from modules.common.abstract_device import AbstractDevice
from modules.common.component_context import SingleComponentUpdateContext

log = logging.getLogger(__name__)


class BatConsiderationMode(Enum):
    BAT_MODE = "bat_mode"
    EV_MODE = "ev_mode"
    MIN_SOC_BAT = "min_soc_bat_mode"


class BatControlMode(Enum):
    SELF_REGULATION = "self_regulation"
    HOME_CONSUMPTION_WHILE_CHARGING = "home_consumption_while_charging"
    BLOCK_DISCHARGE = "block_discharge"
    PV_YIELD_WHILE_CHARGING = "pv_yield_while_charging"
    FORCE_CHARGE_BELOW_PRICE = "force_charge_below_price"
    BLOCK_DISCHARGE_ABOVE_PRICE = "block_discharge_above_price"
    MANUAL = "manual"
    # Ladeleistung begrenzen, Speicher bleibt sonst in Eigenregelung (Entladung, Timing etc.
    # werden nicht vorgegeben) - nutzt set_charge_power_limit statt set_power_limit, siehe
    # AbstractBat. Auch auf Speichern nutzbar, die keine bidirektionale Vorgabe unterstützen.
    LIMIT_CHARGE_POWER = "limit_charge_power"
    # Prognosebasierte Lastspitzenkappung - noch nicht implementiert, siehe
    # get_power_limit()/PEAK_SHAVING. Modus bereits waehlbar, damit die UI/Migration nicht
    # nochmal angepasst werden muss, sobald die Logik dahinter umgesetzt wird.
    PEAK_SHAVING = "peak_shaving"


class ManualControl(Enum):
    CHARGE = "charge"
    STOP = "stop"
    # Kein DISCHARGE: den Speicher unabhängig vom Hausverbrauch aktiv zu entladen (Netz-
    # einspeisung aus dem Speicher erzwingen) ist in Deutschland nicht erlaubt. Wird in der UI
    # als Option sichtbar, aber deaktiviert dargestellt, damit klar ist, dass sie bewusst fehlt.


class CurrentState(Enum):
    STARTUP = "startup"
    ACTIVE = "active"
    IDLE = "idle"


@dataclass
class Config:
    configured: bool = field(default=False, metadata={"topic": "config/configured"})
    control_mode: str = field(default=BatControlMode.SELF_REGULATION.value,
                              metadata={"topic": "config/control_mode"})
    # nur relevant fuer control_mode == MANUAL
    manual_control: str = field(default=ManualControl.STOP.value, metadata={"topic": "config/manual_control"})
    # kW-Vorgabe fuer MANUAL+CHARGE; None/0 = mit maximaler Ladeleistung laden
    manual_power: Optional[int] = field(default=None, metadata={"topic": "config/manual_power"})
    # W-Obergrenze fuer LIMIT_CHARGE_POWER
    charge_power_limit: Optional[int] = field(default=None, metadata={"topic": "config/charge_power_limit"})
    bat_control_min_soc: int = field(default=10, metadata={"topic": "config/bat_control_min_soc"})
    bat_control_max_soc: int = field(default=90, metadata={"topic": "config/bat_control_max_soc"})
    # Preisgrenze fuer BLOCK_DISCHARGE_ABOVE_PRICE
    price_limit: float = field(default=0.30, metadata={"topic": "config/price_limit"})
    # Preisgrenze fuer FORCE_CHARGE_BELOW_PRICE
    charge_limit: float = field(default=0.30, metadata={"topic": "config/charge_limit"})


def config_factory() -> Config:
    return Config()


@dataclass
class Get:
    power_limit_controllable: bool = field(default=False, metadata={"topic": "get/power_limit_controllable"})
    charge_power_limit_controllable: bool = field(default=False, metadata={
        "topic": "get/charge_power_limit_controllable"})
    soc: float = field(default=0, metadata={"topic": "get/soc"})
    daily_exported: float = field(default=0, metadata={"topic": "get/daily_exported"})
    daily_imported: float = field(default=0, metadata={"topic": "get/daily_imported"})
    fault_str: str = field(default=NO_ERROR, metadata={"topic": "get/fault_str"})
    fault_state: int = field(default=0, metadata={"topic": "get/fault_state"})
    imported: float = field(default=0, metadata={"topic": "get/imported"})
    exported: float = field(default=0, metadata={"topic": "get/exported"})
    power: float = field(default=0, metadata={"topic": "get/power"})


def get_factory() -> Get:
    return Get()


@dataclass
class Set:
    charging_power_left: float = field(default=0, metadata={"topic": "set/charging_power_left"})
    power_limit: Optional[float] = field(default=None, metadata={"topic": "set/power_limit"})
    charge_power_limit: Optional[float] = field(default=None, metadata={"topic": "set/charge_power_limit"})
    regulate_up: bool = field(default=False, metadata={"topic": "set/regulate_up"})
    hysteresis_discharge: bool = field(default=False, metadata={"topic": "set/hysteresis_discharge"})
    current_state: str = field(default=CurrentState.STARTUP.value, metadata={"topic": "set/current_state"})
    set_limit: bool = False


def set_factory() -> Set:
    return Set()


@dataclass
class BatAllData:
    config: Config = field(default_factory=config_factory)
    get: Get = field(default_factory=get_factory)
    set: Set = field(default_factory=set_factory)


class BatAll:
    ERROR_CONFIG_MAX_AC_OUT = ("Maximale Entladeleistung des Wechselrichters  muss bei einem Hybrid-System " +
                               "konfiguriert werden. Bitte im Lastmanagement die maximale Ausgangsleistung des"
                               + " Wechselrichters angeben.")

    def __init__(self):
        self.data = BatAllData()

    def calc_power_for_all_components(self):
        try:
            if len(data.data.bat_data) >= 1:
                self.data.config.configured = True
                # Summe für alle konfigurierten Speicher bilden
                exported = 0
                imported = 0
                power = 0
                soc_sum = 0
                soc_count = 0
                fault_state = 0
                for battery in data.data.bat_data.values():
                    try:
                        power += battery.data.get.power
                    except Exception:
                        log.exception(f"Fehler im Bat-Modul {battery.num}")
                    imported += battery.data.get.imported
                    exported += battery.data.get.exported
                    soc_sum += battery.data.get.soc
                    soc_count += 1
                    fault_state = max(fault_state, battery.data.get.fault_state)
                self.data.get.fault_state = fault_state
                self.data.get.fault_str = NO_ERROR if fault_state == 0 else (
                    "Bitte die Statusmeldungen der Speicher prüfen. "
                    "Es haben nicht alle Module aktuelle Zählerstände geliefert.")
                self.data.get.power = power
                self.data.get.imported = imported
                self.data.get.exported = exported
                try:
                    self.data.get.soc = int(soc_sum / soc_count)
                except ZeroDivisionError:
                    self.data.get.soc = 0
            else:
                self.data.config.configured = False
                # prevent stale values when no inverter modules are configured
                self.data.get.power = 0
                self.data.get.exported = 0
                self.data.get.fault_state = 0
                self.data.get.fault_str = NO_ERROR
                self.data.get.daily_exported = 0
                self.data.get.imported = 0
                self.data.get.daily_imported = 0
                self.data.get.soc = 0
                self.data.get.power_limit_controllable = False
        except Exception:
            log.exception("Fehler im Bat-Modul")

    def _absolute_bat_discharge_power(self) -> float:
        discharge_power = 0
        hybrid_bat_ids = data.data.counter_all_data.get_hybrid_bat_ids()
        non_hybrid_bat_ids = data.data.counter_all_data.get_non_hybrid_bat_ids()
        if len(hybrid_bat_ids) == 0:
            # keine Hybrid-WR mit Speicher
            discharge_power = float("inf")
        else:
            # nur Speicher an Hybrid-WR
            discharge_power = 0
            hybrid_inverter_ids = data.data.counter_all_data.get_hybrid_inverter_ids()
            for inverter_id in hybrid_inverter_ids:
                try:
                    inverter = data.data.pv_data[f"pv{inverter_id}"]
                    inverter_power = max(inverter.data.get.power * -1, 0)
                    discharge_power += max(inverter.data.config.max_ac_out - inverter_power, 0)
                except Exception:
                    log.exception(f"Fehler im Bat-Modul {inverter_id}")
            log.debug(f"Verbleibende Speicher-Leistung durch maximale Ausgangsleistung des Wechselrichters auf "
                      f"{discharge_power}W begrenzt.")
        if len(non_hybrid_bat_ids) > 0:
            # Speicher an Hybrid-WR und AC-Speicher im System
            for bat_id in non_hybrid_bat_ids:
                try:
                    bat = data.data.bat_data[f"bat{bat_id}"]
                    discharge_power += bat.data.get.max_discharge_power
                except Exception:
                    log.exception(f"Fehler im Bat-Modul {bat_id}")
        return discharge_power

    def _set_bat_power_active_control(self, power):
        controllable_bat_components, _ = get_bat_components_by_controllability()
        # maximal mögliche Lade- und Entladeleistung des Systems unter Einbeziehung
        # der erlaubten Lade-/ Entladeleistung und SoC der regelbaren Speicher ermitteln
        max_charge_power_total = 0
        bat_ready_to_charge = 0
        max_discharge_power_total = 0
        bat_ready_to_discharge = 0
        if power is not None:
            for bat_component in controllable_bat_components:
                bat_component_data = data.data.bat_data[f"bat{bat_component.component_config.id}"].data
                if bat_component_data.get.soc < self.data.config.bat_control_max_soc:
                    max_charge_power_total += bat_component_data.get.max_charge_power
                    bat_ready_to_charge += 1
                if bat_component_data.get.soc > self.data.config.bat_control_min_soc:
                    max_discharge_power_total += bat_component_data.get.max_discharge_power
                    bat_ready_to_discharge += 1
            log.debug((f"Aktive Speichersteuerung: {power}W auf "
                       f"{len(controllable_bat_components)} regelbare Speicher verteilen."))
            log.debug((f"Ladung: {bat_ready_to_charge} Speicher unterhalb des maximalen SoC mit "
                       f"{max_charge_power_total}W regelbarer Lade-Leistung"))
            log.debug((f"Entladung: {bat_ready_to_discharge} Speicher oberhalb des minimalen SoC mit "
                       f"{max_discharge_power_total}W regelbarer Entlade-Leistung"))

        # Leistung an einzelne Speicher übergeben
        for bat_component in controllable_bat_components:
            bat_component_data = data.data.bat_data[f"bat{bat_component.component_config.id}"].data
            # Falls keine Leistung übergeben wird greift die Eigenregelung der Speicher
            if power is None:
                power_limit = None
                bat_component_data.get.state_str = "Keine Steuerung"
                log.debug(("Speichersteuerung: Eigenregelung - Speicher "
                          f"(ID: {bat_component.component_config.id}) regelt selbst."))
            elif power == 0:
                power_limit = 0
                bat_component_data.get.state_str = "Entladesperre"
                log.debug((f"Aktive Speichersteuerung: Kein Laden/Entladen - "
                           f"0W für Speicher (ID: {bat_component.component_config.id})."))
            elif power < 0:
                # Eigenregelung aller Speicher, da Entladung nicht möglich
                if max_discharge_power_total == 0:
                    power_limit = None
                    bat_component_data.get.state_str = ("Keine Steuerung - alle Speicher "
                                                        "befinden sich unterhalb minimal SoC")
                    log.debug(("Aktive Speichersteuerung: Entladung - alle Speicher befinden sich unterhalb minimal "
                               f"SoC. Eigenregelung des Speichers (ID: {bat_component.component_config.id})"))
                else:
                    # unterhalb des minimal SoC greift die Eigenregelung
                    # das verhindert Tiefentladung
                    if bat_component_data.get.soc <= self.data.config.bat_control_min_soc:
                        power_limit = None
                        bat_component_data.get.state_str = ("Keine Steuerung - dieser Speicher "
                                                            "befindet sich unterhalb minimal SoC")
                        log.debug(("Aktive Speichersteuerung: Entladung - "
                                   f"Speicher (ID: {bat_component.component_config.id}) "
                                   "befindet sich unterhalb minimal SoC - Eigenregelung des Speichers."))
                    # setze Entladeleistung als Bruchteil der möglichen Entladeleistung
                    else:
                        factor = min(power / max_discharge_power_total, 1)
                        power_limit = int(bat_component_data.get.max_discharge_power * factor)
                        bat_component_data.get.state_str = f"Entladung mit {round(power_limit / 1000, 3)} kW"
                        log.debug(("Aktive Speichersteuerung: Entladung - "
                                   f"Speicher (ID: {bat_component.component_config.id}) "
                                   f"entladen mit {power_limit} ({factor} x "
                                   f"{bat_component_data.get.max_discharge_power}) W"))
            else:
                # oberhalb des max_soc soll Speicher nicht entladen wenn andere Speicher laden
                if bat_component_data.get.soc >= self.data.config.bat_control_max_soc:
                    power_limit = 0
                    bat_component_data.get.state_str = ("Speicher befindet sich oberhalb "
                                                        "des maximalen SoC - Ladung gesperrt")
                    log.debug(("Aktive Speichersteuerung: Ladung - "
                               f"Speicher (ID: {bat_component.component_config.id}) "
                               "befindet sich oberhalb maximal SoC - Speicher sperren."))
                else:
                    factor = min(power / max_charge_power_total, 1)
                    power_limit = int(bat_component_data.get.max_charge_power * factor)
                    bat_component_data.get.state_str = f"Ladung mit {round(power_limit / 1000, 3)} kW "
                    log.debug(("Aktive Speichersteuerung: Ladung - "
                               f"Speicher (ID: {bat_component.component_config.id}) "
                               f"laden mit {power_limit} ({factor} x {bat_component_data.get.max_charge_power}) W"))
            data.data.bat_data[f"bat{bat_component.component_config.id}"].data.set.power_limit = power_limit

    def _set_bat_charge_power_limit(self, charge_power_limit: Optional[int]) -> None:
        controllable_bat_components, _ = get_bat_components_by_charge_power_controllability()
        for bat_component in controllable_bat_components:
            bat_component_data = data.data.bat_data[f"bat{bat_component.component_config.id}"].data
            if charge_power_limit is None:
                bat_component_data.set.charge_power_limit = None
            elif bat_component_data.get.max_charge_power <= 0:
                # Ohne konfigurierte maximale Ladeleistung (Lastmanagement) laesst sich keine
                # sinnvolle Obergrenze berechnen - lieber unbegrenzt lassen und warnen, statt
                # stillschweigend auf 0W zu kappen (saehe wie "funktioniert nicht" aus, ohne
                # erkennbaren Grund).
                bat_component_data.set.charge_power_limit = None
                bat_component.fault_state.warning(
                    "Für die Ladeleistungsbegrenzung muss die maximale Ladeleistung dieses "
                    "Speichers im Lastmanagement konfiguriert werden.")
                bat_component.fault_state.store_error()
            else:
                # nie mehr als die konfigurierte maximale Ladeleistung dieses Speichers zulassen
                bat_component_data.set.charge_power_limit = min(
                    charge_power_limit, bat_component_data.get.max_charge_power)

    def setup_bat(self):
        """ prüft, ob mind ein Speicher vorhanden ist und berechnet die Summen-Topics.
        """
        try:
            if self.data.config.configured is True:
                self.set_power_limit_controllable_state()
                self.set_charge_power_limit_controllable_state()
                if self.data.get.fault_state == 0:
                    self.get_power_limit()
                    self._set_bat_power_active_control(self.data.set.power_limit)
                    self._set_bat_charge_power_limit(self.data.set.charge_power_limit)
                    self.get_charging_power_left_diff()
                    log.info(f"{self.data.set.charging_power_left}W verbleibende Speicher-Leistung")
                else:
                    # Bei Warnung oder Fehlerfall, zB durch Kalibrierung, Speicher-Leistung nicht in der
                    # Regelung berücksichtigen.
                    self.data.set.charging_power_left = 0
            else:
                self.data.set.charging_power_left = 0
                self.data.get.power = 0
        except Exception:
            log.exception("Fehler im Bat-Modul")

    def get_charging_power_left_diff(self):
        """Ermittelt die Differenz zur aktuellen Batterie-Leistung,
        die zum Laden der EV verwendet werden darf.
        """
        try:
            config = data.data.general_data.data.chargemode_config.pv_charging

            self.data.set.regulate_up = False
            if config.bat_mode == BatConsiderationMode.BAT_MODE.value:
                if self.data.get.power < 0:
                    # Wenn der Speicher entladen wird, darf diese Leistung nicht zum Laden der Fahrzeuge genutzt werden.
                    # Wenn der Speicher schneller regelt als die LP, würde sonst der Speicher reduziert werden.
                    charging_power_left = self.data.get.power
                else:
                    charging_power_left = 0
                self.data.set.regulate_up = True if self.data.get.soc < 100 else False
            #  ev wird nach Speicher geladen
            elif config.bat_mode == BatConsiderationMode.EV_MODE.value:
                # Speicher sollte weder ge- noch entladen werden.
                # wenn aktive Speichersteuerung in Höhe PV-Leistung lädt
                # hat Speicher Priorität vor EV-Ladung
                if self.data.config.control_mode == BatControlMode.PV_YIELD_WHILE_CHARGING.value:
                    charging_power_left = 0
                else:
                    charging_power_left = self.data.get.power
            else:
                absolute_bat_discharge_power = self._absolute_bat_discharge_power()
                # Speicher soll geladen werden um min SoC zu erreichen
                if self.data.get.soc < config.min_bat_soc:
                    self.data.set.hysteresis_discharge = False
                    if self.data.get.power < 0:
                        # Wenn der Speicher entladen wird, darf diese Leistung nicht zum Laden der Fahrzeuge
                        # genutzt werden. Wenn der Speicher schneller regelt als die LP, würde sonst der Speicher
                        # reduziert werden.
                        charging_power_left = self.data.get.power
                        self.data.set.regulate_up = True
                    else:
                        # Speicher-Vorrang bis zum Min-Soc
                        if config.bat_power_reserve_active:
                            # Die Differenz zwischen aktueller Batterie-Leistung und Reserveleistung bestimmt,
                            # was fuer EV-Ladung verbleibt (positiv) oder zusaetzlich benoetigt wird (negativ).
                            charging_power_left = self.data.get.power - config.bat_power_reserve
                            if charging_power_left < 0:
                                self.data.set.regulate_up = True
                        else:
                            # Speicher wird geladen
                            charging_power_left = 0
                            self.data.set.regulate_up = True
                # Speicher zwischen min und max SoC
                elif int(self.data.get.soc) >= config.min_bat_soc and int(self.data.get.soc) < config.max_bat_soc:
                    # Speicher soll aktiv weder ge- noch entladen werden.
                    # Mindest-SoC wird gehalten oder der Speicher mit weiterem vorhanden Überschuss geladen.
                    if self.data.set.hysteresis_discharge is False:
                        charging_power_left = self.data.get.power
                    # Speicher darf wegen Hysterese bis min_bat_soc entladen werden.
                    else:
                        if self.data.set.power_limit is None:
                            # set allowed power
                            if (self.data.config.control_mode ==
                                    BatControlMode.PV_YIELD_WHILE_CHARGING.value):
                                base_power = 0
                            else:
                                base_power = self.data.get.power

                            # Aktive Steuerung nicht konfiguriert oder
                            # Preisbasierte Steuerung aktiv + Grenze nicht unterschritten
                            # -> dann erlaubte Speicherentladeleistung addieren
                            power_discharge_allowed = (
                                self.data.config.control_mode == BatControlMode.SELF_REGULATION.value or
                                (self.data.config.control_mode in (
                                    BatControlMode.FORCE_CHARGE_BELOW_PRICE.value,
                                    BatControlMode.BLOCK_DISCHARGE_ABOVE_PRICE.value) and
                                 self.data.set.power_limit is None))
                            if config.bat_power_discharge_active and power_discharge_allowed:
                                # max Entladeleistung auf max Ausgangsleistung des WR begrenzen
                                required_absolut_discharge_power = min(
                                    config.bat_power_discharge, absolute_bat_discharge_power)
                                # Differenz zwischen erlaubter Entladeleistung und aktueller Speicherleistung bestimmen.
                                # Wenn der Speicher lädt, darf die freigegebene Entladeleistung nicht die max
                                # Ausgangsleistung des WR überschreiten.
                                # Wenn der Speicher mit mehr als der erlaubten Entladeleistung entladen wird, muss das
                                # vom Überschuss subtrahiert werden.
                                charging_power_left = min(required_absolut_discharge_power + base_power,
                                                          absolute_bat_discharge_power)
                                log.debug(f"Erlaubte Entlade-Leistung nutzen {charging_power_left}W")
                            else:
                                # Speicher sollte weder ge- noch entladen werden.
                                charging_power_left = base_power
                        else:
                            log.debug("Keine erlaubte Entladeleistung freigeben, da der Speicher mit einer vorgegeben "
                                      "Leistung entladen wird.")
                            charging_power_left = 0
                # Speicher oberhalb max SoC. Darf bis min SoC entladen werden.
                else:
                    self.data.set.hysteresis_discharge = True
                    if self.data.set.power_limit is None:
                        # set allowed power
                        if self.data.config.control_mode == BatControlMode.PV_YIELD_WHILE_CHARGING.value:
                            base_power = 0
                        else:
                            base_power = self.data.get.power

                        # Aktive Steuerung nicht konfiguriert oder
                        # Preisbasierte Steuerung aktiv + Grenze nicht unterschritten
                        # -> dann erlaubte Speicherentladeleistung addieren
                        power_discharge_allowed = (
                            self.data.config.control_mode == BatControlMode.SELF_REGULATION.value or
                            (self.data.config.control_mode in (
                                BatControlMode.FORCE_CHARGE_BELOW_PRICE.value,
                                BatControlMode.BLOCK_DISCHARGE_ABOVE_PRICE.value) and
                             self.data.set.power_limit is None))
                        if config.bat_power_discharge_active and power_discharge_allowed:
                            # max Entladeleistung auf max Ausgangsleistung des WR begrenzen
                            required_absolut_discharge_power = min(
                                config.bat_power_discharge, absolute_bat_discharge_power)
                            # Differenz zwischen erlaubter Entladeleistung und aktueller Speicherleistung bestimmen.
                            # Wenn der Speicher lädt, darf die freigegebene Entladeleistung nicht die max
                            # Ausgangsleistung des WR überschreiten.
                            # Wenn der Speicher mit mehr als der erlaubten Entladeleistung entladen wird, muss das
                            # vom Überschuss subtrahiert werden.
                            charging_power_left = min(required_absolut_discharge_power + base_power,
                                                      absolute_bat_discharge_power)
                            log.debug(f"Erlaubte Entlade-Leistung nutzen {charging_power_left}W")
                        else:
                            # Speicher sollte weder ge- noch entladen werden.
                            charging_power_left = base_power
                    else:
                        log.debug("Keine erlaubte Entladeleistung freigeben, da der Speicher mit einer vorgegeben "
                                  "Leistung entladen wird.")
                        charging_power_left = 0
            # Keine Ladeleistung vom Speicher für Fahrzeuge einplanen, wenn max
            # Ausgangsleistung erreicht ist.
            if self.data.set.regulate_up:
                # 100(50 reichen auch?) W Überschuss übrig lassen, damit der Speicher bis zur max Ladeleistung
                # hochregeln kann.
                log.debug("Damit der Speicher hochregeln kann, muss unabhängig vom eingestellten Regelmodus "
                          "Einspeisung erzeugt werden.")
                charging_power_left -= 100
            self.data.set.charging_power_left = charging_power_left
        except Exception:
            log.exception("Fehler im Bat-Modul")

    def power_for_bat_charging(self):
        """ gibt die Leistung zurück, die zum Laden verwendet werden kann.

        Return
        ------
        int: Leistung, die zum Laden verwendet werden darf.
        """
        try:
            if self.data.config.configured:
                return self.data.set.charging_power_left
            else:
                return 0
        except Exception:
            log.exception("Fehler im Bat-Modul")
            return 0

    def set_power_limit_controllable_state(self):
        bat_components_controllable, bat_components_not_controllable = get_bat_components_by_controllability()
        if len(bat_components_controllable) > 0:
            self.data.get.power_limit_controllable = True
        else:
            self.data.get.power_limit_controllable = False

        for bat in bat_components_controllable:
            data.data.bat_data[f"bat{bat.component_config.id}"].data.get.power_limit_controllable = True
        for bat in bat_components_not_controllable:
            data.data.bat_data[f"bat{bat.component_config.id}"].data.get.power_limit_controllable = False

    def set_charge_power_limit_controllable_state(self):
        bat_components_controllable, bat_components_not_controllable = (
            get_bat_components_by_charge_power_controllability())
        self.data.get.charge_power_limit_controllable = len(bat_components_controllable) > 0

        for bat in bat_components_controllable:
            data.data.bat_data[f"bat{bat.component_config.id}"].data.get.charge_power_limit_controllable = True
        for bat in bat_components_not_controllable:
            data.data.bat_data[f"bat{bat.component_config.id}"].data.get.charge_power_limit_controllable = False

    def _vehicle_charging_power_limit(self) -> Optional[float]:
        chargepoint_by_chargemodes = get_chargepoints_with_required_current_by_chargemode(
            CONSIDERED_CHARGE_MODES_CHARGING)
        keep_pv_yield = self.data.config.control_mode == BatControlMode.PV_YIELD_WHILE_CHARGING.value
        # Fahrzeuge laden
        vehicle_charging = (len(chargepoint_by_chargemodes) > 0 and
                            data.data.cp_all_data.data.get.power > 100)
        # Speicher entlädt oder Speicher lädt bei gewollter PV-Ladung
        bat_power_valid = (self.data.get.power <= 0 or
                           (self.data.get.power > 0 and keep_pv_yield))
        # EVU Bezug vorhanden oder gewollte PV-Ladung aktiv
        evu_power_valid = (data.data.counter_all_data.get_evu_counter().data.get.power >= -100 or
                           keep_pv_yield)

        if (
            vehicle_charging and
            evu_power_valid and
            bat_power_valid
        ):
            log.debug("Speicher-Entladung beschränken da Fahrzeuge laden.")
            return self._use_limit_power()

        # Debug Informationen
        control_range_low = data.data.general_data.data.chargemode_config.pv_charging.control_range[0]
        control_range_high = data.data.general_data.data.chargemode_config.pv_charging.control_range[1]
        control_range_center = control_range_high - (control_range_high - control_range_low) / 2
        if len(chargepoint_by_chargemodes) == 0:
            log.debug("Speicher-Leistung nicht begrenzen, da keine Ladepunkte in einem aktiven Lademodus sind.")
        elif data.data.cp_all_data.data.get.power <= 100:
            log.debug("Speicher-Leistung nicht begrenzen, da kein Ladepunkt lädt.")
        elif self.data.get.power > 0:
            log.debug("Speicher-Leistung nicht begrenzen, da kein Speicher entladen wird.")
        elif data.data.counter_all_data.get_evu_counter().data.get.power < control_range_center + 80:
            # Wenn der Regelbereich zB auf Bezug steht, darf auch die Leistung des Regelbereichs entladen
            # werden.
            log.debug("Speicher-Leistung nicht begrenzen, da EVU-Überschuss vorhanden ist.")
        else:
            log.debug("Speicher-Leistung nicht begrenzen.")
        return None

    def _force_charge_below_price_power(self, controllable_bat_components) -> Optional[float]:
        if not data.data.optional_data.data.electricity_pricing.configured:
            return None
        if data.data.optional_data.ep_is_charging_allowed_price_threshold(self.data.config.charge_limit):
            log.debug(f"Aktive Speichersteuerung: Ladung erzwingen. Preislimit: {self.data.config.charge_limit}")
            # manual_power gilt nur fuer MANUAL - hier immer mit voller Leistung laden.
            return self._force_charge_power(controllable_bat_components)
        return None

    def _block_discharge_above_price_power(self) -> Optional[float]:
        if not data.data.optional_data.data.electricity_pricing.configured:
            return None
        if data.data.optional_data.ep_is_charging_allowed_price_threshold(self.data.config.price_limit):
            # Preis liegt auf/unter der Grenze -> keine Entladesperre nötig
            return None
        log.debug(f"Aktive Speichersteuerung: Entladesperre. Preislimit: {self.data.config.price_limit}")
        return self._use_limit_power()

    def _manual_power(self, controllable_bat_components) -> Optional[float]:
        if self.data.config.manual_control == ManualControl.CHARGE.value:
            log.debug("Aktive Speichersteuerung: Manuelle Vorgabe - Speicher laden.")
            return self._force_charge_power(controllable_bat_components, self.data.config.manual_power)
        # STOP (Standard) sowie jeder unbekannte Wert - Entladen ist in DE nicht erlaubt und
        # daher in ManualControl kein wählbarer Wert.
        log.debug("Aktive Speichersteuerung: Manuelle Vorgabe - Stop.")
        return self._use_limit_power()

    def _force_charge_power(self, controllable_bat_components, manual_power: Optional[int] = None) -> float:
        # maximal konfigurierte Ladeleistung des Speichers ermitteln (Obergrenze für eine
        # manuelle Vorgabe unterhalb des Maximums). manual_power wird nur von MANUAL uebergeben -
        # FORCE_CHARGE_BELOW_PRICE laedt immer mit voller Leistung, unabhaengig von einem evtl.
        # anderswo konfigurierten manual_power-Wert.
        max_charge_power_total = 0
        for bat_component in controllable_bat_components:
            bat_component_data = data.data.bat_data[f"bat{bat_component.component_config.id}"].data
            max_charge_power_total += bat_component_data.get.max_charge_power
        if manual_power:
            if manual_power > max_charge_power_total:
                # Backend-seitige Absicherung (defense in depth) fuer den Fall, dass ein zu hoher
                # Wert trotz UI-seitiger Begrenzung eingetragen wurde (z.B. direkt per MQTT/API,
                # oder bevor die UI-seitige Obergrenze umgesetzt ist) - wird begrenzt, nicht
                # ignoriert, aber sichtbar gemeldet statt stillschweigend gekappt.
                for bat_component in controllable_bat_components:
                    bat_component.fault_state.warning(
                        f"Die manuell eingestellte Ladeleistung ({manual_power}W) übersteigt die "
                        f"maximale Ladeleistung der Speicher ({max_charge_power_total}W) und wird "
                        "entsprechend begrenzt.")
                    bat_component.fault_state.store_error()
            return min(manual_power, max_charge_power_total)
        return max_charge_power_total

    def get_power_limit(self):
        controllable_bat_components, _ = get_bat_components_by_controllability()
        control_mode = self.data.config.control_mode
        # Falls kein steuerbarer Speicher installiert oder Eigenregelung gewählt ist
        if self.data.get.power_limit_controllable is False or control_mode == BatControlMode.SELF_REGULATION.value:
            power_limit = None
            if self.data.get.power_limit_controllable is False:
                log.debug("Speicher-Leistung nicht begrenzen, da keine regelbaren Speicher vorhanden sind.")
            elif control_mode == BatControlMode.SELF_REGULATION.value:
                log.debug("Speicher-Leistung nicht begrenzen, da Eigenregelung gewählt ist.")
        elif control_mode in (BatControlMode.HOME_CONSUMPTION_WHILE_CHARGING.value,
                              BatControlMode.PV_YIELD_WHILE_CHARGING.value):
            log.debug("Aktive Speichersteuerung: Wenn Fahrzeuge laden.")
            power_limit = self._vehicle_charging_power_limit()
        elif control_mode == BatControlMode.FORCE_CHARGE_BELOW_PRICE.value:
            log.debug("Aktive Speichersteuerung: Laden erzwingen bei niedrigem Strompreis.")
            power_limit = self._force_charge_below_price_power(controllable_bat_components)
        elif control_mode == BatControlMode.BLOCK_DISCHARGE_ABOVE_PRICE.value:
            log.debug("Aktive Speichersteuerung: Entladesperre bei hohem Strompreis.")
            power_limit = self._block_discharge_above_price_power()
        elif control_mode == BatControlMode.MANUAL.value:
            log.debug("Aktive Speichersteuerung: Manuelle Vorgabe.")
            power_limit = self._manual_power(controllable_bat_components)
        elif control_mode == BatControlMode.BLOCK_DISCHARGE.value:
            log.debug("Aktive Speichersteuerung: Entladesperre.")
            power_limit = self._use_limit_power()
        else:
            # LIMIT_CHARGE_POWER und PEAK_SHAVING (noch nicht implementiert) greifen nicht in
            # den bidirektionalen Pfad ein.
            power_limit = None

        # Unabhängig von power_limit: LIMIT_CHARGE_POWER nutzt set_charge_power_limit statt
        # set_power_limit und lässt den Speicher ansonsten in Eigenregelung (siehe AbstractBat).
        if control_mode == BatControlMode.LIMIT_CHARGE_POWER.value:
            self.data.set.charge_power_limit = self.data.config.charge_power_limit
        else:
            self.data.set.charge_power_limit = None

        self.data.set.power_limit = power_limit
        if power_limit is None:
            log.debug("Speicher-Leistung nicht begrenzen")

        if (control_mode == BatControlMode.SELF_REGULATION.value
                and self.data.set.current_state == CurrentState.STARTUP.value):
            self.data.set.set_limit = False
        elif self.data.set.current_state == CurrentState.IDLE.value and power_limit is None:
            self.data.set.set_limit = False
        else:
            self.data.set.set_limit = True

        if power_limit is None:
            self.data.set.current_state = CurrentState.IDLE.value
        else:
            self.data.set.current_state = CurrentState.ACTIVE.value

    def _use_limit_power(self) -> float:
        """Leistungsvorgabe für eine Entlade-Begrenzung, abhängig vom control_mode.

        HOME_CONSUMPTION_WHILE_CHARGING und PV_YIELD_WHILE_CHARGING
        werden nur erreicht, nachdem _vehicle_charging_power_limit() Fahrzeugladung als
        Bedingung bereits geprüft hat. Alle übrigen Fälle (BLOCK_DISCHARGE,
        BLOCK_DISCHARGE_ABOVE_PRICE, MANUAL+Stop) sperren die Entladung vollständig.
        """
        control_mode = self.data.config.control_mode
        if control_mode == BatControlMode.HOME_CONSUMPTION_WHILE_CHARGING.value:
            power_limit = data.data.counter_all_data.data.set.home_consumption * -1
            log.debug(f"Speicher-Leistung begrenzen auf {power_limit/1000}kW")
        elif control_mode == BatControlMode.PV_YIELD_WHILE_CHARGING.value:
            # PV-Überschuss abzüglich Hausverbrauch als Ladeleistung des Speichers nutzen.
            # Bei geringem Überschuss wird Hausverbrauch durch Speicher ausgeglichen
            pv_power = min(data.data.pv_all_data.data.get.power, 0)
            power_limit = (pv_power + data.data.counter_all_data.data.set.home_consumption) * -1
            if power_limit > 0:
                log.debug(f"Speicher in Höhe des verbliebenen PV-Überschusses laden: {power_limit/1000}kW")
            else:
                log.debug(f"Speicher Entladen um Hausverbrauch zu decken: {power_limit/1000}kW")
        else:
            power_limit = 0
            log.debug("Speicher-Leistung begrenzen auf 0kW")
        return power_limit

    def time_charging_min_bat_soc_allowed(self) -> bool:
        if self.data.config.configured:
            control_mode = self.data.config.control_mode
            if control_mode == BatControlMode.SELF_REGULATION.value:
                return True
            if control_mode == BatControlMode.FORCE_CHARGE_BELOW_PRICE.value:
                return data.data.optional_data.ep_is_charging_allowed_price_threshold(self.data.config.charge_limit)
            if control_mode == BatControlMode.BLOCK_DISCHARGE_ABOVE_PRICE.value:
                return data.data.optional_data.ep_is_charging_allowed_price_threshold(self.data.config.price_limit)
            # jeder andere aktive Modus (Fahrzeugladung, Entladesperre, Manuell, Ladeleistung
            # begrenzen, PeakShaving) kann den Speicher jederzeit beanspruchen
            return False
        return True


def _get_bat_components_by_capability(capability_check) -> Tuple[List, List]:
    components_controllable, components_not_controllable = [], []
    for value in data.data.system_data.values():
        if isinstance(value, AbstractDevice):
            for comp_value in value.components.values():
                if "bat" in comp_value.component_config.type:
                    try:
                        with SingleComponentUpdateContext(comp_value.fault_state, update_always=False, reraise=True):
                            if capability_check(comp_value):
                                components_controllable.append(comp_value)
                            else:
                                components_not_controllable.append(comp_value)
                    except Exception:
                        components_not_controllable.append(comp_value)
    return components_controllable, components_not_controllable


def get_bat_components_by_controllability() -> Tuple[List, List]:
    return _get_bat_components_by_capability(lambda comp: comp.power_limit_controllable())


def get_bat_components_by_charge_power_controllability() -> Tuple[List, List]:
    return _get_bat_components_by_capability(lambda comp: comp.charge_power_limit_controllable())
