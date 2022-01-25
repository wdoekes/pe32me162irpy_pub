class WattGauge:
    """
    WattGauge attempts to approximate current Watt (Joule/s)
    production/consumption based on a regular input of current absolute
    (increasing) watt hour values.

    For power/electricity meters that do not provide "current" Watt usage
    but do provide current totals, feeding this regular updates will allow
    a fair average to be calculated.

    --------
    Use case
    --------

    We need to get a lot of input, or else the Watt approximation makes no
    sense for low Wh deltas. For 550W, we'd still only get 9.17 Wh per minute.
    If we simply count the delta over 60s, we would oscillate between (5x) 9
    (540W) and (1x) 10 (600W):

    - (3600/60)*9  = 540W
    - (3600/60)*10 = 600W

    However, if we sample every second or so, you'll get 9 Wh for 59 seconds
    instead, which is a lot closer to the actual value.

    - (3600/59)*9 = 549W

    -----
    Usage
    -----

    prod = WattGauge()

    # Running set_active_energy_total more often will get better averages:
    schedule_every_second((lambda: (
        prod.set_active_energy_total(millis(), get_watthour()))))

    # Running get_instantaneous_power less often will get better averages;
    # .. but you're free to get_watt()/reset() whenever you like;
    # .. and an interval of 15s is fine for higher wattage (>1500).
    schedule_every_x_seconds((lambda: (
        publish(prod.get_instantaneous_power()) and prod.reset())))
    """
    def __init__(self):
        self._t = [0, 0, 0]  # t0, t(end-1), t(end)
        self._p = [0, 0, 0]  # P(sum) in t[n]
        self._tlast = 0      # latest time, even without changed data
        self._watt = 0       # average value, but only if it makes some sense

    def get_active_energy_total(self):
        "Get the latest stored value in watt hours"
        return self._p[2]

    def get_instantaneous_power(self):
        "Get a best guess of the current power usage in watt"
        return self._watt

    def interval_since_last_change(self):
        "Is there anything report for this interval?"
        return self._tlast - self._t[2]

    def set_active_energy_total(self, time_ms, current_wh):
        "Feed data to the WattGauge: do this often"
        self._tlast = time_ms

        try:
            self._data_valid
        except AttributeError:
            # Happens only once after construction
            self._t[0] = self._t[1] = self._t[2] = time_ms
            self._p[0] = self._p[1] = self._p[2] = current_wh
            self._watt = 0
            self._data_valid = True
            return

        # If there was no change. Do nothing.
        if current_wh == self._p[2]:
            # Except if there was activity earlier, but not anymore.
            # 60 W is 1 Wh/min, so let's recalculate based on the
            # latest values only.
            if (self._tlast - self._t[2]) > 30000:
                possible_watt = (1000 * 3600 // (self._tlast - self._t[2]))
                if possible_watt < self._watt:
                    self._watt = possible_watt
            return

        # Set first change
        if self._t[0] == self._t[1]:
            self._t[1] = self._t[2] = time_ms
            self._p[1] = self._p[2] = current_wh
        # Update next to last change
        else:
            self._t[1] = self._t[2]
            self._p[1] = self._p[2]
            self._t[2] = time_ms
            self._p[2] = current_wh

        # If the difference between the deltas is large, then
        # force a reset based on the previous value.
        # - If delta between t[0] and t[1] is more than 60 seconds;
        # - and that is only one change (1 Wh);
        # - and recent changes are 4+ times faster.
        if ((self._t[1] - self._t[0]) > 60000 and
                (self._p[1] - self._p[0]) <= 1 and
                (self._t[2] - self._t[1]) < 15000):
            # This fixes a quicker increase if usage suddenly spikes.
            self.reset()

        self._recalculate_if_sensible()

    def reset(self):
        """
        After reading get_instantaneous_power() you'll generally want to
        reset the state to start a new measurement interval
        """
        if self._there_are_enough_values:
            # We don't touch the _watt average. Also note that we update to
            # the latest time-in-which-there-was-a-change.
            self._t[0] = self._t[1]
            self._p[0] = self._p[1]
            self._t[1] = self._t[2]
            self._p[1] = self._p[2]

    @property
    def _tdelta(self):
        return self._t[2] - self._t[0]

    @property
    def _pdelta(self):
        return self._p[2] - self._p[0]

    @property
    def _there_are_enough_values(self):
        """
        Are there enough values to make any reasonable estimate?
        - Minimum sampling interval: 20s
        - Minimum sampling size: 6
        """
        return (
            (self._tdelta >= 20000 and self._pdelta >= 6) or
            (self._tdelta >= 50000 and self._pdelta >= 2) or
            (self._tdelta >= 300000))

    def _recalculate_if_sensible(self):
        """
        Recalculate watt usage, but only if there are enough values
        """
        if self._there_are_enough_values:
            self._watt = self._pdelta * 1000 * 3600 // self._tdelta
        elif (self._tlast - self._t[0]) > 300000:
            self._watt = 0


class EnergyGauge:
    """
    EnergyGauge combines two WattGauge gauges to monitor both positive
    and negative energy.

    The combination is needed because a proper estimate for either can
    only be given if the other is known to have a 0-delta.
    """
    def __init__(self):
        self._positive = WattGauge()
        self._negative = WattGauge()
        self._wprev = 0

    def get_positive_active_energy_total(self):
        return self._positive.get_active_energy_total()

    def get_negative_active_energy_total(self):
        return self._negative.get_active_energy_total()

    def get_instantaneous_power(self):
        if (self._positive.interval_since_last_change() <
                self._negative.interval_since_last_change()):
            return self._positive.get_instantaneous_power()
        return -self._negative.get_instantaneous_power()

    def has_significant_change(self):
        watt, wprev = self.get_instantaneous_power(), self._wprev
        if (wprev < 0 and watt > 0) or (watt < 0 and wprev > 0):
            return True     # sign change is significant
        elif wprev == 0 and -20 < watt < 20:
            return False    # fluctuating around 0 is not significant
        elif wprev == 0:
            return True     # otherwise a change from 0 is significant

        factor = float(watt) / float(wprev)
        if 0.6 < factor < 1.6:
            return False    # change factor is small

        return True         # yes, significant

    def set_positive_active_energy_total(self, time_ms, current_wh):
        self._positive.set_active_energy_total(time_ms, current_wh)

    def set_negative_active_energy_total(self, time_ms, current_wh):
        self._negative.set_active_energy_total(time_ms, current_wh)

    def reset(self):
        self._wprev = self.get_instantaneous_power()
        self._positive.reset()
        self._negative.reset()
