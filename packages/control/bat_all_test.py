from dataclasses import dataclass, field
from typing import List, Optional
from unittest.mock import MagicMock, Mock
import pytest
from packages.conftest import hierarchy_standard
from control import bat_all
from control.bat import Bat

from control.bat_all import BatAll, BatConsiderationMode, BatControlMode
from control import data
from control.chargepoint.chargepoint import Chargepoint
from control.chargepoint.chargepoint_all import AllChargepointData, AllChargepoints, AllGet
from control.general import General, PvCharging
from control.pv import Config, Get, Pv, PvData
from modules.common.abstract_device import AbstractDevice
from modules.common.fault_state import ComponentInfo, FaultState
from modules.devices.generic.mqtt.bat import MqttBat
from modules.devices.generic.mqtt.config import MqttBatSetup


@pytest.fixture(autouse=True)
def data_fixture() -> None:
    data.data_init(Mock())
    data.data.general_data = General()
    data.data.cp_all_data = Mock(spec=AllChargepoints, data=Mock(
        spec=AllChargepointData, get=Mock(spec=AllGet, power=0)))
    data.data.pv_data["pv1"] = Mock(spec=Pv, data=Mock(spec=PvData, get=Mock(spec=Get, power=-6400),
                                                       config=Mock(spec=Config, max_ac_out=7200)))


@pytest.mark.parametrize(
    "bat_power, pv_power, expected_power",
    [
        pytest.param(-1000, 0, 4000, id="Leistung verfügbar"),
        pytest.param(-4900, -100, 0, id="max Leistung des WR um 100W überschritten, Speicher entlädt"),
        pytest.param(1000, -4500, 500, id="Speicher lädt, soll entladen"),
    ])
def test_get_charging_power_left_diff_hybrid(bat_power: int,
                                             pv_power: int,
                                             expected_power: int,
                                             monkeypatch: pytest.MonkeyPatch):
    # setup
    data.data.pv_data = {"pv2": Pv(2)}
    data.data.pv_data["pv2"].data.get.power = pv_power
    data.data.pv_data["pv2"].data.config.max_ac_out = 5000
    data.data.bat_data["bat1"] = Bat(1)
    data.data.bat_data["bat1"].data.get.power = bat_power
    data.data.bat_data["bat1"].data.get.soc = 71
    data.data.general_data.data.chargemode_config.pv_charging.bat_mode = BatConsiderationMode.MIN_SOC_BAT.value
    data.data.general_data.data.chargemode_config.pv_charging.bat_power_discharge = 5000
    data.data.general_data.data.chargemode_config.pv_charging.bat_power_discharge_active = True
    monkeypatch.setattr(data.data.counter_all_data, "get_hybrid_bat_ids", Mock(return_value=[1]))
    monkeypatch.setattr(data.data.counter_all_data, "get_non_hybrid_bat_ids", Mock(return_value=[]))
    monkeypatch.setattr(data.data.counter_all_data, "get_hybrid_inverter_ids", Mock(return_value=[2]))

    b_all = BatAll()
    b_all.data.get.power = bat_power
    b_all.data.get.soc = 71

    # execution
    b_all.get_charging_power_left_diff()

    # evaluation
    assert b_all.data.set.charging_power_left == expected_power


@pytest.mark.parametrize(
    "hybrid_bat_ids, non_hybrid_bat_ids, hybrid_inverter_ids, expected_power",
    [
        pytest.param([], [1], [], float("inf"), id="no hybrid"),
        pytest.param([1], [], [2], 4900, id="hybrid,"),
        pytest.param([1], [3], [2], 10900, id="hybrid an non hybrid bat"),
    ])
