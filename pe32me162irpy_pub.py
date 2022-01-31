#!/usr/bin/env python3
import asyncio
import logging
import os
import serial
import serial_asyncio
import sys
import termios
import time

from collections import namedtuple
from contextlib import AsyncExitStack
from decimal import Decimal
from enum import Enum

from asyncio_mqtt import Client as MqttClient

try:
    from .ctrlcode import ACK, CR, EOT, ETX, LF, NAK, SOH, STX
    from .din66219 import append_bcc, check_bcc
    from .obis import DecimalWithUnit, ElectricityObis
    from .wattgauge import EnergyGauge
except ImportError:
    from ctrlcode import ACK, CR, EOT, ETX, LF, NAK, SOH, STX
    from din66219 import append_bcc, check_bcc
    from obis import DecimalWithUnit, ElectricityObis
    from wattgauge import EnergyGauge

__version__ = 'pe32me162irpy_pub-FIXME'

log = logging.getLogger(__name__)


def parse_iec6205621_dataset(dataset):
    # dataset ::= address? "(" value? ( "*" unit )? ")"
    try:
        address, rest = dataset.split('(', 1)
        if not rest.endswith(')'):
            raise ValueError
    except ValueError:
        raise ValueError(f'error parsing dataset {dataset!r}')
    return (address,) + parse_iec6205621_value(rest[0:-1])


def parse_iec6205621_value(value):
    # address ::= (max 16 chars, except for "()/!")
    # value ::= (max 128 chars, except for "()/!*"; decimals use 1 period)
    # unit ::= (max 16 chars , except for "()/!")
    if '*' in value:
        # For values with units, we'll assume they're in single-period
        # decimal.
        value, unit = value.split('*', 1)
        value = Decimal(value)
    else:
        # For unit-less values, we do not make assumptions about the
        # data type (int, decimal, hex?).
        unit = None
    return value, unit


def unpack_iec6205621_datamessage(buf):
    # datamessage ::= STX datablock "!" CR LF ETX bcc  (readout mode)
    # datamessage ::= STX dataset ETX bcc  (programming mode)
    check_bcc(buf)  # raises ValueError on failure
    return buf[1:-2].decode('ascii')  # remove {STX}...{ETX}{$BCC}


class Pe32Me162Publisher:
    def __init__(self):
        self._mqtt_broker = os.environ.get(
            'PE32ME162_BROKER', 'test.mosquitto.org')
        self._mqtt_topic = os.environ.get(
            'PE32ME162_TOPIC', 'myhome/infra/power/xwwwform')
        self._mqttc = None
        self._guid = os.environ.get(
            'PE32ME162_GUID', 'EUI48:11:22:33:44:55:66')

    def open(self):
        # Unfortunately this does use a thread for keepalives. Oh well.
        # As long as it's implemented correctly, I guess we can live
        # with it.
        self._mqttc = MqttClient(self._mqtt_broker)
        return self._mqttc

    def publish(self, pos_act, neg_act, inst_pwr):
        # FIXME:
        # 2022-01-24 23:10:58 Task exception was never retrieved
        # future: <Task finished name='Task-4'
        #   coro=<IskraMe162ValueProcessor.publish() done,
        #   defined at ./iec62056_test_client.py:148> exception=ValueError()>
        # Traceback (most recent call last):
        #   File "./iec62056_test_client.py", line 152, in publish
        #     raise ValueError('x')  # should be caught somewhere!!!
        # ValueError: x
        asyncio.create_task(self._publish(pos_act, neg_act, inst_pwr))

    async def _publish(self, pos_act, neg_act, inst_pwr):
        log.debug(
            f'_publish: 1.8.0 {pos_act}, 2.8.0 {neg_act}, '
            f'16.7.0 {inst_pwr}')

        tm = int(time.time())
        mqtt_string = (
            f'device_id={self._guid}&'
            f'e_pos_act_energy_wh={int(pos_act)}&'
            f'e_neg_act_energy_wh={int(neg_act)}&'
            f'e_inst_power_w={int(inst_pwr)}&'
            f'dbg_uptime={tm}&'
            f'dbg_version={__version__}').encode('ascii')

        await self._mqttc.publish(self._mqtt_topic, payload=mqtt_string)

        log.info(
            f'Published: 1.8.0 {pos_act}, 2.8.0 {neg_act}, '
            f'16.7.0 {inst_pwr}')


