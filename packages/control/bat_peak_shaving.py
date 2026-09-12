"""Prognosebasierte Lastspitzenkappung (BatControlMode.PEAK_SHAVING).

Reine Entscheidungslogik, unabhaengig von MQTT/dataclass-Anbindung, damit sie ohne die
Prognose-Anbindung (siehe PR #3782, "openWB/forecast/get/values") isoliert entwickelt und
getestet werden kann. bat_all.py ruft BatPeakShaving().get_charge_power() analog zu
get_power_limit() auf und schreibt das Ergebnis nach self.data.set.charge_power_limit -
genau wie bei LIMIT_CHARGE_POWER nutzt Peak Shaving ausschliesslich die
Ladeleistungsbegrenzung (set_charge_power_limit), nie set_power_limit; Entladung/Timing
bleiben Sache der Eigenregelung des Speichers.

Fensterermittlung: eine Stunde zaehlt als "im Fenster", wenn ihre Prognose mindestens
window_threshold_fraction des Tagesmaximums erreicht. Ausserhalb des Fensters (davor UND
danach) sowie ohne bestaetigten Ueberschuss wird auf hold_power gehalten; einen
Nach-Fenster-Freigabe-Zustand (wie im Vorbild-Skript) gibt es hier bewusst nicht - das
uebernimmt bei Bedarf ein anschliessender Zeitplan (BatControlMode.SCHEDULED).

Ladeleistungsaenderungen werden pro Zyklus auf max_step begrenzt und muessen mindestens
min_dwell_duration Sekunden auseinanderliegen (siehe _ramp()) - ohne das wuerde die
prognosegepacete Leistung bei jedem Regelzyklus (Sekundentakt, nicht der 5-Minuten-Takt
des Vorbild-Skripts) minimal schwanken und das Ladeleistungs-Register unnoetig oft
beschreiben (Flash-Verschleiss). Die eine Ausnahme ist der Kontrollverlust bei zu
niedriger Tagesprognose (siehe get_charge_power()) - das ist ein bewusster
Freigabe-Zustandswechsel wie bat_control_activated in bat_all.py, kein Regelschritt.
"""
from dataclasses import dataclass
from enum import Enum
import logging
from typing import Optional, Sequence

from helpermodules import timecheck

log = logging.getLogger(__name__)

QUARTERS_PER_HOUR = 4
MINUTES_PER_QUARTER = 15


class PeakShavingMode(Enum):
    # Ladeleistung wird ueber den Prognose-Tagesverlauf so skaliert, dass die Flaeche
    # darunter (jetzt bis Fensterende) der verbleibenden nutzbaren Speicherkapazitaet
    # entspricht - braucht eine konfigurierte Speicherkapazitaet.
    PACED = "paced"
    # Feste Ladeleistungs-Obergrenze im Fenster, keine Kapazitaet noetig.
    FIXED = "fixed"


@dataclass
class SurplusWindow:
    start_hour: int
    end_hour: int  # inklusiv

    def contains(self, hour: int) -> bool:
        return self.start_hour <= hour <= self.end_hour


