# This behaves similar to:
#   socat PTY,link=./server.sock PTY,link=client.sock
# But it also simulates baudrate slowness and checks for baudrate
# compatibility.
import asyncio
import fcntl
import os
import select
import sys
import termios
import time
from collections import namedtuple
from contextlib import suppress
from warnings import warn


tcattr = namedtuple('tcattr', 'iflag oflag cflag lflag ispeed ospeed cc')

TCSPEED_TO_BAUDRATE = dict(
    # termios.B300: 300,
    # termios.B9600: 9600,
    (getattr(termios, i), int(i[1:]))
    for i in dir(termios) if i[0] == 'B' and i[1:].isdigit())


class HangupError(Exception):
    pass


class SerialPty:
    def __init__(self):
        # > How can I detect when someone opens the slave side of a pty
        # > (pseudo-terminal) in Linux?
        # > https://stackoverflow.com/questions/3486491/
        # > [...]
        # > However, there is a trick that allows you to do it. After
        # > opening the pseudo-terminal master (assumed here to be file
        # > descriptor ptm), you open and immediately close the slave side.
        # > [...]
        # > You now poll the HUP flag regularly with poll()
        # This is done in _detect_connect_and_hup().

        # TODO: can/should we do this manually using open('/dev/ptmx') instead?
        self.fd, self.worker_fd = os.openpty()
        self._ptsname = os.ttyname(self.worker_fd)  # do it before close!
        os.close(self.worker_fd)

        self._baudrate = None
        self._writebuf = []
        self._writelast = 0

        self._set_nonblock()

    @property
    def baudrate(self):
        "Baudrate detected on this pty"
        if self._baudrate is None:
            self._detect_baudrate()
            assert self._baudrate is not None
        return self._baudrate

    @property
    def bits_per_byte(self):
        "Assume 1start, 7data, 1parity, 1stop"
        return (1 + 7 + 1 + 1)

    @property
    def time_per_byte(self):
        "How much time/delay we emulate for a single byte for this baudrate"
        return (1.0 / self.baudrate * self.bits_per_byte)

    def _set_nonblock(self):
        "Set to non-blocking; standard for asyncio"
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def read_byte(self):
        "Read byte from fd directly"
        try:
            byte = os.read(self.fd, 1)
        except OSError as e:
            if e.args[0] == 5:  # EIO
                raise HangupError()
            elif e.args[0] == 11:  # EAGAIN/EWOULDBLOCK
                pass
            else:
                raise

        assert len(byte) == 1, byte
        self._detect_baudrate()  # update detected baud on read
        return byte

    def write_byte(self, byte, baudrate):
        "Schedule byte to be written as fast as baudrate permits"
        assert len(byte) == 1, byte
        self._writebuf.append((byte, baudrate))

        # Schedule a single byte to be written _only_ if this is the
        # first in the queue. In other cases the latest write will
        # schedule a new one.
        if len(self._writebuf) == 1:
            self._schedule_write_after_baudrate_delay()

    def close(self):
        "Clean up used fds"
        # When we close self.fd, self.worker_fd gets closed automatically.
        with suppress(OSError):
            # This should succeed, but if it doesn't we might as well
            # resume our cleanup.
            os.close(self.fd)

    def _detect_baudrate(self):
        "Called on read and on write"
        # TODO: can we / do we want to detect more? CS7? etc..?
        tc = tcattr(*termios.tcgetattr(self.fd))
        assert tc.ispeed == tc.ospeed, tc
        self._baudrate = TCSPEED_TO_BAUDRATE[tc.ispeed]

    def _schedule_write_after_baudrate_delay(self):
        "Schedule a write after baudrate delay"
        loop = asyncio.get_running_loop()
        tnext = self._writelast + self.time_per_byte
        tdelay = tnext - time.time()
        if tdelay <= 0:
            loop.call_soon(self._background_write)
        else:
            # #loop.call_soon(self._writer, fd)
            # #loop.call_at(tnext, self._writer, fd)
            loop.call_later(tdelay, self._background_write)

    def _background_write(self):
        "The actual write after the baudrate delay"
        assert self._writebuf, self

        self._detect_baudrate()  # update detected baud on write
        byte, peer_baudrate = self._writebuf.pop(0)

        # Compare baudrate with expected baudrate.
        if peer_baudrate != self.baudrate:
            # This is NOT always a problem. It might be if there are
            # many of these though.. (There's a slight going on when
            # (re)setting the baud rate.)
            print('(baudrate mismatch, forwarding {!r} from {} to {})'.format(
                byte, peer_baudrate, self.baudrate))

        # An EWOULDBLOCK/EAGAIN or any other error is unexpected here.
        count = os.write(self.fd, byte)
        assert count == 1, count
        self._writelast = time.time()

        # Schedule next write in a timely manner.
        if self._writebuf:
            self._schedule_write_after_baudrate_delay()

    def __repr__(self):
        buflen = len(self._writebuf)
        return f'<Pty(fd={self.fd}, baud={self.baudrate}, writebuf={buflen}>'