def test__absolute_bat_discharge_power(hybrid_bat_ids: List[int],
                                       non_hybrid_bat_ids: List[int],
                                       hybrid_inverter_ids: List[int],
                                       expected_power: float,
                                       monkeypatch: pytest.MonkeyPatch):
    # setup
    data.data.pv_data = {"pv2": Pv(2)}
    data.data.pv_data["pv2"].data.get.power = -100
    data.data.pv_data["pv2"].data.config.max_ac_out = 5000
    data.data.bat_data["bat1"] = Bat(1)
    data.data.bat_data["bat1"].data.get.power = -4900
    data.data.bat_data["bat3"] = Bat(3)
    data.data.bat_data["bat3"].data.get.max_discharge_power = 6000
    monkeypatch.setattr(data.data.counter_all_data, "get_hybrid_bat_ids", Mock(return_value=hybrid_bat_ids))
    monkeypatch.setattr(data.data.counter_all_data, "get_non_hybrid_bat_ids", Mock(return_value=non_hybrid_bat_ids))
    monkeypatch.setattr(data.data.counter_all_data, "get_hybrid_inverter_ids", Mock(return_value=hybrid_inverter_ids))

    b = BatAll()
    b.data.get.power = -4900

    # execution
    power = b._absolute_bat_discharge_power()  # pyright: ignore[reportPrivateUsage]

    # evaluation
    assert power == expected_power


@dataclass
class Params:
    name: str
    config: PvCharging
    power: float
    soc: float
    expected_charging_power_left: float
    expected_regulate_up: bool
    power_limit: Optional[float] = None
    hysteresis_discharge: Optional[bool] = False


cases = [
    Params("Speicher, Speicher lädt", PvCharging(bat_mode="bat_mode"), 500, 90, -100, True),
    Params("Speicher, Speicher entlädt", PvCharging(bat_mode="bat_mode"), -500, 90, -600, True),
    Params("Speicher, Speicher ist voll", PvCharging(bat_mode="bat_mode"), 0, 100, 0, False),
    Params("EV, Speicher lädt", PvCharging(bat_mode="ev_mode"), 500, 90, 500, False),
    Params("EV, Speicher entlädt", PvCharging(bat_mode="ev_mode"), -500, 90, -500, False),
    Params("EV, Speicher ist voll", PvCharging(bat_mode="ev_mode"), 0, 100, 0, False),
    Params("Mindest-SoC, SoC nicht erreicht, Speicher entlädt",
           PvCharging(bat_mode="min_soc_bat_mode"), -500, 40, -600, True),
    Params("Mindest-SoC, SoC nicht erreicht, Speicher lädt",
           PvCharging(bat_mode="min_soc_bat_mode"), 500, 40, -100, True),
    Params("Mindest-SoC, SoC nicht erreicht, Speicher-Reserve, Speicher entlädt",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_reserve=2000, bat_power_reserve_active=True),
           -500, 40, -600, True),
    Params("Mindest-SoC, SoC nicht erreicht, Speicher-Reserve nicht ausgenutzt, Speicher lädt",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_reserve=2000, bat_power_reserve_active=True),
           1600, 40, -500, True),
    Params("Mindest-SoC, SoC nicht erreicht, Speicher-Reserve ausgenutzt, Speicher lädt",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_reserve=2000, bat_power_reserve_active=True),
           2200, 40, 200, False),
    Params("Mindest-SoC, SoC erreicht, Speicher entlädt", PvCharging(bat_mode="min_soc_bat_mode"), -500, 90, -500,
           False),
    Params("Mindest-SoC, SoC erreicht, Speicher lädt", PvCharging(bat_mode="min_soc_bat_mode"), 500, 90, 500, False),
    Params("Mindest-SoC, SoC erreicht, Speicher ist voll", PvCharging(bat_mode="min_soc_bat_mode"), 0, 100, 0, False),
    Params("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher entlädt, Entladeleistung nicht erreicht",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           -400, 90, 100, False),
    Params("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher entlädt, mehr als Entladeleistung",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           -600, 90, -100, False),
    Params("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher entlädt, Entladeleistung erreicht",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           -500, 90, 0, False),
    Params("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher lädt mit mehr als Entladeleistung",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           650, 90, 1150, False),
    Params("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher lädt mit weniger als Entladeleistung",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           400, 90, 900, False),
    Params("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher voll",
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_reserve=500, bat_power_reserve_active=True,
                      min_bat_soc=100), 0, 100, 0, False),
    Params(("Mindest-SoC, SoC erreicht, Entladung in Auto, Speicher lädt mit weniger als Entladeleistung, "
           "Speicher-Sperre aktiv"),
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           400, 90, 0, False, 600),
    Params(("Mindest-SoC, Hysterese, EV-Vorrang, keine Speichernutzung"),
           PvCharging(bat_mode="min_soc_bat_mode"), 400, 60, 400, False, hysteresis_discharge=False),
    Params(("Mindest-SoC, Hysterese, Speicherentladung, Speichernutzung erlaubt"),
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           400, 60, 900, False, hysteresis_discharge=True),
    Params(("Mindest-SoC, Hysterese, Speicherentladung, Speichernutzung erlaubt, Speicher-Sperre aktiv"),
           PvCharging(bat_mode="min_soc_bat_mode", bat_power_discharge=500, bat_power_discharge_active=True),
           400, 60, 0, False, 600, hysteresis_discharge=True),
]


