#!/usr/bin/env python3
import asyncio
import serial
from enum import Enum
from datetime import datetime
import os
import time


DEVNAME = '/dev/ttyAMA0'
DEVNAME = './iec62056_sample_server.sock'


class State(Enum):
    STATE_NONE = 0
    STATE_WR_LOGIN = 1
    STATE_RD_IDENTIFICATION = 2
    STATE_WR_REQ_DATA_MODE = 3
    STATE_RD_DATA_READOUT = 4
    STATE_END = 255


def log(msg):
    print('{}: {}'.format(datetime.now(), msg))


class Client:
    def __init__(self, me162, state=State.STATE_NONE):
        self.me162 = me162
        self.state = state

    def send(self):
        log('send ({})'.format(self.state))
        if self.state == State.STATE_NONE:
            self.iskra_tx("\x01B0\x03q")
            self.me162.baudrate = 300
            self.state = State.STATE_WR_LOGIN
            loop = asyncio.get_running_loop()
            loop.call_soon(self.send)

        elif self.state == State.STATE_WR_LOGIN:
            self.iskra_tx("/?!\r\n")
            self.state = State.STATE_RD_IDENTIFICATION

        elif self.state == State.STATE_WR_REQ_DATA_MODE:
            self.iskra_tx("\x06050\r\n")
            self.me162.baudrate = 9600
            self.state = State.STATE_RD_DATA_READOUT

        else:
            print('state done')
            loop = asyncio.get_running_loop()
            loop.stop()

    def read(self):
        log('read ({})'.format(self.state))
        text = b''

        if self.state == State.STATE_RD_IDENTIFICATION:
            while text[-2:] != b'\r\n':
                msg = self.me162.read()
                if msg == '\x00':
                    log('got 0x00')  # problem? no?
                text += msg
        elif self.state == State.STATE_RD_DATA_READOUT:
            while text[-6:-1] != b'\r\n!\r\n':
                msg = self.me162.read()
                if msg == '\x00':
                    log('got 0x00')  # problem? no?
                text += msg

        log('got data: {}'.format(text))
        loop = asyncio.get_running_loop()

        if self.state == State.STATE_RD_IDENTIFICATION:
            self.state = State.STATE_WR_REQ_DATA_MODE
            loop.call_soon(self.send)
        elif self.state == State.STATE_RD_DATA_READOUT:
            self.state = State.STATE_END

    def iskra_tx(self, msg):
        log('sending {!r}'.format(msg))
        self.me162.write(msg.encode('ascii'))

        # Sleep a short while. This is useful when testing against the
        # SerialProxy. The drain functions otherwise don't appear to act
        # fast enough.
        sleep_time = (1.0 / self.me162.baudrate * 10 * len(msg))
        time.sleep(sleep_time)

        log('sent    {!r}'.format(msg))

    def close(self):
        self.me162.close()


async def async_main(client):
    try:
        client.read()
        client.send()
        client.read()
        client.send()
        client.read()
        client.send()
        client.read()
        print('done reading/writing')
    finally:
        client.close()


def main():
    print(f"pid {os.getpid()}: send SIGINT or SIGTERM to exit.")
    state = State.STATE_NONE
    me162 = serial.Serial(
        DEVNAME, baudrate=9600, bytesize=7, parity=serial.PARITY_EVEN,
        stopbits=1)
    client = Client(me162, state)
    print(client.me162)
    asyncio.run(async_main(client))
    print('end of main')


if __name__ == '__main__':
    main()
