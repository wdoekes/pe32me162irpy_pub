#!/usr/bin/env python3
import os
import serial
import signal

from serialproxy import spawn_serialproxy_child


class IEC62056dash21ProtoModeCServer:
    """
    IEC62056-21 mode C Server

    This is not async because the serial_asyncio class failed miserably
    when setting timeout=0 (non-blocking) when connected to our custom
    SerialProxy.

    The interesting thing about this server is that it switches baudrate
    if the right handshake is done.
    """
    STATE_NONE = 0
    STATE_RD_LOGIN = 1
    STATE_WR_IDENTIFICATION = 2
    STATE_RD_REQ_MODE = 3
    STATE_RD_REQ_DATA_MODE = 4
    STATE_RD_REQ_PROG_MODE = 5
    STATE_WR_DATA_READOUT = 5

    def __init__(self, devname):
        self._devname = devname

    def sync_run(self):
        self._ser = serial.Serial(
            port=self._devname,
            baudrate=300, bytesize=7,
            parity=serial.PARITY_EVEN, stopbits=1,
            exclusive=True)
        self._loop()

    def _loop(self):
        state, buf = self.STATE_NONE, None

        while True:
            if state == self.STATE_NONE:
                state, buf = self._state_none()
            elif state == self.STATE_RD_LOGIN:
                state, buf = self._state_rd_login(buf)
            elif state == self.STATE_WR_IDENTIFICATION:
                state, buf = self._state_wr_identification()
            elif state == self.STATE_RD_REQ_MODE:
                state, buf = self._state_rd_req_mode(buf)
            elif state == self.STATE_RD_REQ_DATA_MODE:
                state, buf = self._state_rd_req_data_mode()
            elif state == self.STATE_RD_REQ_PROG_MODE:
                state, buf = self._state_rd_req_prog_mode()

    def _state_none(self):
        buf = bytearray()
        while True:
            byte = self._ser.read(1)
            if byte == '':
                raise Exception('STOP')

            buf.extend(byte)
            if buf.endswith(b'/?!\r\n'):
                # assert (
                #     self._proxydevice is None or
                #     self._proxydevice.pty1.baud
                return self.STATE_RD_LOGIN, buf[-5:]
            elif buf.endswith(b'/?1!\r\n'):
                # assert (
                #     self._proxydevice is None or
                #     self._proxydevice.pty1.baud
                return self.STATE_RD_LOGIN, buf[-6:]

    def _state_rd_login(self, buf):
        print('STATE_RD_LOGIN', buf)
        return self.STATE_WR_IDENTIFICATION, None

    def _state_wr_identification(self):
        print('STATE_WR_IDENTIFICATION', b'/ISK5ME162-0033\r\n')
        self._ser.write(b'/ISK5ME162-0033\r\n')
        return self.STATE_RD_REQ_MODE, None

    def _state_rd_req_mode(self, buf):
        buf = bytearray()
        while True:
            byte = self._ser.read(1)
            if byte == '':
                raise Exception('STOP')

            buf.extend(byte)
            if buf.endswith(b'\r\n'):
                break

        print('STATE_RD_REQ_MODE', buf)
        if buf == b'\x06050\r\n':
            self._ser.baudrate = 9600
            return self.STATE_RD_REQ_DATA_MODE, buf
        elif buf == b'\x06150\r\b':
            return self.STATE_RD_REQ_PROG_MODE, buf
        else:
            # ... if timeout too
            self._ser.baudrate = 300
            return self.STATE_NONE, None

    def _state_rd_req_data_mode(self):
        self._ser.write(
            b'\x02C.1.0(12345678)\r\n'
            b'0.0.0(47983850)\r\n'
            b'1.8.0(0034204.753*kWh)\r\n'
            b'1.8.1(0000000.000*kWh)\r\n'
            b'1.8.2(0034204.753*kWh)\r\n'
            b'2.8.0(0001516.488*kWh)\r\n'
            b'2.8.1(0000000.000*kWh)\r\n'
            b'2.8.2(0001516.488*kWh)\r\n'
            b'F.F(0000000)\r\n!\r\n\xfe')

        # TODO: now we wait, flush, go back to 300 baud..
        return self.STATE_NONE, None

    def close(self):
        self._ser.close()


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

    devname = '{}.sock'.format(__file__.rsplit('.', 1)[0])
    proxy_child, proxy_devname = spawn_serialproxy_child(devname)
    print('Parent', os.getpid(), 'connects to', proxy_devname)

    server = IEC62056dash21ProtoModeCServer(proxy_devname)
    try:
        # No asyncio for the serial.Serial() stuff. It has trouble
        # playing nicely with our opentty using SerialProxy.
        # > tcsetattr: termios.error: (22, 'Invalid argument')
        server.sync_run()
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
    main()