@pytest.mark.parametrize("params", cases, ids=[c.name for c in cases])
def test_get_charging_power_left(params: Params, caplog, data_, monkeypatch):
    # setup
    b_all = BatAll()
    b_all.data.get.power = params.power
    b_all.data.get.soc = params.soc
    b_all.data.set.power_limit = params.power_limit
    b_all.data.set.hysteresis_discharge = params.hysteresis_discharge
    b = Bat(0)
    b.data.get.power = params.power
    data.data.bat_data["bat0"] = b
    data.data.general_data.data.chargemode_config.pv_charging = params.config
    mock_absolute_bat_discharge_power = MagicMock(return_value=10000)
    monkeypatch.setattr(BatAll, "_absolute_bat_discharge_power", mock_absolute_bat_discharge_power)

    # execution
    b_all.get_charging_power_left_diff()

    # evaluation
    assert b_all.data.set.charging_power_left == params.expected_charging_power_left
    assert b_all.data.set.regulate_up == params.expected_regulate_up


def test_get_charging_power_left_uses_limited_bat_discharge_in_hysteresis(
        data_: data.Data, monkeypatch: pytest.MonkeyPatch):
    # setup: min/max-SoC-Bereich mit aktiver Hysterese und erlaubter Entladeleistung
    b_all = BatAll()
    b_all.data.get.power = -2500
    b_all.data.get.soc = 60
    b_all.data.set.hysteresis_discharge = True
    b_all.data.set.power_limit = None
    data.data.general_data.data.chargemode_config.pv_charging = PvCharging(
        bat_mode="min_soc_bat_mode",
        min_bat_soc=40,
        max_bat_soc=80,
        bat_power_discharge=8000,
        bat_power_discharge_active=True,
    )

    # Hybrid-Setup fuer reale Berechnung in _limit_bat_power_discharge
    data.data.pv_data = {"pv2": Pv(2)}
    data.data.pv_data["pv2"].data.get.power = -7500
    data.data.pv_data["pv2"].data.config.max_ac_out = 10000
    data.data.bat_data["bat1"] = Bat(1)
    data.data.bat_data["bat1"].data.get.power = -2500
    monkeypatch.setattr(data.data.counter_all_data, "get_hybrid_bat_ids", Mock(return_value=[1]))
    monkeypatch.setattr(data.data.counter_all_data, "get_non_hybrid_bat_ids", Mock(return_value=[]))
    monkeypatch.setattr(data.data.counter_all_data, "get_hybrid_inverter_ids", Mock(return_value=[2]))

    # execution
    b_all.get_charging_power_left_diff()

    # evaluation: reale Begrenzung (300W) + base_power (400W)
    assert b_all.data.set.charging_power_left == 0
    assert b_all.data.set.regulate_up is False


