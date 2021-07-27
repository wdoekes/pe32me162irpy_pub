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


def send():
    global state
    log('send ({})'.format(state))
    if state == State.STATE_NONE:
        iskra_tx("\x01B0\x03q")
        me162.baudrate = 300
        state = State.STATE_WR_LOGIN
        loop = asyncio.get_running_loop()
        loop.call_soon(send)

    elif state == State.STATE_WR_LOGIN:
        iskra_tx("/?!\r\n")
        state = State.STATE_RD_IDENTIFICATION

    elif state == State.STATE_WR_REQ_DATA_MODE:
        iskra_tx("\x06050\r\n")
        me162.baudrate = 9600
        state = State.STATE_RD_DATA_READOUT

    else:
        print('state done')
        loop = asyncio.get_running_loop()
        loop.stop()


def read():
    global state
    log('read ({})'.format(state))
    text = b''

    if state == State.STATE_RD_IDENTIFICATION:
        while text[-2:] != b'\r\n':
            msg = me162.read()
            if msg == '\x00':
                log('got 0x00')  # problem? no?
            text += msg
    elif state == State.STATE_RD_DATA_READOUT:
        while text[-6:-1] != b'\r\n!\r\n':
            msg = me162.read()
            if msg == '\x00':
                log('got 0x00')  # problem? no?
            text += msg

    log('got data: {}'.format(text))
    loop = asyncio.get_running_loop()

    if state == State.STATE_RD_IDENTIFICATION:
        state = State.STATE_WR_REQ_DATA_MODE
        loop.call_soon(send)
    elif state == State.STATE_RD_DATA_READOUT:
        state = State.STATE_END


def iskra_tx(msg):
    log('sending {!r}'.format(msg))
    me162.write(msg.encode('ascii'))

    # Sleep a short while. This is useful when testing against the
    # SerialProxy. The drain functions otherwise don't appear to act
    # fast enough.
    sleep_time = (1.0 / me162.baudrate * 10 * len(msg))
    time.sleep(sleep_time)

    log('sent    {!r}'.format(msg))


async def main():
    try:
        read()
        send()
        read()
        send()
        read()
        send()
        read()
        print('done reading/writing')
    finally:
        me162.close()


# TODO: yuck, globals, etc..
print(f"pid {os.getpid()}: send SIGINT or SIGTERM to exit.")
state = State.STATE_NONE
me162 = serial.Serial(
    DEVNAME, baudrate=9600, bytesize=7, parity=serial.PARITY_EVEN, stopbits=1)
print(me162)
asyncio.run(main())
print('end of main')