class SerialProxy:
    def __init__(self):
        # We create two PTYs. Then we can attach a server serial.Serial
        # to one end and a client serial.Serial to the other.
        self._pty1 = SerialPty()
        self._pty2 = SerialPty()
        # ^-- XXX: interestingly.. the order matters; see .adev and .bdev below
        #     as if the first openpty() and the second are aware of each other

    @property
    def adev(self):
        "Device name of the A (server) side"
        return self._pty1._ptsname

    @property
    def bdev(self):
        "Device name of the B (client) side"
        return self._pty2._ptsname

    async def run(self):
        "Start the serial proxy and run until one end disconnects"
        # Don't add writer tasks, as we have nothing to write; we'd just
        # busy-loop. Don't add reader tasks either. That's done by
        # _detect_connect_and_hup once both sockets are connected.
        await self._detect_connect_and_hup()

    async def _detect_connect_and_hup(self):
        """
        Poll the closed worker_fd for POLLHUP; they will un-HUP once
        they're connected. Then we add the readers. And shutdown once
        one side HUPs again.

        NOTE: Reusing this proxy is _not_ an option. Something in the
        serial.Serial tcsetattr() destroys the state so certain
        consecutive tcsetattr()s would EINVAL. So, once we go back to
        HUP state, this proxy has got to go.

        This effect is also observed when attempting to reconnect to a
        socat proxy:

            $ socat PTY,link=./server.sock PTY,link=iec62056_sample_server.sock
            $ ./iec62056_test_client.py
            (ok)
            $ ./iec62056_test_client.py
            ...
              File "serial/serialposix.py", line 435, in _reconfigure_port
                termios.tcsetattr(
            termios.error: (22, 'Invalid argument')
        """
        def hup_count(evs):
            hups = 0
            for fd, ev in evs:
                assert not (ev & (select.POLLERR | select.POLLNVAL)), evs
                if ev & select.POLLHUP:
                    hups += 1
            return hups

        # Add the two (disconnected) slave FDs to the poller.
        poller = select.poll()
        for pty in (self._pty1, self._pty2):
            poller.register(pty.worker_fd, (
                select.POLLIN | select.POLLHUP | select.POLLERR |
                select.POLLNVAL))

        # Is any fd still in hangup (HUP) state?
        while hup_count(poller.poll(0)) != 0:
            await asyncio.sleep(0.1)

        print('Both sides connected, starting readers')
        loop = asyncio.get_running_loop()
        loop.add_reader(
            self._pty1.fd, self._reader, self._pty1, self._pty2)
        loop.add_reader(
            self._pty2.fd, self._reader, self._pty2, self._pty1)

        try:
            # Are all fds still connected (not in hangup state)?
            while hup_count(poller.poll(0)) == 0:
                await asyncio.sleep(0.1)
        except asyncio.exceptions.CancelledError:
            # Task is stopped because we're shutting down. (loop.stop(),
            # possibly due to a fatal signal.)
            print('Poll task is cancelled, stopping all')
        else:
            print('One side closed the connection, stopping all')
        finally:
            # Stop reader tasks.
            loop.remove_reader(self._pty1.fd)
            loop.remove_reader(self._pty2.fd)

    def _reader(self, pty, peer_pty):
        "Reader gets called once there is a byte available"
        # We don't attempt to do multiple bytes at once. We assume that
        # the baudrate/speed is sufficiently low that we can task switch
        # easily. Remember: this is made for low baudrate tests.
        # XXX: test this? does this statement make sense?
        try:
            byte = pty.read_byte()
        except BaseException as e:
            loop = asyncio.get_running_loop()
            loop.remove_reader(pty.fd)
            loop.remove_reader(peer_pty.fd)
            if not isinstance(e, HangupError):
                raise
        else:
            peer_pty.write_byte(byte, pty.baudrate)

    def close(self):
        "Clean up the file descriptors"
        self._pty1.close()
        self._pty2.close()