def default_chargepoint_factory() -> List[Chargepoint]:
    cp = Chargepoint(3, None)
    cp.data.get.power = 1400
    return [cp]


@dataclass
class BatControlParams:
    name: str
    expected_power_limit_bat: Optional[float]
    control_mode: str = BatControlMode.BLOCK_DISCHARGE_WHILE_VEHICLE_CHARGING.value
    manual_power: Optional[int] = None
    cps: List[Chargepoint] = field(default_factory=default_chargepoint_factory)
    power_limit_controllable: bool = True
    bat_power: float = -10
    bat_soc: float = 50.0
    evu_power: float = 200
    pv_power: float = -654
    bat_control_permitted: bool = True
    bat_control_activated: bool = True
    max_charge_power: float = 5000
    max_discharge_power: float = -5000
    bat_control_min_soc: float = 10.0
    bat_control_max_soc: float = 90.0
    price_limit_activated: bool = False
    price_charge_activated: bool = False
    price_limit: float = 0.30
    charge_limit: float = 0.30


cases = [
    BatControlParams("Speicher nicht regelbar", None, power_limit_controllable=False),
    BatControlParams("Speichersteuerung deaktiviert", None, bat_control_activated=False),
    # Manuelle Steuerung (dauerhaft, unabhaengig von Fahrzeugladung)
    BatControlParams("Manuelle Steuerung, Speichersteuerung deaktiviert", None,
                     control_mode=BatControlMode.BLOCK_DISCHARGE.value,
                     bat_control_activated=False),
    BatControlParams("Manuelle Steuerung, Entladung sperren", 0,
                     control_mode=BatControlMode.BLOCK_DISCHARGE.value),
    BatControlParams("Manuelle Steuerung, Aktive Ladung, ohne Vorgabe -> maximale Leistung", 5000,
                     control_mode=BatControlMode.FORCE_CHARGE.value),
    BatControlParams("Manuelle Steuerung, Aktive Ladung, mit Leistungsvorgabe", 3000,
                     control_mode=BatControlMode.FORCE_CHARGE.value, manual_power=3000),
    BatControlParams("Manuelle Steuerung, Aktive Ladung, Vorgabe über Maximum gekappt", 5000,
                     control_mode=BatControlMode.FORCE_CHARGE.value, manual_power=8000),
    # Wenn Fahrzeuge Laden
    BatControlParams("Fahrzeuge laden, Begrenzung immer, keine LP im Sofortladen", None, cps=[]),
    BatControlParams("Fahrzeuge laden, Begrenzung immer, Speicher lädt", None, bat_power=100),
    BatControlParams("Fahrzeuge laden, Begrenzung immer,Einspeisung", None, evu_power=-110),
    BatControlParams("Fahrzeuge laden, Begrenzung immer", 0),
    BatControlParams("Fahrzeuge laden, Begrenzung Hausverbrauch", -456,
                     control_mode=BatControlMode.HOME_CONSUMPTION_ONLY_WHILE_VEHICLE_CHARGING.value),
    BatControlParams("Fahrzeuge laden, Ladung PV Überschuss", 198,
                     control_mode=BatControlMode.KEEP_PV_YIELD_WHILE_VEHICLE_CHARGING.value),
    BatControlParams("Fahrzeuge laden, Ladung PV Überschuss, Eigenverbrauch PV-Anlage", -456,
                     control_mode=BatControlMode.KEEP_PV_YIELD_WHILE_VEHICLE_CHARGING.value,
                     pv_power=100),
]


