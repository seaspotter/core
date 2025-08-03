#!/usr/bin/env python3
import logging
from typing import Iterable, Union

from modules.common import modbus
from modules.common.abstract_device import DeviceDescriptor
from modules.common.configurable_device import ComponentFactoryByType, ConfigurableDevice, MultiComponentUpdater
from modules.devices.saxpower.saxpower.bat import SaxpowerBat
from modules.devices.saxpower.saxpower.counter import SaxpowerCounter
from modules.devices.saxpower.saxpower.config import Saxpower, SaxpowerBatSetup, SaxpowerCounterSetup

log = logging.getLogger(__name__)


def create_device(device_config: Saxpower):
    client = None

    def create_counter_component(component_config: SaxpowerCounterSetup):
        nonlocal client
        return SaxpowerCounter(component_config, device_config=device_config, client=client)

    def create_counter_component(component_config: SaxpowerBatSetup):
        nonlocal client
        return SaxpowerBat(component_config, device_config=device_config, client=client)

    def update_components(components: Iterable[Union[SaxpowerBat, SaxpowerCounter]]):
        nonlocal client
        with client:
            for component in components:
                if isinstance(component, SaxpowerCounter):
                    component.update()
            for component in components:
                if isinstance(component, SaxpowerBat):
                    component.update()

    def initializer():
        nonlocal client
        client = modbus.ModbusTcpClient_(device_config.configuration.ip_address, device_config.configuration.port)

    return ConfigurableDevice(
        device_config=device_config,
        initializer=initializer,
        component_factory=ComponentFactoryByType(
            bat=create_bat_component,
            counter=create_counter_component,
        ),
        component_updater=MultiComponentUpdater(update_components)
    )


device_descriptor = DeviceDescriptor(configuration_factory=Saxpower)