class IskraMe162ValueProcessor:
    def __init__(self, publisher=None):
        self._publisher = publisher
        self._gauge = EnergyGauge()
        self._last_sane_value = int(time.time() * 1000)  # start valid

    def ms_since_last_value(self):
        return int(time.time() * 1000 - self._last_sane_value)

    def should_stop(self):
        """
        Return true if we're done. Return false to keep getting values.
        """
        return False

    def set_readout(self, text_readout):
        """
        Accept textual readout, useful for debug purposes
        """
        log.info('[text readout] %r', text_readout)

    def set_register(self, address, value, unit):
        """
        Accept register values

        Usage:

            set_register('1.8.0', Decimal('33402.264'), 'kWh')
        """
        obis = ElectricityObis.from_code(address).set_value(value, unit)
        current_ms = int(time.time() * 1000)

        log.info('set_register (at %s): %s', current_ms, obis)

        if address == '1.8.0':
            self._gauge.set_positive_active_energy_total(
                current_ms, obis.value)
            self._last_sane_value = current_ms
        elif address == '2.8.0':
            self._gauge.set_negative_active_energy_total(
                current_ms, obis.value)
            self._last_sane_value = current_ms

    def is_time_to_publish(self):
        """
        Publish every 120s or more often when there are significant changes
        """
        try:
            self._last_publish
        except AttributeError:
            # No last_publish, so we'll wait for the first significant
            # change.
            tdelta_s = 30
        else:
            tdelta_s = int(time.time() - self._last_publish)

        inst_pwr = self._gauge.get_instantaneous_power()
        return (
            # every 120 seconds
            tdelta_s >= 120 or
            # power is higher than 400: then we have more detail
            (tdelta_s >= 60 and abs(inst_pwr) >= 400) or
            # we have more detail, and time is larger than 25
            (tdelta_s >= 25 and self._gauge.has_significant_change()))

    def try_publish(self):
        """
        Called after every run; allows us to quickly push changes
        """
        if False:
            log.debug(
                'CURRENT: 1.8.0 - %s',
                self._gauge.get_positive_active_energy_total())
            log.debug(
                'CURRENT: 2.8.0 - %s',
                self._gauge.get_negative_active_energy_total())
            log.debug(
                'CURRENT: 16.7.0 - %s (has_change %s)',
                self._gauge.get_instantaneous_power(),
                self._gauge.has_significant_change())

        if self.is_time_to_publish():
            pos_act = self._gauge.get_positive_active_energy_total()
            neg_act = self._gauge.get_negative_active_energy_total()
            inst_pwr = self._gauge.get_instantaneous_power()
            inst_pwr = DecimalWithUnit.with_unit(inst_pwr, 'W')

            assert isinstance(pos_act, DecimalWithUnit), (
                type(pos_act), pos_act)
            assert isinstance(neg_act, DecimalWithUnit), (
                type(neg_act), neg_act)
            assert isinstance(inst_pwr, DecimalWithUnit), (
                type(inst_pwr), inst_pwr)

            # Go go go.
            if self._publisher:
                self._publisher.publish(pos_act, neg_act, inst_pwr)
            else:
                log.info(
                    f'Time to publish: 1.8.0 {pos_act}, 2.8.0 {neg_act}, '
                    f'16.7.0 {inst_pwr}')

            self._last_publish = time.time()
            self._gauge.reset()


Obis = namedtuple('Obis', 'code description')