@pytest.mark.parametrize("params", cases, ids=[c.name for c in cases])
def test_active_bat_control(params: BatControlParams, data_, monkeypatch):
    b_all = BatAll()
    b_all.data.config.bat_control_activated = params.bat_control_activated
    b_all.data.config.control_mode = params.control_mode
    b_all.data.config.manual_power = params.manual_power
    b_all.data.get.power_limit_controllable = params.power_limit_controllable
    b_all.data.config.bat_control_min_soc = params.bat_control_min_soc
    b_all.data.config.bat_control_max_soc = params.bat_control_max_soc
    b_all.data.config.price_limit_activated = params.price_limit_activated
    b_all.data.config.price_charge_activated = params.price_charge_activated
    b_all.data.config.price_limit = params.price_limit
    b_all.data.config.charge_limit = params.charge_limit

    b_all.data.get.power = params.bat_power
    # b_all.data.get.soc = 50.0
    data.data.counter_all_data = hierarchy_standard()
    data.data.counter_all_data.data.set.home_consumption = 456
    data.data.pv_all_data.data.get.power = params.pv_power
    data.data.cp_all_data.data.get.power = 1400
    data.data.counter_data["counter0"].data.get.power = params.evu_power
    data.data.bat_all_data = b_all

    get_chargepoints_with_required_current_by_chargemode_mock = Mock(return_value=params.cps)
    monkeypatch.setattr(bat_all, "get_chargepoints_with_required_current_by_chargemode",
                        get_chargepoints_with_required_current_by_chargemode_mock)
    get_evu_counter_mock = Mock(return_value=data.data.counter_data["counter0"])
    monkeypatch.setattr(data.data.counter_all_data, "get_evu_counter", get_evu_counter_mock)
    get_bat_components_by_controllability_mock = Mock(return_value=([MqttBat(MqttBatSetup(id=2), device_id=0)], []))
    data.data.bat_data["bat2"].data.get.soc = params.bat_soc
    data.data.bat_data["bat2"].data.get.max_charge_power = params.max_charge_power
    data.data.bat_data["bat2"].data.get.max_discharge_power = params.max_discharge_power
    monkeypatch.setattr(bat_all, "get_bat_components_by_controllability",
                        get_bat_components_by_controllability_mock)

    data.data.bat_all_data.get_power_limit()
    data.data.bat_all_data._set_bat_power_active_control(data.data.bat_all_data.data.set.power_limit)

    assert data.data.bat_data["bat2"].data.set.power_limit == params.expected_power_limit_bat


cases = [
    # Nach Preisgrenze - der Regelmodus oberhalb der Preisgrenze ist fest auf Entladesperre
    # gesetzt (kein waehlbarer Regelmodus mehr, siehe control_mode-Vereinfachung); ein bisher
    # ueber PRICE_LIMIT + MODE_CHARGE_PV_PRODUCTION erreichbares "PV-Ertrag speichern nur bei
    # guenstigem Preis" gibt es dadurch nicht mehr - siehe "Grenze unterschritten" unten.
    BatControlParams("Preisgrenze, Grenze deaktiviert, Eigenregelung", None,
                     control_mode=BatControlMode.PRICE_BASED.value,
                     price_limit_activated=False,
                     price_limit=0.40),
    BatControlParams("Preisgrenze, Entladung sperren, Grenze unterschritten", 0,
                     control_mode=BatControlMode.PRICE_BASED.value,
                     price_limit_activated=True,
                     price_limit=0.30),
    BatControlParams("Preisgrenze, Entladung sperren, Grenze greift nicht", None,
                     control_mode=BatControlMode.PRICE_BASED.value,
                     price_limit_activated=True,
                     price_limit=0.10),
    # Aktive Ladung
    BatControlParams("Preisgrenze, Grenze deaktiviert, Eigenregelung", None,
                     control_mode=BatControlMode.PRICE_BASED.value,
                     price_charge_activated=False,
                     charge_limit=0.40),
    BatControlParams("Preisgrenze, Grenze unterschritten, Ladung", 5000,
                     control_mode=BatControlMode.PRICE_BASED.value,
                     price_charge_activated=True,
                     charge_limit=0.30),
    BatControlParams("Preisgrenze, Grenze greift nicht, Eigenregelung", None,
                     control_mode=BatControlMode.PRICE_BASED.value,
                     price_charge_activated=True,
                     charge_limit=0.10),
]