class ExposedSerialProxy(SerialProxy):
    """
    ExposedSerialProxy exposes the B-device as a symlink

    Other than that, it's just the plain SerialProxy. Don't forget to
    call close(), even if you haven't connected yet.
    """
    def __init__(self, exposed_as):
        super().__init__()

        os.symlink(self.bdev, exposed_as)
        self._exposed_as = exposed_as

    def _hide(self):
        "Hide symlink as soon as someone connects"
        if not os.path.islink(self._exposed_as):
            warn('{!r} is not a symlink?'.format(self._exposed_as))
        else:
            try:
                os.unlink(self._exposed_as)
            except OSError as e:
                warn('error {} during unlink of {!r}'.format(
                    e.args[0], self._exposed_as))
        self._exposed_as = None

    def _reader(self, *args, **kwargs):
        if self._exposed_as is not None:
            self._hide()
        return super()._reader(*args, **kwargs)

    def close(self):
        if self._exposed_as is not None:
            self._hide()
        super().close()


def spawn_serialproxy_child(devname):
    """
    Spawn a SerialProxy child process

    The SerialProxy will proxy data between the two serial endpoints.
    Connect two applications which you would normally connect to a
    serial interface like /dev/ttyAMA0.

    Example in server process:

        class ChildExited(Exception):
            pass

        def sigchld(frame, signum):
            pid, status = os.wait()
            raise ChildExited(pid, status)

        signal.signal(signal.SIGCHLD, sigchld)

        proxy_child, proxy_devname = spawn_serialproxy_child(
            '/path/to/serial.sock')
        ser = serial.Serial(
            port=proxy_devname, baudrate=9600, bytesize=7,
            parity=serial.PARITY_EVEN, stopbits=1,
            exclusive=True)
        ser.read_byte()

    Example in the client process:

        ser = serial.Serial(
            port='/path/to/serial.sock', baudrate=9600, bytesize=7,
            parity=serial.PARITY_EVEN, stopbits=1,
            exclusive=True)
        ser.write_byte(b'X')

    This allows you to test the client against a local (test) server
    instead of to a hardware device.

    Caveats: baudrate, bytesize, parity and stopbits are not actually
    enforced, but an attempt is made to check baudrate equality on both
    sides of the proxy.
    """
    rfd, wfd = os.pipe2(0)

    # Take care not to do any asyncio calls before the fork. Otherwise
    # their results may be attached to the parent loop, which is now
    # unreachable.
    child_pid = os.fork()
    if child_pid != 0:
        os.close(wfd)
        adev = os.read(rfd, 255).decode('ascii')
        os.close(rfd)
        return child_pid, adev

    # We don't need stdin in the child (and sys.stdin.close() does not
    # actually close any fds).
    os.close(sys.stdin.fileno())
    os.close(rfd)

    proxy = ExposedSerialProxy(devname)
    child_pid = os.getpid()
    print(f'Running proxy {child_pid} on {devname} ({proxy.bdev})')

    # Report address back to parent.
    os.write(wfd, proxy.adev.encode('ascii'))
    os.close(wfd)

    try:
        asyncio.run(proxy.run())
    except KeyboardInterrupt:
        proxy.close()
    print('Stopped proxy')
    os._exit(0)
