"""
XXX: iets met OBIS/EDIS codes

En iets met de ISKRA/ME162-specific values.

Example:

    obis = Me162Obis.from_code('1.8.0').set_value('0033402.264*kWh')
    obis.code == '1.8.0'
    obis.unit == 'Wh'
    obis.description == '...iets..met..active..energy...'
    obis.value == 33402.264

XXX: alleen voor Me162 subclass voor de F.F de Me162.. de rest gewone obis?
"""
from decimal import Decimal


class DecimalWithUnit(Decimal):
    @classmethod
    def with_unit(cls, value, unit):
        ret = cls(value)
        ret.unit = unit
        return ret

    def __str__(self, format_spec=''):
        return '{} {}'.format(super().__str__(), self.unit)
    __format__ = __str__


class ElectricityObis:
    """
    Object Identification System (OBIS) code and value

    Successor to Electronic Data Interchange Standards (EDIS) code.

    Format:

        A-B:C.D.E*F

    All these may or may not be present in the identifier
    (e.g. groups A and B are often omitted).

    - The A group specifies the medium (0=abstract objects,
      1=electricity, 6=heat, 7=gas, 8=water ...)
    - The B group specifies the channel. Each device with
      multiple channels generating measurement results, can
      separate the results into the channels.
    - The C group specifies the physical value (current,
      voltage, energy, level, temperature, ...)
    - The D group specifies the quantity computation result of
      specific [algorithm]
    - The E group specifies the measurement type defined by
      groups A to D into individual measurements (e.g. switching
      ranges)
    - The F group separates the results partly defined by
      groups A to E. The typical usage is the specification of
      individual time ranges.
      (For example: in the ME162, 1.8.0*08 would request the value for 8
      billing periods ago.)

    Here, we process electricity (A=1, ElectricityObis) and we only
    concern us with the C.D.E and optionally *F.

    Usage:

        obis = ElectricityObis.from_code('1.8.0')
        obis.set_value(33402.264, 'kWh')
    """
    unit = NotImplemented

    @classmethod
    def from_code(cls, code):
        try:
            if code == 'F.F':
                code = 'F.F.0'  # "F.F" on ISKRA ME162
            c, d, e = code.split('.')
            c = int(c) if c.isdigit() else c
            d = int(d) if d.isdigit() else d
            f = None
            if '*' in e:
                e, f = e.split('*')
                e = int(e)
                f = int(f)
            else:
                e = int(e)
        except ValueError:
            raise NotImplementedError(f'cannot parse code {code!r}')

        # 1.8.0, 15.8.0, ...
        if c in (1, 2, 15, 16) and d == 8:
            return ActiveEnergyElectricityObis(c, d, e, f)
        # 1.7.0, 15.7.0, ...
        elif c in (1, 2, 15, 16) and d == 7:
            return InstantaneousPowerElectricityObis(c, d, e, f)
        # C.1.0, 0.0.0, 0.9.1
        elif f is None and (c == 0 or code == 'C.1.0' or code == 'F.F.0'):
            return MiscObis(c, d, e, f)
        raise NotImplementedError(f'unknown/unhandled code {code!r}')

    def __init__(self, c, d, e, f):
        self.c, self.d, self.e, self.f = c, d, e, f
        self._value = 0

    @property
    def code(self):
        if self.f is not None:
            return f'{self.c}.{self.d}.{self.e}*{self.f}'
        return f'{self.c}.{self.d}.{self.e}'

    @property
    def value(self):
        if self.unit is NotImplemented:
            return self._value
        return DecimalWithUnit.with_unit(self._value, self.unit)

    def set_value(self, value, unit=None):
        if unit is None:
            pass
        elif unit == self.unit:
            pass
        elif unit[0:1] == 'k' and unit[1:] == self.unit:
            value *= 1000
            value = int(value)  # drop extra (probably) useless decimals
        else:
            raise NotImplementedError(f'unhandled unit {unit!r}')
        self._value = value
        return self

    def __repr__(self):
        return f'<{self.code}({self.value})>'


class ActiveEnergyElectricityObis(ElectricityObis):
    unit = 'Wh'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.d == 8, self.code
        parts = f'in T{self.e}' if self.e else 'total'
        self.description = {
            1: f'Positive active energy (A+) {parts}',  # 1.8.x
            2: f'Negative active energy (A-) {parts}',  # 2.8.x
            15: f'Absolute active energy (A+) {parts} (=A+ - A-)',  # 15.8.x
            16: (f'Sum active energy without reverse blockade {parts} '
                 f'(=A+ - A-)'),    # 16.8.x
        }[self.c]


class InstantaneousPowerElectricityObis(ElectricityObis):
    unit = 'W'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.d == 7 and self.e == 0, self.code
        self.description = {
            1: 'Positive active instantaneous power (A+)',      # 1.7.0
            2: 'Negative active instantaneous power (A-)',      # 2.7.0
            15: 'Absolute active instantaneous power (|A|)',    # 15.7.0
            16: 'Sum active instantaneous power (A+ - A-)',     # 16.7.0
        }[self.c]


class MiscObis(ElectricityObis):
    """
    Other data types found in the ME162.

    Obis('C.1.0', 'Meter serial number'),
    Obis('F.F', 'Fatal error meter status'),  # should hold "0000000"
    Obis('0.9.1', 'Time (returns (hh:mm:ss))'),
    Obis('0.9.2', 'Date (returns (YY.MM.DD))'),

    In other specifications (than the ME162), the "F.F" might be
    "F.F.0".

    For the ME162, it is:
    > 2.6.4. Error register description
    > The error register F.F is a hexadecimal value and
    > generates the following alarms when particular bits
    > are set to 1.
    > Bit Error description
    > 0   Check sum error in energy registers in EEPROM
    > 1   Check sum error of meter parameters in EEPROM
    > 2   Check sum error of meter parameters in RAM
    > 3   Check sum error of program code
    > 4   False tariff table
    > 5   Not implemented
    > 6   Not implemented
    > 7   Not implemented
    > (We expect this to look like "000001F" if bits 0..4 are set.)
    """
    pass