@pytest.mark.parametrize("params", cases, ids=[c.name for c in cases])
def test_control_price_limit(params: BatControlParams, data_, monkeypatch):
    monkeypatch.setattr(data.data.optional_data, "ep_get_current_price", Mock(return_value=0.2))
    b_all = BatAll()
    b_all.data.config.bat_control_activated = params.bat_control_activated
    b_all.data.config.control_mode = params.control_mode
    b_all.data.config.manual_power = params.manual_power
    b_all.data.get.power_limit_controllable = params.power_limit_controllable
    b_all.data.config.bat_control_min_soc = params.bat_control_min_soc
    b_all.data.config.bat_control_max_soc = params.bat_control_max_soc
    b_all.data.config.price_limit_activated = params.price_limit_activated
    b_all.data.config.price_charge_activated = params.price_charge_activated
    b_all.data.config.price_limit = params.price_limit
    b_all.data.config.charge_limit = params.charge_limit

    b_all.data.get.power = params.bat_power
    # b_all.data.get.soc = 50.0
    data.data.optional_data.data.electricity_pricing.configured = True
    data.data.counter_all_data = hierarchy_standard()
    data.data.counter_all_data.data.set.home_consumption = 456
    data.data.pv_all_data.data.get.power = -654
    data.data.cp_all_data.data.get.power = 1400
    data.data.counter_data["counter0"].data.get.power = params.evu_power
    data.data.bat_all_data = b_all

    get_chargepoints_with_required_current_by_chargemode_mock = Mock(return_value=params.cps)
    monkeypatch.setattr(bat_all, "get_chargepoints_with_required_current_by_chargemode",
                        get_chargepoints_with_required_current_by_chargemode_mock)
    get_evu_counter_mock = Mock(return_value=data.data.counter_data["counter0"])
    monkeypatch.setattr(data.data.counter_all_data, "get_evu_counter", get_evu_counter_mock)
    get_bat_components_by_controllability_mock = Mock(return_value=([MqttBat(MqttBatSetup(id=2), device_id=0)], []))
    data.data.bat_data["bat2"].data.get.soc = params.bat_soc
    data.data.bat_data["bat2"].data.get.max_charge_power = params.max_charge_power
    data.data.bat_data["bat2"].data.get.max_discharge_power = params.max_discharge_power
    monkeypatch.setattr(bat_all, "get_bat_components_by_controllability",
                        get_bat_components_by_controllability_mock)

    data.data.bat_all_data.get_power_limit()
    data.data.bat_all_data._set_bat_power_active_control(data.data.bat_all_data.data.set.power_limit)

    assert data.data.bat_data["bat2"].data.set.power_limit == params.expected_power_limit_bat


@pytest.mark.parametrize(
    "control_activated, control_mode, expected_result",
    [
        pytest.param(False,
                     BatControlMode.BLOCK_DISCHARGE_WHILE_VEHICLE_CHARGING.value, True,
                     id="Speichersteuerung nicht aktiviert -> laden erlaubt"),
        pytest.param(True,
                     BatControlMode.BLOCK_DISCHARGE.value, False,
                     id="Manuell, Entladesperre -> nicht laden"),
        pytest.param(True,
                     BatControlMode.FORCE_CHARGE.value, False,
                     id="Manuell, Aktive Ladung -> nicht laden"),
        pytest.param(True,
                     BatControlMode.BLOCK_DISCHARGE_WHILE_VEHICLE_CHARGING.value, False,
                     id="Fahrzeuge laden, volle Entladesperre -> nicht laden"),
        pytest.param(True,
                     BatControlMode.HOME_CONSUMPTION_ONLY_WHILE_VEHICLE_CHARGING.value, False,
                     id="Fahrzeuge laden, Entladung in Fahrzeuge sperren -> nicht laden"),
        pytest.param(True,
                     BatControlMode.KEEP_PV_YIELD_WHILE_VEHICLE_CHARGING.value, False,
                     id="Fahrzeuge laden, PV-Ertrag speichern -> nicht laden"),
    ]
)
def test_time_charging_min_bat_soc_allowed(control_activated: bool,
                                           control_mode: str,
                                           expected_result: bool):
    # setup
    b = BatAll()
    b.data.config.configured = True
    b.data.config.control_mode = control_mode
    b.data.config.bat_control_activated = control_activated

    # execution
    result = b.time_charging_min_bat_soc_allowed()

    # evaluation
    assert result == expected_result


