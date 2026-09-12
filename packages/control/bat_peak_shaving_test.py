import pytest

from control.bat_peak_shaving import BatPeakShaving, PeakShavingMode, SurplusWindow

# Fuer Tests, die nur einen einzelnen Aufruf pruefen: min_daily_forecast/max_step/
# min_dwell_duration so gewaehlt, dass sie nicht eingreifen (siehe eigene Tests dafuer
# unten) - der erste Aufruf einer frischen BatPeakShaving-Instanz rampt ohnehin nicht.
DAILY_FORECAST = 10.0
MIN_DAILY_FORECAST = 1.0
MAX_STEP = 5000
MIN_DWELL_DURATION = 0


@pytest.fixture
def peak_shaving() -> BatPeakShaving:
    return BatPeakShaving()


@pytest.mark.parametrize(
    "hourly_forecast, threshold_fraction, expected_window",
    [
        pytest.param([0]*6+[1000, 3000, 5000, 6000, 5000, 3000, 1000]+[0]*11, 0.8,
                     SurplusWindow(start_hour=8, end_hour=10), id="klares Mittagsfenster"),
        pytest.param([0]*24, 0.8, None, id="keine Prognose - kein Fenster"),
        pytest.param([100]*24, 0.8, SurplusWindow(start_hour=0, end_hour=23), id="konstante Prognose - ganztags"),
        pytest.param([0, 1000, 100, 1000, 0]+[0]*19, 0.8,
                     SurplusWindow(start_hour=1, end_hour=3), id="Wolkendurchzug reisst Fenster nicht auseinander"),
    ])
def test_surplus_window(peak_shaving: BatPeakShaving, hourly_forecast, threshold_fraction, expected_window):
    assert peak_shaving._surplus_window(hourly_forecast, threshold_fraction) == expected_window


@pytest.mark.parametrize(
    "grid_power, export_threshold, expected",
    [
        pytest.param(-500, -100, True, id="Einspeisung deutlich ueber Schwelle"),
        pytest.param(-50, -100, False, id="Einspeisung unterhalb der Schwelle - Messrauschen"),
        pytest.param(200, -100, False, id="Netzbezug"),
    ])
def test_export_confirmed(peak_shaving: BatPeakShaving, grid_power, export_threshold, expected):
    assert peak_shaving._export_confirmed(grid_power, export_threshold) is expected


def test_paced_charge_power_scales_to_remaining_capacity(peak_shaving: BatPeakShaving):
    # 4 verbleibende 15-Min-Slots a 2000W = 2kWh Prognose, 1kWh verbleibende Kapazitaet
    # -> Faktor 0.5, aktueller Slot wird entsprechend skaliert.
    quarterly_forecast = [2000, 2000, 2000, 2000]
    power = peak_shaving._paced_charge_power(
        quarterly_forecast, now_quarter_idx=0, window_end_quarter_idx=4,
        remaining_capacity=1.0, fallback_power=3000)
    assert power == 1000


def test_paced_charge_power_falls_back_without_capacity(peak_shaving: BatPeakShaving):
    power = peak_shaving._paced_charge_power(
        [2000, 2000], now_quarter_idx=0, window_end_quarter_idx=2,
        remaining_capacity=0, fallback_power=3000)
    assert power == 3000


def test_paced_charge_power_falls_back_when_remaining_forecast_too_low(peak_shaving: BatPeakShaving):
    power = peak_shaving._paced_charge_power(
        [10], now_quarter_idx=0, window_end_quarter_idx=1,
        remaining_capacity=1.0, fallback_power=3000, min_remaining_forecast=0.3)
    assert power == 3000


def test_get_charge_power_holds_before_window(peak_shaving: BatPeakShaving):
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=6, now_quarter_idx=24,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power == 200


def test_get_charge_power_holds_in_window_without_confirmed_surplus(peak_shaving: BatPeakShaving):
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=9, now_quarter_idx=36,
        grid_power=50, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power == 200


def test_get_charge_power_fixed_mode_in_window_with_export(peak_shaving: BatPeakShaving):
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=9, now_quarter_idx=36,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power == 3000


def test_get_charge_power_fixed_mode_in_window_with_ev_charging(peak_shaving: BatPeakShaving):
    # Ladendes EV unterdrueckt ggf. die Netz-Einspeisung, obwohl noch PV-Ueberschuss da ist -
    # zaehlt daher auch ohne bestaetigte Einspeisung als Freigabe.
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=9, now_quarter_idx=36,
        grid_power=50, ev_charging=True,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power == 3000


