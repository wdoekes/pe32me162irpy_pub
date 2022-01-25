"""
XXX: fix what/why
"""


class Char:
    """
    Magic char class that allows us to f"{ACK}" and bin[2] == ACK

    Example usage:

        ACK = Char(6)
        NAK = Char(21)

        buf = f'{ACK}123\r\n'  # or ..{CR}{LF}
        assert len(buf) == 6
        assert buf[0] == ACK
        assert buf.encode('ascii')[0] in (ACK, NAK)

    """
    def __init__(self, i):
        self.i = i

    def __eq__(self, other):
        if isinstance(other, Char):
            return self.i == other.i
        elif isinstance(other, int):
            return self.i == other
        elif isinstance(other, (bytes, bytearray, str)) and len(other) == 1:
            # NOTE: ord() may return high integers for unicode chars
            return self.i == ord(other)
        return False

    def __str__(self):
        return chr(self.i)

    def __repr__(self):
        if self.i < 32:
            return '\x1b[1;34m^{}\x1b[0m'.format(chr(64 + self.i))
        # elif self.i == 94:
        #     return '^^'
        return chr(self.i)


SOH = Char(1)
STX = Char(2)
ETX = Char(3)
EOT = Char(4)
ACK = Char(6)
LF = Char(10)
CR = Char(13)
NAK = Char(21)