@pytest.mark.parametrize(
    "ep_configured, price_limit_activated, price_charge_activated, price_threshold_mock, expected_result",
    [
        pytest.param(False, True, True, [True, True], True,
                     id="Preislimit aktiviert, aber kein Preis konfiguriert -> Eigenregelung -> laden"),
        pytest.param(True, True, False, [True], True,
                     id="Strompreis für Regelmodus, Preis unter Limit -> laden"),
        pytest.param(True, True, False, [False], False,
                     id="Strompreis für Regelmodus, Preis über Limit -> nicht laden"),
        pytest.param(True, False, True, [True], True,
                     id="Strompreis für aktives Laden, Preis unter Limit -> laden"),
        pytest.param(True, False, True, [False], False,
                     id="Strompreis für aktives Laden, Preis unter Limit -> nicht laden"),
        pytest.param(True, False, False, [], False,
                     id="beide Strompreise deaktiviert -> nicht laden"),
    ]
)
def test_time_charging_min_bat_soc_allowed_pricing(ep_configured: bool,
                                                   price_limit_activated: bool,
                                                   price_charge_activated: bool,
                                                   price_threshold_mock: List[bool],
                                                   expected_result: bool,
                                                   monkeypatch: pytest.MonkeyPatch):
    # setup
    b = BatAll()
    b.data.config.configured = True
    b.data.config.control_mode = BatControlMode.PRICE_BASED.value
    b.data.config.price_limit_activated = price_limit_activated
    b.data.config.price_charge_activated = price_charge_activated
    data.data.optional_data.data.electricity_pricing.configured = ep_configured
    b.data.config.bat_control_activated = True

    monkeypatch.setattr(data.data.optional_data, "ep_is_charging_allowed_price_threshold",
                        Mock(side_effect=price_threshold_mock))

    # execution
    result = b.time_charging_min_bat_soc_allowed()

    # evaluation
    assert result == expected_result


def test_get_power_limit_limit_charge_power(data_, monkeypatch):
    # LIMIT_CHARGE_POWER ist unabhängig von power_limit_controllable (bidirektionale Steuerung) -
    # ein Speicher, der nur set_charge_power_limit unterstützt, muss diesen Modus trotzdem nutzen können.
    b_all = BatAll()
    b_all.data.config.bat_control_activated = True
    b_all.data.config.control_mode = BatControlMode.LIMIT_CHARGE_POWER.value
    b_all.data.config.charge_power_limit = 3000
    b_all.data.get.power_limit_controllable = False
    monkeypatch.setattr(bat_all, "get_bat_components_by_controllability", Mock(return_value=([], [])))

    b_all.get_power_limit()

    assert b_all.data.set.power_limit is None
    assert b_all.data.set.charge_power_limit == 3000


def test_get_power_limit_limit_charge_power_requires_activation(data_, monkeypatch):
    b_all = BatAll()
    b_all.data.config.bat_control_activated = False
    b_all.data.config.control_mode = BatControlMode.LIMIT_CHARGE_POWER.value
    b_all.data.config.charge_power_limit = 3000
    monkeypatch.setattr(bat_all, "get_bat_components_by_controllability", Mock(return_value=([], [])))

    b_all.get_power_limit()

    assert b_all.data.set.charge_power_limit is None


