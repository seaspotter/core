import pytest

from modules.common.component_type import ComponentType, type_to_topic_mapping


@pytest.mark.parametrize("component_type, expected", [
    pytest.param(ComponentType.BAT_PRICE.value, "optional/ep/bat_price", id="bat_price_not_matched_as_bat"),
    pytest.param(ComponentType.PV_PRICE.value, "optional/ep/pv_price", id="pv_price"),
    pytest.param(ComponentType.FEED_IN_PRICE.value, "optional/ep/feed_in_price", id="feed_in_price"),
    pytest.param("bat", "bat", id="bat_unchanged"),
    pytest.param(ComponentType.GRID_FEE.value, "optional/ep/grid_fee", id="grid_fee_unchanged"),
])
def test_type_to_topic_mapping(component_type, expected):
    assert type_to_topic_mapping(component_type) == expected
