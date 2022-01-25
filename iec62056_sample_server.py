#!/usr/bin/env python3
import os
import re
import select
import serial
import signal
import termios
import time

from ctrlcode import ACK, CR, EOT, ETX, LF, NAK, SOH, STX
from din66219 import append_bcc, check_bcc
from serialproxy import spawn_serialproxy_child


class Dataset:
    def __init__(self, address, value, unit=None):
        self.address = address
        self.value = value
        self.unit = unit

    def as_dataline(self, with_address):
        if self.unit:
            value = f'{self.value}*{self.unit}'
        else:
            value = self.value
        if with_address:
            ret = f'{self.address}({value})'
        else:
            ret = f'({value})'
        return ret


class BaseDataProvider:
    def __init__(self):
        self.addresses = {
            'C.1.0': self.get_meter_serial,
            '0.0.0': self.get_device_address,
            'F.F': '0000000',
        }

    def get_data_readout_addresses(self):
        return ['C.1.0', '0.0.0', 'F.F']

    def get_meter_serial(self):
        return '12345678'

    def get_device_address(self):
        return '44455566'

    def get_dataset(self, address):
        "Return Dataset with address, value and possibly unit"
        try:
            value_with_optional_unit = self.get_value(address)
        except KeyError:
            return Dataset(address, 'ERROR')
        if isinstance(value_with_optional_unit, tuple):
            value, unit = value_with_optional_unit
            return Dataset(address, value, unit)
        return Dataset(address, value_with_optional_unit)

    def get_value(self, address):
        "Return value or 2-tuple of (value, unit)"
        value = self.addresses[address]
        if callable(value):
            value = value()
        return value


class ResetState:
    def __init__(self, next_state):
        self.next_state = next_state

    def __repr__(self):
        return '<ResetState(->{})'.format(self.next_state)


class ReadState:
    def __init__(self, action):
        self.action = action

    def __repr__(self):
        return '<ReadState(->{})'.format(self.action.__name__)


class WriteState:
    def __init__(self, data, next_state):
        if isinstance(data, str):
            data = data.encode('ascii')
        self.data = data
        self.next_state = next_state

    def __repr__(self):
        data = self.data
        if len(data) > 10:
            data = data[0:10] + b'...'
        return '<WriteState({!r}, ->{!r})'.format(data, self.next_state)