OBIS_MAP = dict((v.code, v) for v in (
    # Administrative values:

    # We read these two in a loop for totals and to calculate
    # approximations for 1.7.0 and 2.7.0:
    Obis('1.8.0', 'Positive active energy (A+) total [Wh]'),
    Obis('2.8.0', 'Negative active energy (A+) total [Wh]'),

    # Available in ME-162, but not that useful to us:
    Obis('1.8.1', 'Positive active energy (A+) in tariff T1 [Wh]'),
    Obis('1.8.2', 'Positive active energy (A+) in tariff T2 [Wh]'),
    Obis('1.8.3', 'Positive active energy (A+) in tariff T3 [Wh]'),
    Obis('1.8.4', 'Positive active energy (A+) in tariff T4 [Wh]'),
    Obis('2.8.1', 'Negative active energy (A+) in tariff T1 [Wh]'),
    # ...
    Obis('15.8.0', 'Total absolute active energy (= 1_8_0 + 2_8_0)')

    # Alas, not in ME-162 (returns (ERROR) when queried):
    # Obis('1.7.0', 'Positive active instantaneous power (A+) [W]'),
    # Obis('2.7.0', 'Negative active instantaneous power (A+) [W]'),
    # Obis('16.7.0', 'Sum active instantaneous power [W] (=1_7_0-2_7_0)'),
    # Obis('16.8.0', 'Sum of active energy without blockade (=1_8_0-2_8_0)'),
))


class State:
    class MODE(Enum):
        DATA_READOUT = 1
        PROGRAMMING_MODE = 2

    class IO(Enum):
        W_BREAK = 0
        W_LOGIN = 1
        R_IDENT = 2
        W_REQ_DATA_MODE = 3
        R_DATA_READOUT = 4
        W_REQ_PROG_MODE = 8
        R_ACK_PROG_MODE = 9
        W_REQ_OBIS = 10
        R_READ_OBIS = 11
        TRY_PUBLISH = 253
        SLEEP = 254
        END = 255

    def __init__(self):
        self.mode = self.MODE.DATA_READOUT
        self.io = self.IO.W_BREAK
        self._obis_idx = 0
        self._obis_requests = ['1.8.0', '2.8.0']

    @property
    def obis_request(self):
        return self._obis_requests[self._obis_idx]

    def obis_has_next(self):
        return bool(self._obis_idx + 1 < len(self._obis_requests))

    def obis_set_next(self):
        self._obis_idx += 1
        assert self._obis_idx < len(self._obis_requests)

    def obis_reset(self):
        self._obis_idx = 0

    def __repr__(self):
        return '{}:{}'.format(self.mode.name, self.io.name)