class BatPeakShaving:
    def __init__(self) -> None:
        # Zustand fuer _ramp() - ueber Regelzyklen hinweg gueltig, siehe Modul-Docstring.
        # Wird von bat_all.py als langlebige Instanz gehalten, genau wie
        # SurplusControlled/NoCurrent in algorithm.py.
        self._last_power: Optional[int] = None
        self._last_change_timestamp: Optional[float] = None

    def get_charge_power(self,
                         mode: str,
                         hourly_forecast: Sequence[float],
                         quarterly_forecast: Sequence[float],
                         daily_forecast: float,
                         now_hour: int,
                         now_quarter_idx: int,
                         grid_power: float,
                         ev_charging: bool,
                         remaining_capacity: float,
                         min_daily_forecast: float,
                         window_threshold_fraction: float,
                         hold_power: int,
                         export_threshold: float,
                         fixed_charge_power: int,
                         max_step: int,
                         min_dwell_duration: int) -> Optional[int]:
        """Liefert die fuer diesen Zyklus zu setzende Ladeleistung, oder None fuer
        Kontrollverlust (Speicher laedt unbegrenzt/Eigenregelung).

        Reihenfolge:
        1. Tagesprognose unter min_daily_forecast (z.B. Winter, kein nennenswerter Peak zu
           schuetzen) -> None, keine Kapazitaet aufsparen.
        2. Kein heutiges Ueberschussfenster oder aktuelle Stunde ausserhalb davon ->
           hold_power (gilt fuer davor UND danach gleichermassen, siehe Modul-Docstring).
        3. Im Fenster, aber weder EV laedt noch Netz-Einspeisung bestaetigt -> hold_power
           (Kapazitaet fuer den erwarteten Peak aufsparen).
        4. Im Fenster mit bestaetigtem Ueberschuss -> je nach mode entweder die feste
           Obergrenze (FIXED) oder die kapazitaetsgepacete Ladeleistung (PACED).
        Faelle 2-4 durchlaufen zusaetzlich _ramp(), Fall 1 nicht (siehe Modul-Docstring).
        """
        if daily_forecast < min_daily_forecast:
            log.debug("Aktive Speichersteuerung: Peak Shaving - Tagesprognose "
                      f"({daily_forecast}kWh) unter Mindestwert ({min_daily_forecast}kWh), "
                      "kein nennenswerter Peak - Kontrollverlust.")
            self._last_power = None
            self._last_change_timestamp = None
            return None

        window = self._surplus_window(hourly_forecast, window_threshold_fraction)
        if window is None or not window.contains(now_hour):
            log.debug("Aktive Speichersteuerung: Peak Shaving - kein Ueberschussfenster heute oder "
                      f"ausserhalb des Fensters, halte {hold_power}W.")
            return self._ramp(hold_power, max_step, min_dwell_duration)
        if not (ev_charging or self._export_confirmed(grid_power, export_threshold)):
            log.debug(f"Aktive Speichersteuerung: Peak Shaving - im Fenster ({window.start_hour}-"
                      f"{window.end_hour}h), aber kein bestaetigter Ueberschuss, halte {hold_power}W.")
            return self._ramp(hold_power, max_step, min_dwell_duration)
        if mode == PeakShavingMode.FIXED.value:
            log.debug(f"Aktive Speichersteuerung: Peak Shaving - im Fenster, feste Ladeleistung "
                      f"{fixed_charge_power}W.")
            return self._ramp(fixed_charge_power, max_step, min_dwell_duration)
        window_end_quarter_idx = (window.end_hour + 1) * QUARTERS_PER_HOUR
        power = self._paced_charge_power(
            quarterly_forecast, now_quarter_idx, window_end_quarter_idx, remaining_capacity, fixed_charge_power)
        log.debug(f"Aktive Speichersteuerung: Peak Shaving - im Fenster, prognosegepacete Ladeleistung {power}W.")
        return self._ramp(power, max_step, min_dwell_duration)

    def _surplus_window(self,
                        hourly_forecast: Sequence[float],
                        threshold_fraction: float) -> Optional[SurplusWindow]:
        # Eine Stunde gilt als "im Fenster", wenn ihr Prognosewert mindestens
        # threshold_fraction des Tagesmaximums erreicht. start/end sind die erste/letzte
        # Stunde, die die Schwelle erreicht - Stunden dazwischen, die die Schwelle knapp
        # verfehlen, zaehlen weiterhin als "im Fenster" (ein kurzer Wolkendurchzug soll das
        # Fenster nicht zerreissen). None, wenn keine Prognose vorliegt oder das
        # Tagesmaximum <= 0 ist (z.B. Nacht/Winter).
        if not hourly_forecast:
            return None
        peak = max(hourly_forecast)
        if peak <= 0:
            return None
        threshold = peak * threshold_fraction
        start: Optional[int] = None
        end: Optional[int] = None
        for hour, value in enumerate(hourly_forecast):
            if value >= threshold:
                if start is None:
                    start = hour
                end = hour
        if start is None or end is None:
            return None
        return SurplusWindow(start_hour=start, end_hour=end)

    def _export_confirmed(self, grid_power: float, export_threshold: float) -> bool:
        # grid_power folgt der ueblichen openWB-Konvention: negativ = Einspeisung.
        return grid_power < export_threshold

    def _paced_charge_power(self,
                            quarterly_forecast: Sequence[float],
                            now_quarter_idx: int,
                            window_end_quarter_idx: int,
                            remaining_capacity: float,
                            fallback_power: int,
                            min_remaining_forecast: float = 0.3) -> int:
        # Skaliert die Prognose-Kurve (15-Min-Werte) von jetzt bis Fensterende so, dass die
        # Flaeche darunter der verbleibenden nutzbaren Kapazitaet entspricht. Faellt auf
        # fallback_power zurueck, wenn Kapazitaet fehlt/0 ist oder die verbleibende
        # Prognose zu klein ist, um den Skalierungsfaktor sinnvoll zu berechnen (z.B. kurz
        # vor Fensterende).
        if remaining_capacity <= 0:
            return fallback_power
        start = max(0, min(now_quarter_idx, len(quarterly_forecast)))
        end = max(start, min(window_end_quarter_idx, len(quarterly_forecast)))
        remaining_forecast = sum(quarterly_forecast[start:end]) * MINUTES_PER_QUARTER / 60 / 1000
        if remaining_forecast < min_remaining_forecast:
            return fallback_power
        factor = remaining_capacity / remaining_forecast
        current_forecast = quarterly_forecast[start] if start < len(quarterly_forecast) else 0
        return round(current_forecast * factor)

    def _ramp(self, target: int, max_step: int, min_dwell_duration: int) -> int:
        # Begrenzt Aenderungen der Ladeleistung auf max_step pro Zyklus und mindestens
        # min_dwell_duration Sekunden Abstand zwischen zwei Aenderungen - schont das
        # Ladeleistungs-Register bei jedem schnellen openWB-Regelzyklus (siehe
        # Modul-Docstring). Erster Aufruf (self._last_power is None) setzt direkt auf
        # target, ohne Rampe - es gibt noch keinen Vorwert, den man schonen koennte.
        if self._last_power is None:
            power = target
        elif (min_dwell_duration > 0 and self._last_change_timestamp is not None and
                timecheck.check_timestamp(self._last_change_timestamp, min_dwell_duration)):
            power = self._last_power
        else:
            step = max(-max_step, min(max_step, target - self._last_power))
            power = self._last_power + step
        if power != self._last_power:
            self._last_power = power
            self._last_change_timestamp = timecheck.create_timestamp()
        return power