class IEC62056dash21ProtoModeCServer:
    """
    IEC62056-21 mode C Server

    NOTE: This is not async because the serial_asyncio class failed
    miserably when setting timeout=0 (non-blocking) when connected to
    our custom SerialProxy.

    An interesting thing about this server is that it switches baudrate
    from 300 to 9600 when the right handshake is done.

    General setup:

      << "/?" ADDR "!" CR LF
      >> "/" ID CR LF
      << ACK NUM CR LF

    Data readout mode:

      >> STX DATA "!" CR LF ETX BCC

      << SOH "B0" ETX BCC (or NAK)

    Programming mode:

      >> SOH "P0" STX "()" ETX BCC

      << SOH "R1" STX "1.8.0()" ETX BCC
      >> STX "(0033402.264*kWh)" ETX BCC

      << SOH "B0" ETX BCC (or NAK)

      >> ACK (or NAK)
    """
    def __init__(self, dataprovider, devname):
        self._dataprovider = dataprovider
        self._devname = devname
        self._baudrate = 300
        self._serial = serial.Serial(
            port=self._devname, baudrate=self._baudrate,
            exclusive=True)
        try:
            # These aren't set in the constructor, but afterwards
            # because of a gem in tcsetattr(). It will only report error
            # if all values cannot be changed. So we set them
            # individually.
            self._serial.bytesize = 7
            self._serial.parity = serial.PARITY_EVEN
            self._serial.stopbits = 1
        except termios.error:
            # Apparently we're not talking to a proper UART that can
            # deal with 7-bit bytes or parity. Likely this is an
            # openpty() serial bridge. That's okay.
            print('NOTICE: connected to software instead of hardware')

        # Set up states:
        self.STATE_RECV_REQUEST_MESSAGE = ReadState(
            self.read_request_message)
        self.STATE_RECV_ACK_OPT_SELECT = ReadState(
            self.read_ack_opt_select)
        self.STATE_RECV_CMD_IN_DATA_READOUT = ReadState(
            self.read_cmd_in_data_readout)
        self.STATE_RECV_CMD_IN_PROGRAMMING = ReadState(
            self.read_cmd_in_programming)

        # Initialize state. (STATE_0 will possibly make you wait after
        # startup.)
        self.STATE_0 = ResetState(self.STATE_RECV_REQUEST_MESSAGE)
        self._state = self.STATE_RECV_REQUEST_MESSAGE
        self._set_state(self.STATE_RECV_REQUEST_MESSAGE)

    def _set_baud(self, new_baud):
        if self._baudrate != new_baud:
            self._baudrate = new_baud
            self._serial.baudrate = new_baud

    def _set_state(self, new_state):
        if new_state == self.STATE_RECV_REQUEST_MESSAGE:
            self._set_baud(300)
        if new_state != self._state:
            print('state:', self._state, '->', new_state)
            self._state = new_state
        # XXX: last transition time?

    def read_request_message(self, mutable_buf):
        """
        Parse "/?" DEVICE_ADDR "!" CR LF; ignore anything else

        > Device address, optional field, manufacturer-specific, 32
        > characters maximum. The characters can be digits (0...9),
        > upper-case letters (A...Z), or lower case letters (a...z), or a
        > space ( ). Upper and lower case letters, and the space character
        > are unique*. Leading zeros shall not be evaluated. This means
        > that all leading zeros in the transmitted address are ignored
        > and all leading zeros in the tariff device address are ignored
        > (i.e. 10203 = 010203 = 000010203).
        """
        m = re.search(
            b'/?(?P<device_address>[A-Za-z0-9 ]*)!\r\n$', mutable_buf)
        if not m:
            # Trim the buffer to 5+32 characters. So we can ignore
            # previous garbage.
            if len(mutable_buf) > 37:
                print('(trimming)', mutable_buf)
                mutable_buf[0:-37] = bytearray()
            return None

        # Must fetch contents of groupdict() before mutating mutable_buf!
        params = dict((k, v.decode('ascii')) for k, v in m.groupdict().items())
        print('<<', mutable_buf[0:m.endpos])
        mutable_buf[0:m.endpos] = bytearray()

        # When both the transmitted address and the tariff device
        # address contain only zeros, regardless of their respective
        # lengths, the addresses are considered equivalent.
        if len(params['device_address']):
            params['device_address'] = '{}{}'.format(
                params['device_address'][0:-1].lstrip('0'),
                params['device_address'][-1])

        if self.on_request_message(**params):
            return WriteState(
                self.build_identification_message(),
                self.STATE_RECV_ACK_OPT_SELECT)
        else:
            # XXX?
            pass

    def read_ack_opt_select(self, mutable_buf):
        """
        Parse ACK V Z Y CR LF, or return to state 0

        > Usage of protocol control character "V" in protocol
        > mode C and E (item 10 in 6.3.3)

        > The communication will proceed at 300 Bd (initial baud rate) if:
        > - the "Z" character in the acknowledgement/option select
        >   message is 0; or
        > - an incorrect or unsupported acknowledgement/option select
        >   message is sent or received; or
        > - no acknowledgement/option select message is sent or received.

        > Usage of mode control character "Y" in protocol
        > modes C and E (item 11 in 6.3.3)
        > 0 - data readout;
        > 1 - programming mode;
        > 2 - binary mode (HDLC), see Annex E.
        """
        if len(mutable_buf) < 6:
            return None
        m = re.match(
            (f'{ACK}(?P<opt_v>\\d)(?P<opt_z>\\d)(?P<opt_y>\\d)\\r\\n'
             .encode('ascii')),
            mutable_buf)
        if not m:
            # FIXME: log error/mismatch
            return self.STATE_0

        # Must fetch contents of groupdict() before mutating mutable_buf!
        params = dict((k, v.decode('ascii')) for k, v in m.groupdict().items())
        print('<<', mutable_buf[0:m.endpos])
        mutable_buf[0:m.endpos] = bytearray()

        return self.on_ack_opt_select(**params)

    def read_cmd_in_data_readout(self, mutable_buf):
        """
        Wait for break or NAK in protocol-mode-C

        << SOH "B0" ETX "q" (break/exit)
        or
        << NAK (repeat request, not entirely sure if allowed)

        > Termination occurs following SOH B0 ETX BCC (without NAK
        > response), or by timeout.
        [...]
        > NOTE 1: The inactivity time-out period for the tariff device is
        > 60 s to 120 s after which the operation moves from any point
        > to the start.
        > NOTE 2: A break message can be issued at any point. Operation
        > then moves to the start after finishing the current operation.
        [...]
        > The HHU can transmit a repeat request if the transmission was
        > faulty.

        > The time between the reception of a message and the
        > transmission of an answer is:
        >   (20 ms) 200 ms ≤ t r ≤ 1 500 ms (see item 12) of 6.3.14).
        > If a response has not been received, the waiting time of the
        > transmitting equipment after transmission of the identification
        > message, before it continues with the transmission, is:
        >   1 500 ms < t t ≤ 2 200 ms
        > The time between two characters in a character sequence is:
        >   t[a] < 1500 ms
        """
        if len(mutable_buf) == 1 and mutable_buf[0] == NAK:
            # "Repeat request"
            raise NotImplementedError('should repeat last write')
        elif mutable_buf == append_bcc(f'{SOH}B0{ETX}'):
            print('<<', mutable_buf)
            mutable_buf[:] = bytearray()
            return self.STATE_0

        if len(mutable_buf) > 5:
            mutable_buf[0:-5] = bytearray()
        return None

    def read_cmd_in_programming(self, mutable_buf):
        """
        XXX
        """
        if mutable_buf[0] == NAK:
            # "Repeat request"
            raise NotImplementedError('should repeat last write')

        # Because in programming mode, failures result in a return to
        # this state, we'll drop non-SOH from the start.
        while mutable_buf and mutable_buf[0] != SOH:
            # Drop characters.
            print('(dropping leading)', mutable_buf)
            mutable_buf[0:1] = bytearray()
            return None

        if not mutable_buf:
            return None

        assert mutable_buf[0] == SOH, mutable_buf
        m = re.search(
            f'({SOH}[^{ETX}{EOT}]+[{ETX}{EOT}].)'.encode('ascii'), mutable_buf)
        if not m:
            # XXX: trim?
            return None

        result_buf = m.groups()[0]
        print('<<', mutable_buf[0:m.endpos])
        mutable_buf[0:m.endpos] = bytearray()

        try:
            check_bcc(result_buf)
        except ValueError:
            return WriteState(NAK, self.STATE_RECV_CMD_IN_PROGRAMMING)

        if result_buf[1:3] == b'B0' and len(result_buf) == 5:
            # {SOH}B0{ETX}
            return self.STATE_0
        elif result_buf[1:3] == b'R1' and result_buf[3] == STX:
            read_dataset = result_buf[4:-2]
            if read_dataset.endswith(b'()'):
                address = read_dataset[0:-2].decode()
                return WriteState(
                    self.build_prog_r1(address),
                    self.STATE_RECV_CMD_IN_PROGRAMMING)
            else:
                assert False, result_buf
        else:
            assert False, result_buf

        if len(mutable_buf) > 32:
            mutable_buf[0:-32] = bytearray()
        return None

    def on_request_message(self, device_address):
        print('on_request_message: address {!r}'.format(device_address))
        return True  # accept any device_address?

    def on_ack_opt_select(self, opt_v, opt_z, opt_y):
        print(
            'on_ack_opt_select: protoctrl={!r}, baudid={!r}, modectrl={!r}'
            .format(opt_v, opt_z, opt_y))
        # Protocol mode
        new_protocol = {
            0: 'N',  # normal protocol procedure
        }.get(int(opt_v))

        # Baud rate changeover
        new_baud = {
            0: 300,
            1: 600,
            2: 1200,
            3: 2400,
            4: 4800,
            5: 9600,
            6: 19200,
        }.get(int(opt_z))

        # Mode
        new_mode = {
            0: 'D',  # data readout
            1: 'P',  # programming
        }.get(int(opt_y))

        if (new_protocol is None or new_baud is None or
                opt_z != self.build_identification_message()[4] or
                new_mode is None):
            # FIXME: log error/mismatch
            assert False, 'return -> statefail -> 300 data readout?'

        # Accepted, switch baud
        self._set_baud(new_baud)

        if new_mode == 'P':
            return WriteState(
                append_bcc(f'{SOH}P0{STX}(){ETX}'),
                self.STATE_RECV_CMD_IN_PROGRAMMING)

        assert new_mode == 'D', new_mode
        return WriteState(
            self.build_data_readout(), self.STATE_RECV_CMD_IN_DATA_READOUT)

    def build_identification_message(self):
        """
        "/" X X X Z IDENT CR LF

        For "/ISK5ME162-0033": "ISK" is the manufacturer identification,
        the 5 is the allowed baud rate changeover option, "ME162-0033"
        is the (16 character max.) identifier.

        XXX:
        > Manufacturer's identification comprising three upper case
        > letters except as noted below: If a tariff device transmits the
        > third letter in lower case, the minimum reaction time t[r] for
        > the device is 20 ms instead of 200 ms. Even though a tariff
        > device transmits an upper case third letter, this does not
        > preclude supporting a 20 ms reaction time.

        Z:
        > The communication will only switch to Z baud if the Z
        > characters in the identification response and the
        > acknowledgement/option select message are identical.
        > 0 -   300 Bd
        > 1 -   600 Bd
        > 2 -  1200 Bd
        > 3 -  2400 Bd
        > 4 -  4800 Bd
        > 5 -  9600 Bd
        > 6 - 19200 Bd

        IDENT:
        > Identification, manufacturer-specific, 16 printable characters
        > maximum ("/" and "!" not allowed, and "\\" only allowed for
        > enhanced baud stuff).
        """
        return '/ISK5ME162-0033\r\n'

    def build_break(self):
        """
        Break command

        Details of how the command is formed:

        > Command message identifier
        > P - Password command
        > W - Write command
        > R - Read command
        > E - Execute command
        > B - Exit command (break)

        > for password P command
        > 0 data is operand for secure algorithm
        > 1 data is operand for comparison with internally held password
        > 2 data is result of secure algorithm (manufacturer-specific)

        > for password W command
        > 1 - write ASCII-coded data
        > 2 - formatted communication coding method write (optional)
        > 3 - write ASCII-coded with partial block (optional)
        > 4 - formatted communication coding method write (optional)
        > with partial block

        > for read R command
        > 1 - read ASCII-coded data
        > 2 - formatted communication coding method read (optional)
        > 3 - read ASCII-coded with partial block (optional)
        > 4 - formatted communication coding method read (optional)
        >     with partial block

        > for execute E command
        > 2 - formatted communication coding method execute (optional)

        > for exit B command
        > 0 - complete sign-off
        > 1 - complete sign-off for battery operated devices using the
        >     fast wake-up method
        """
        return append_bcc(f'{SOH}B0{ETX}')

    def build_error(self):
        """
        Example error message

        > This consists of 32 printable characters maximum with exception
        > of (, ), *, / and !. It is bounded by front and rear boundary
        > characters, as in the data set structure.
        """
        return append_bcc(f'{STX}(ERROR){ETX}')

    def build_data_readout(self):
        """
        Build data readout reply taken from the supplied from the DataProvider.

        > bcc := (xor of all characters except the first SOH or STX)
        > datamessage ::= STX datablock "!" CR LF ETX bcc
        > datablock ::= ( dataline CR LF )*
        > dataline ::= ( dataset )+
        > dataset ::= address? "(" value? ( "*" unit )? ")"
        > address ::= (max 16 chars, except for "()/!")
        > value ::= (max 128 chars, except for "()/!*"; decimals use 1 period)
        > unit ::= (max 16 chars , except for "()/!")
        > NOTE: A data line should be not longer than 78 characters,
        > including all boundary, separating and control characters.
        > NOTE: In programming mode, a datamessage is:
        > datamessage ::= STX dataset ETX bcc
        > programming_command ::= SOH cmd_id cmd_type ( STX dataset )? ETX bcc
        > cmd_id ::= "P" | "W" | "R" | "E" | "B"
        > cmd_type ::= [0-9]
        > NOTE: For "B0" (break/exit), there is no dataset.
        > NOTE: For "R1" (read) command, the optional value may be the
        > number of locations to read. (E.g. start at 1.8.0 and read 4
        > locations.)
        """
        addresses = self._dataprovider.get_data_readout_addresses()
        datalines = [
            self._dataprovider.get_dataset(address).as_dataline(True)
            for address in addresses]
        datablock = ''.join(f'{dataline}{CR}{LF}' for dataline in datalines)
        return append_bcc(f'{STX}{datablock}!{CR}{LF}{ETX}')

    def build_prog_r1(self, address):
        """
        Build data R1 reply taken from the supplied from the DataProvider.

        For example reply {STX}(0033402.270*kWh){ETX} to a request like
        {SOH}R1{STX}1.8.0(){ETX} ("read ascii coded data from register
        1.8.0").
        """
        assert isinstance(address, str), address
        value = self._dataprovider.get_dataset(address).as_dataline(False)
        return append_bcc(f'{STX}{value}{ETX}')

    def serve(self):
        poll_read = select.poll()
        poll_read.register(
            self._serial.fileno(),
            select.POLLHUP | select.POLLIN | select.POLLERR)
        poll_write = select.poll()
        poll_write.register(
            self._serial.fileno(),
            select.POLLHUP | select.POLLOUT | select.POLLERR)

        readbuf = bytearray()  # reset at every state switch after write?

        while True:
            if isinstance(self._state, ReadState):
                evs = poll_read.poll(30)
                if evs and evs[0][1] == select.POLLIN:
                    readbuf.extend(self._serial.read(1))
                    # #print('<<', readbuf)
                    new_state = self._state.action(readbuf)
                    if new_state:
                        assert not isinstance(new_state, ReadState), new_state
                        self._set_state(new_state)
                elif evs and evs[0][1] & select.POLLHUP:
                    break
                elif evs:
                    assert False, f'read? 30s? HUP? ERR? {evs}'
            elif isinstance(self._state, WriteState):
                evs = poll_write.poll(30)
                if evs and evs[0][1] == select.POLLOUT:
                    # FIXME: when not everything is written... we do?
                    print('(waiting 200ms)')
                    time.sleep(0.2)
                    print('>>', self._state.data)
                    self._serial.write(self._state.data)
                    new_state = self._state.next_state
                    assert not isinstance(new_state, WriteState), new_state
                    self._set_state(new_state)
                elif evs and evs[0][1] & select.POLLHUP:
                    break
                elif evs:
                    assert False, f'write? 30s? HUP? ERR? {evs}'
            else:
                assert isinstance(self._state, ResetState), self._state
                # XXX: also wait/sleep?
                self._set_state(self._state.next_state)

    def close(self):
        self._serial.close()


