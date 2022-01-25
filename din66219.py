from ctrlcode import EOT, ETX, SOH, STX


def append_bcc(bstr):
    if isinstance(bstr, str):
        bstr = bstr.encode('ascii')

    bcc = 0
    it = iter(bstr)
    ch = None
    cnt = 0
    for ch in it:
        cnt += 1
        if ch in (SOH, STX):
            break
    for ch in it:
        cnt += 1
        bcc ^= ch
        # if ch in (ETX, EOT):
        #     break

    if cnt != len(bstr) or ch not in (ETX, EOT):
        raise ValueError(f'expected one ETX/EOT at end of {bstr!r}, got {ch}')

    return bstr + bytearray([bcc])


def check_bcc(bstr):
    if isinstance(bstr, str):
        bstr = bstr.encode('ascii')

    bcc = 0
    it = iter(bstr)
    ch = None
    cnt = 0
    for ch in it:
        cnt += 1
        if ch in (SOH, STX):
            break
    for ch in it:
        cnt += 1
        bcc ^= ch
        if ch in (ETX, EOT):
            break

    if ch not in (ETX, EOT):
        raise ValueError(f'expected ETX/EOT at end of {bstr!r}, got {ch}')
    if cnt != (len(bstr) - 1):
        raise ValueError(f'expected $BCC at end of {bstr!r}')

    ch = next(it)
    if ch != bcc:
        raise ValueError(f'$BCC mismatch {bstr!r} expected {bcc}')