class Iec6205621CClient:
    def __init__(self, devname, processor):
        self._devname = devname
        self._reader = self._writer = None
        self._processor = processor

    async def open(self):
        try:
            # Hardware UART should cope with bytesize=7 and parity.
            # Start with 9600 baud because our peer might still be in the
            # upgraded state.
            reader, writer = await serial_asyncio.open_serial_connection(
                url=self._devname, baudrate=9600, bytesize=7,
                parity=serial.PARITY_EVEN, stopbits=1, exclusive=True)
        except termios.error:
            # Software openpty bridge does not copy with bytesize=7 and
            # parity.
            log.info('Detected non-UART (connected to software serial bridge)')
            reader, writer = await serial_asyncio.open_serial_connection(
                url=self._devname, baudrate=9600, bytesize=8,
                parity=serial.PARITY_NONE, stopbits=1)

        self._reader = reader
        self._writer = writer

    def close(self):
        log.debug('(Iec6205621CClient.close)')
        # close() to signal to the other side that we're done/gone. Useful
        # for software/PTY bridge.
        self._writer.close()
        # self._reader has no close()
        self._writer = self._reader = None

    async def run(self):
        state = State()
        while await self.loop(state):
            pass

    async def send(self, msg, state):
        log.debug(f'{state}: sleep 0.020 (pre-send)')
        await asyncio.sleep(0.02)

        if not isinstance(msg, (bytes, bytearray)):
            msg = msg.encode('ascii')
        log.debug(f'{state}: send {bytes(msg)}')
        self._writer.write(msg)  # actually synchronous!

        # Sleep a short while. This is useful when testing against the
        # SerialProxy. The drain functions otherwise don't appear to act
        # fast enough.
        sleep_time = (
            # 10 bits per byte, divided by baud rate.
            len(msg) * 10.0 / self._writer.transport._serial.baudrate)
        log.debug(f'{state}: sleep {sleep_time:.3}')
        await asyncio.sleep(sleep_time)

    async def recv_text(self, buf, state):
        "Fill buf with text (delimited by CRLF)"
        while buf[-2:] != b'\r\n':
            msg = await self._reader.read(1)
            buf += msg
        log.debug(f'{state}: recv {bytes(buf)}')

    async def recv_datamessage(self, buf, state):
        "Full buf with datamessage (ended by ETX/EOT + bcc), or empty on NAK"
        byte = await self._reader.read(1)
        log.debug(f'{state}: first byte')
        if byte == NAK:
            return  # keep buf empty

        buf += byte
        while buf[-2:-1] not in (ETX, EOT):
            byte = await self._reader.read(1)
            buf += byte

        log.debug(f'{state}: recv {bytes(buf)}')
        # Buf should now hold data including checksum.
        try:
            check_bcc(buf)
        except ValueError:
            # Ignore.. sad times.
            buf = b''
            assert False, 'en nu??'  # XXX: send NAK (=resend)
        else:
            if buf[-2] == EOT:
                assert False, 'unexpected EOT, should send NAK'

    async def loop(self, state):
        "Does a recv/send cycle (if applicable)"

        # Read/fill buffer. This does not need any timeout because we
        # have a dead mans switch.
        if state.io.name.startswith('R_'):
            buf = bytearray()
            if state.io == State.IO.R_IDENT:
                try:
                    await asyncio.wait_for(
                        self.recv_text(buf, state), timeout=5)
                except asyncio.TimeoutError:
                    log.error(
                        f'{state}: timeout in recv_text: {bytes(buf)}')
                    state.io = State.IO.W_LOGIN
                else:
                    assert buf[-2:] == b'\r\n', buf
            else:
                try:
                    await asyncio.wait_for(
                        self.recv_datamessage(buf, state), timeout=10)
                except asyncio.TimeoutError:
                    log.error(
                        f'{state}: timeout in recv_datamessage: {bytes(buf)}')
                    state.io = State.IO.W_REQ_OBIS
                else:
                    if not buf:
                        log.error(f'{state}: got NAK, back to W_REQ_OBIS')
                        state.io = State.IO.W_REQ_OBIS

        # Act upon state and buffer.
        if state.io == State.IO.W_BREAK:
            await self.send(f'{SOH}B0{ETX}q', state)
            self._writer.transport._serial.baudrate = 300
            state.io = State.IO.W_LOGIN

        elif state.io == State.IO.W_LOGIN:
            await self.send(f'/?!{CR}{LF}', state)
            state.io = State.IO.R_IDENT

        elif state.io == State.IO.R_IDENT:
            assert buf == b'/ISK5ME162-0033\r\n', buf
            if state.mode == State.MODE.DATA_READOUT:
                state.io = State.IO.W_REQ_DATA_MODE
            elif state.mode == State.MODE.PROGRAMMING_MODE:
                state.io = State.IO.W_REQ_PROG_MODE
            else:
                raise NotImplementedError(state)

        elif state.io == State.IO.W_REQ_DATA_MODE:
            await self.send(f'{ACK}050{CR}{LF}', state)
            self._writer.transport._serial.baudrate = 9600
            state.io = State.IO.R_DATA_READOUT

        elif state.io == State.IO.R_DATA_READOUT:
            datamessage = unpack_iec6205621_datamessage(buf)
            self._processor.set_readout(datamessage)

            # Don't just set the readout. Also fill all registers with the
            # values we got from the full readout.
            assert datamessage.endswith('\r\n!\r\n')
            for part in datamessage[0:-5].split('\r\n'):
                address, value, unit = parse_iec6205621_dataset(part)
                self._processor.set_register(address, value, unit)

            state.mode = State.MODE.PROGRAMMING_MODE
            state.io = State.IO.W_BREAK

        elif state.io == State.IO.W_REQ_PROG_MODE:
            await self.send(f'{ACK}051{CR}{LF}', state)
            self._writer.transport._serial.baudrate = 9600
            state.io = State.IO.R_ACK_PROG_MODE

        elif state.io == State.IO.R_ACK_PROG_MODE:
            assert buf == append_bcc(f'{SOH}P0{STX}(){ETX}'), buf
            state.io = State.IO.W_REQ_OBIS

        elif state.io == State.IO.W_REQ_OBIS:
            await self.send(
                append_bcc(f'{SOH}R1{STX}{state.obis_request}(){ETX}'), state)
            state.io = State.IO.R_READ_OBIS

        elif state.io == State.IO.R_READ_OBIS:
            dataset = unpack_iec6205621_datamessage(buf)
            address, value, unit = parse_iec6205621_dataset(dataset)
            assert address == '', dataset
            self._processor.set_register(state.obis_request, value, unit)
            if state.obis_has_next():
                state.obis_set_next()
                state.io = State.IO.W_REQ_OBIS
            else:
                state.io = State.IO.TRY_PUBLISH

        elif state.io == State.IO.TRY_PUBLISH:
            self._processor.try_publish()

            if self._processor.should_stop():
                state.io = State.IO.END
            else:
                state.io = State.IO.SLEEP

        elif state.io == State.IO.SLEEP:
            await asyncio.sleep(2)
            state.io = State.IO.W_REQ_OBIS
            state.obis_reset()

        elif state.io == State.IO.END:
            return False

        else:
            raise NotImplementedError(state)

        return True