class ExampleMe162DataProvider(BaseDataProvider):
    def __init__(self):
        super().__init__()
        self._182 = 34204753
        self._282 = 1516488

    def get_data_readout_addresses(self):
        return [
            'C.1.0', '0.0.0',
            '1.8.0', '1.8.1', '1.8.2', '2.8.0', '2.8.1', '2.8.2',
            'F.F']

    def get_dataset(self, address):
        # Hackery to increment the 1.8.[02] and 2.8.[02] values during
        # readout testing.
        if address == '1.8.0':
            self._bogus_increment()

        if address == '1.8.0':
            return self._make_kwh_dataset(address, 0 + self._182)
        elif address == '1.8.1':
            return self._make_kwh_dataset(address, 0)
        elif address == '1.8.2':
            return self._make_kwh_dataset(address, self._182)
        elif address == '2.8.0':
            return self._make_kwh_dataset(address, 0 + self._282)
        elif address == '2.8.1':
            return self._make_kwh_dataset(address, 0)
        elif address == '2.8.2':
            return self._make_kwh_dataset(address, self._282)
        return super().get_dataset(address)

    def _bogus_increment(self):
        self._182 += 10
        self._282 += 5

    def _make_kwh_dataset(self, address, watthour):
        return Dataset(address, '{:011.3f}'.format(watthour / 1000.0), 'kWh')