def test_get_power_limit_other_modes_leave_charge_power_limit_none(data_, monkeypatch):
    b_all = BatAll()
    b_all.data.config.bat_control_activated = True
    b_all.data.config.control_mode = BatControlMode.BLOCK_DISCHARGE.value
    b_all.data.config.charge_power_limit = 3000
    b_all.data.get.power_limit_controllable = True
    monkeypatch.setattr(bat_all, "get_bat_components_by_controllability", Mock(return_value=([], [])))

    b_all.get_power_limit()

    assert b_all.data.set.charge_power_limit is None


def test_set_bat_charge_power_limit_caps_at_max_charge_power(data_, monkeypatch):
    b_all = BatAll()
    bat_component = MqttBat(MqttBatSetup(id=2), device_id=0)
    data.data.bat_data["bat2"].data.get.max_charge_power = 5000
    monkeypatch.setattr(bat_all, "get_bat_components_by_charge_power_controllability",
                        Mock(return_value=([bat_component], [])))

    b_all._set_bat_charge_power_limit(8000)

    assert data.data.bat_data["bat2"].data.set.charge_power_limit == 5000


def test_set_bat_charge_power_limit_passes_through_value_within_max(data_, monkeypatch):
    b_all = BatAll()
    bat_component = MqttBat(MqttBatSetup(id=2), device_id=0)
    data.data.bat_data["bat2"].data.get.max_charge_power = 5000
    monkeypatch.setattr(bat_all, "get_bat_components_by_charge_power_controllability",
                        Mock(return_value=([bat_component], [])))

    b_all._set_bat_charge_power_limit(2000)

    assert data.data.bat_data["bat2"].data.set.charge_power_limit == 2000


def test_set_bat_charge_power_limit_none_clears_cap(data_, monkeypatch):
    b_all = BatAll()
    bat_component = MqttBat(MqttBatSetup(id=2), device_id=0)
    data.data.bat_data["bat2"].data.set.charge_power_limit = 2000
    monkeypatch.setattr(bat_all, "get_bat_components_by_charge_power_controllability",
                        Mock(return_value=([bat_component], [])))

    b_all._set_bat_charge_power_limit(None)

    assert data.data.bat_data["bat2"].data.set.charge_power_limit is None


def _make_bat_device(component) -> Mock:
    device = Mock(spec=AbstractDevice)
    device.components = {"component0": component}
    return device


def _make_bat_component(comp_id: int, charge_power_limit_controllable: bool) -> Mock:
    component = Mock()
    component.component_config = Mock(type="bat", id=comp_id)
    component.charge_power_limit_controllable = Mock(return_value=charge_power_limit_controllable)
    component.fault_state = FaultState(ComponentInfo(comp_id, f"Speicher {comp_id}", "bat"))
    return component


def test_get_bat_components_by_charge_power_controllability_splits_by_capability(data_):
    controllable = _make_bat_component(1, True)
    not_controllable = _make_bat_component(2, False)
    data.data.system_data["dev1"] = _make_bat_device(controllable)
    data.data.system_data["dev2"] = _make_bat_device(not_controllable)

    result_controllable, result_not_controllable = bat_all.get_bat_components_by_charge_power_controllability()

    assert result_controllable == [controllable]
    assert result_not_controllable == [not_controllable]


def test_set_charge_power_limit_controllable_state(data_):
    controllable = _make_bat_component(1, True)
    data.data.system_data["dev1"] = _make_bat_device(controllable)
    data.data.bat_data["bat1"] = Bat(1)

    b_all = BatAll()
    b_all.set_charge_power_limit_controllable_state()

    assert b_all.data.get.charge_power_limit_controllable is True
    assert data.data.bat_data["bat1"].data.get.charge_power_limit_controllable is True