def test_get_charge_power_paced_mode_in_window_with_export(peak_shaving: BatPeakShaving):
    # Fenster 9-11h (Schwelle 0.8 x 6000 = 4800, Stunden 9/10/11 erreichen sie).
    hourly_forecast = [0]*9+[5000, 6000, 5000]+[0]*12
    # 12 Quartale (Stunden 9-11) a 4000W = 12kWh Prognose, 3kWh verbleibende Kapazitaet
    # -> Faktor 0.25, aktueller Quartalswert (4000W) wird entsprechend skaliert.
    quarterly_forecast = [0]*36 + [4000]*12 + [0]*48
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.PACED.value,
        hourly_forecast=hourly_forecast,
        quarterly_forecast=quarterly_forecast,
        daily_forecast=DAILY_FORECAST,
        now_hour=9, now_quarter_idx=36,
        grid_power=-500, ev_charging=False,
        remaining_capacity=3.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power == 1000


def test_get_charge_power_no_window_today(peak_shaving: BatPeakShaving):
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*24,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=12, now_quarter_idx=48,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power == 200


def test_get_charge_power_releases_control_on_low_daily_forecast(peak_shaving: BatPeakShaving):
    # Wintertag: Tagesprognose unter min_daily_forecast -> kein nennenswerter Peak, der
    # sich zu schuetzen lohnt - Kontrollverlust statt hold_power, unabhaengig vom Fenster.
    power = peak_shaving.get_charge_power(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=30.0,
        now_hour=9, now_quarter_idx=36,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=50.0,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100, fixed_charge_power=3000,
        max_step=MAX_STEP, min_dwell_duration=MIN_DWELL_DURATION)
    assert power is None


def test_get_charge_power_ramp_limits_step_per_cycle(peak_shaving: BatPeakShaving):
    kwargs = dict(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=9, now_quarter_idx=36,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        export_threshold=-100,
        max_step=500, min_dwell_duration=0)
    first = peak_shaving.get_charge_power(hold_power=200, fixed_charge_power=200, **kwargs)
    assert first == 200
    second = peak_shaving.get_charge_power(hold_power=200, fixed_charge_power=3000, **kwargs)
    # Sprung 200 -> 3000 wird auf max_step=500 pro Zyklus begrenzt.
    assert second == 700


def test_get_charge_power_dwell_holds_previous_value(peak_shaving: BatPeakShaving):
    kwargs = dict(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        daily_forecast=DAILY_FORECAST,
        now_hour=9, now_quarter_idx=36,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0, min_daily_forecast=MIN_DAILY_FORECAST,
        window_threshold_fraction=0.8,
        export_threshold=-100,
        max_step=5000, min_dwell_duration=3600)
    first = peak_shaving.get_charge_power(hold_power=200, fixed_charge_power=200, **kwargs)
    assert first == 200
    # Trotz max_step=5000 (kein Sprunglimit) bleibt die Leistung waehrend der Dwell-Zeit
    # (1h) auf dem alten Wert, da seit der letzten Aenderung keine Sekunde vergangen ist.
    second = peak_shaving.get_charge_power(hold_power=200, fixed_charge_power=3000, **kwargs)
    assert second == 200


def test_get_charge_power_low_daily_forecast_resets_ramp_state(peak_shaving: BatPeakShaving):
    kwargs = dict(
        mode=PeakShavingMode.FIXED.value,
        hourly_forecast=[0]*8+[5000]*4+[0]*12,
        quarterly_forecast=[0]*96,
        now_hour=9, now_quarter_idx=36,
        grid_power=-500, ev_charging=False,
        remaining_capacity=5.0,
        window_threshold_fraction=0.8,
        hold_power=200, export_threshold=-100,
        max_step=500, min_dwell_duration=0)
    peak_shaving.get_charge_power(daily_forecast=DAILY_FORECAST, min_daily_forecast=MIN_DAILY_FORECAST,
                                  fixed_charge_power=3000, **kwargs)
    assert peak_shaving.get_charge_power(daily_forecast=30.0, min_daily_forecast=50.0,
                                         fixed_charge_power=3000, **kwargs) is None
    # Nach Kontrollverlust startet die naechste Freigabe wieder ohne Rampe direkt beim Ziel.
    resumed = peak_shaving.get_charge_power(daily_forecast=DAILY_FORECAST, min_daily_forecast=MIN_DAILY_FORECAST,
                                            fixed_charge_power=3000, **kwargs)
    assert resumed == 3000
