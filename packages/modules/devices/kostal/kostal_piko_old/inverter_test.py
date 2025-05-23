from pathlib import Path
from unittest.mock import Mock

import pytest
from modules.common.component_state import InverterState

from modules.devices.kostal.kostal_piko_old import inverter
from modules.devices.kostal.kostal_piko_old.config import KostalPikoOldInverterSetup


@pytest.mark.parametrize("sample_file_name, expected_inverter_state",
                         [pytest.param("sample.html", InverterState(power=-50, exported=200), id="Inverter on"),
                          pytest.param("sample_off.html", InverterState(
                              power=0, exported=200), id="Inverter off")]
                         )
def test_parse_html(sample_file_name, expected_inverter_state, monkeypatch):
    # setup
    sample = (Path(__file__).parent / sample_file_name).read_text()
    mock_inverter_value_store = Mock()
    monkeypatch.setattr(inverter, 'get_inverter_value_store', Mock(return_value=mock_inverter_value_store))
    inv = inverter.KostalPikoOldInverter(KostalPikoOldInverterSetup(), device_id=0)
    inv.initialize()

    # execution
    inv.update(sample)

    # evaluation
    assert mock_inverter_value_store.set.call_count == 1
    assert vars(mock_inverter_value_store.set.call_args[0][0]) == vars(expected_inverter_state)