def main():
    """
    Start ExposedSerialProxy, start IEC62056dash21ProtoModeCServer and
    wait for either to complete.
    """
    class ChildExited(Exception):
        pass

    def sigchld(frame, signum):
        pid, status = os.wait()
        raise ChildExited(pid, status)

    signal.signal(signal.SIGCHLD, sigchld)

    use_proxy = True
    if use_proxy:
        peer_devname = '{}.dev'.format(__file__.rsplit('.', 1)[0])
        proxy_child, devname = spawn_serialproxy_child(peer_devname)
        print('Parent', os.getpid(), 'connects to', devname)
    else:
        proxy_child = None
        devname = '/dev/ttyAMA0'
        devname = '/dev/serial0'
        # socat -dd pty,rawer,link=server.dev pty,rawer,link=client.dev
        devname = './server.dev'

    server = IEC62056dash21ProtoModeCServer(
        ExampleMe162DataProvider(), devname)
    try:
        # No asyncio for the serial.Serial() stuff. It has trouble
        # playing nicely with our opentty using SerialProxy.
        # > tcsetattr: termios.error: (22, 'Invalid argument')
        server.serve()
    except ChildExited as e:
        print(f'Child exited with status {e.args[1]}')
        assert e.args[0] == proxy_child, e.args
        proxy_child = None
    except KeyboardInterrupt:
        print('Got SIGINT')
    finally:
        if proxy_child:
            print('Asking child nicely to stop...')
            os.kill(proxy_child, 2)  # SIGINT

        print('Stopping server...')
        server.close()


if __name__ == '__main__':
    assert append_bcc(f'{SOH}B0{ETX}') == b'\x01B0\x03q'
    main()
