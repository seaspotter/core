#!/usr/bin/env python3
import asyncio
import logging

from modules.common.component_state import CarState
from modules.vehicles.saic_ismart.config import SaicIsmartConfiguration

from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration

log = logging.getLogger(__name__)

# 10-Bit-Sentinel: SAIC-Cloud hat keine aktuellen Daten vom Fahrzeug,
# muss erst per get_vehicle_status() geweckt werden.
SOC_INVALID_SENTINEL = 1023


async def _resolve_vin(api: SaicApi, configured_vin: str) -> str:
    if configured_vin:
        return configured_vin
    vehicles = await api.vehicle_list()
    if not vehicles.vinList:
        raise Exception("SAIC iSMART: Keine Fahrzeuge am Account gefunden!")
    for v in vehicles.vinList:
        if v.isCurrentVehicle:
            return v.vin
    return vehicles.vinList[0].vin


async def _fetch_soc(cfg: SaicIsmartConfiguration) -> CarState:
    api = SaicApi(SaicApiConfiguration(
        username=cfg.saic_user,
        password=cfg.saic_password,
        region=cfg.region,
    ))
    await api.login()

    try:
        vin = await _resolve_vin(api, cfg.vin)

        charge_data = await api.get_vehicle_charging_management_data(vin)
        soc = charge_data.chrgMgmtData.bmsPackSOCDsp if charge_data.chrgMgmtData else None

        if soc is None or soc == SOC_INVALID_SENTINEL:
            log.info("SAIC iSMART: Cloud-Daten veraltet, wecke Fahrzeug...")
            await api.get_vehicle_status(vin)
            await asyncio.sleep(5)
            charge_data = await api.get_vehicle_charging_management_data(vin)
            soc = charge_data.chrgMgmtData.bmsPackSOCDsp if charge_data.chrgMgmtData else None

        if soc is None or soc == SOC_INVALID_SENTINEL:
            raise Exception("SAIC iSMART: Kein gültiger SoC-Wert erhalten (Fahrzeug offline?)")

        rvs = charge_data.rvsChargeStatus
        vehicle_range = (rvs.fuelRangeElec / 10) if rvs and rvs.fuelRangeElec else None
        odometer = (rvs.mileage / 10) if rvs and rvs.mileage else None

        log.info("SAIC iSMART: SoC=%.1f%%, Reichweite=%s km, Odometer=%s km",
                 soc / 10, vehicle_range, odometer)
        return CarState(soc=soc / 10, range=vehicle_range, odometer=odometer)
    finally:
        api.logout()  # synchron, kein await!


def fetch_soc(config: SaicIsmartConfiguration) -> CarState:
    """Polling-Intervalle werden generisch von ConfigurableVehicle im
    Core gesteuert, nicht hier."""
    if not config.saic_user or not config.saic_password:
        raise Exception("SAIC iSMART: Zugangsdaten nicht konfiguriert!")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_fetch_soc(config))
    finally:
        loop.close()