class DeadMansSwitchTripped(Exception):
    pass


async def dead_mans_switch(processor):
    while True:
        tdelta = processor.ms_since_last_value()
        if tdelta >= 50000:
            raise DeadMansSwitchTripped(
                f'more than {tdelta} ms have passed without changes')
        await asyncio.sleep(1)


async def main(serial_dev, publisher_class=Pe32Me162Publisher):
    async def cancel_tasks(tasks):
        log.debug(f'Checking tasks {tasks!r}')
        for task in tasks:
            if task.done():
                log.debug(f'- task {task} was already done')
                continue
            try:
                log.debug(f'- task {task} to be cancelled')
                task.cancel()
                await task
            except asyncio.CancelledError:
                pass

    async with AsyncExitStack() as stack:
        # Keep track of the asyncio tasks that we create, so that
        # we can cancel them on exit
        tasks = set()
        stack.push_async_callback(cancel_tasks, tasks)

        publisher = publisher_class()
        await stack.enter_async_context(publisher.open())

        processor = IskraMe162ValueProcessor(publisher)

        # Create Iec6205621CClient client, open connection and push
        # shutdown code.
        iec62056_client = Iec6205621CClient(serial_dev, processor)
        await iec62056_client.open()
        stack.callback(iec62056_client.close)  # synchronous!

        # Start our two tasks.
        # XXX: do we need a publisher task here as well; one that can
        # die if there is something permanently wrong with the mqtt?
        tasks.add(asyncio.create_task(
            iec62056_client.run(), name='iec62056_client'))
        tasks.add(asyncio.create_task(
            dead_mans_switch(processor), name='dead_mans_switch'))

        # Execute tasks and handle exceptions.
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION)
        assert done
        for task in done:
            task.result()  # raises exceptions if any


if __name__ == '__main__':
    called_from_cli = (
        # Reading just JOURNAL_STREAM or INVOCATION_ID will not tell us
        # whether a user is looking at this, or whether output is passed to
        # systemd directly.
        any(os.isatty(i.fileno())
            for i in (sys.stdin, sys.stdout, sys.stderr)) or
        not os.environ.get('JOURNAL_STREAM'))
    sys.stdout.reconfigure(line_buffering=True)  # PYTHONUNBUFFERED, but better
    logging.basicConfig(
        level=(
            logging.DEBUG if os.environ.get('PE32ME162_DEBUG', '')
            else logging.INFO),
        format=(
            '%(asctime)s %(message)s' if called_from_cli
            else '%(message)s'),
        stream=sys.stdout,
        datefmt='%Y-%m-%d %H:%M:%S')

    print(f"pid {os.getpid()}: send SIGINT or SIGTERM to exit.")
    loop = asyncio.get_event_loop()
    if sys.argv[1:2]:
        main_coro = main(sys.argv[1])  # '/dev/ttyAMA0' or '/dev/serial0'
    else:
        main_coro = main('./iec62056_sample_server.dev')
    loop.run_until_complete(main_coro)
    loop.close()
    print('end of main')
