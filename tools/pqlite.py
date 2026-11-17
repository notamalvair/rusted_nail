"""Минимальный читатель Parquet: плоская схема, PLAIN/RLE_DICTIONARY, страницы v1/v2, ZSTD/без сжатия.
Хватает для файлов, которые пишет DuckDB. Сжатие ZSTD через системный libzstd (ctypes)."""

import ctypes
import ctypes.util
import struct

_zstd = ctypes.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
_zstd.ZSTD_decompress.restype = ctypes.c_size_t
_zstd.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
_zstd.ZSTD_isError.argtypes = [ctypes.c_size_t]
_zstd.ZSTD_isError.restype = ctypes.c_uint


def zstd_decompress(data: bytes, size: int) -> bytes:
    out = ctypes.create_string_buffer(size)
    n = _zstd.ZSTD_decompress(out, size, data, len(data))
    if _zstd.ZSTD_isError(n):
        raise ValueError("zstd error")
    if n != size:
        raise ValueError(f"zstd size mismatch {n} != {size}")
    return out.raw


# ---------------- Thrift compact protocol ----------------
class Reader:
    def __init__(self, buf, pos=0):
        self.b, self.p = buf, pos

    def byte(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def varint(self):
        shift = result = 0
        while True:
            c = self.byte()
            result |= (c & 0x7F) << shift
            if not c & 0x80:
                return result
            shift += 7

    def zigzag(self):
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def value(self, t):
        if t == 1:
            return True
        if t == 2:
            return False
        if t == 3:
            v = self.byte()
            return v - 256 if v > 127 else v
        if t in (4, 5, 6):
            return self.zigzag()
        if t == 7:
            v = struct.unpack_from("<d", self.b, self.p)[0]
            self.p += 8
            return v
        if t == 8:
            n = self.varint()
            v = bytes(self.b[self.p:self.p + n])
            self.p += n
            return v
        if t in (9, 10):
            h = self.byte()
            size, et = h >> 4, h & 0x0F
            if size == 15:
                size = self.varint()
            if et in (1, 2):  # bool в списке — байт
                return [self.byte() == 1 for _ in range(size)]
            return [self.value(et) for _ in range(size)]
        if t == 11:
            size = self.varint()
            if size == 0:
                return {}
            kv = self.byte()
            return {self.value(kv >> 4): self.value(kv & 0x0F) for _ in range(size)}
        if t == 12:
            return self.struct()
        raise ValueError(f"thrift type {t}")

    def struct(self):
        out, last = {}, 0
        while True:
            h = self.byte()
            if h == 0:
                return out
            delta, t = h >> 4, h & 0x0F
            fid = last + delta if delta else self.zigzag()
            out[fid] = self.value(t)
            last = fid


# ---------------- encodings ----------------
def rle_hybrid(buf, pos, end, bit_width, count):
    """RLE/bit-packed hybrid; возвращает список из count значений."""
    out = []
    r = Reader(buf, pos)
    byte_w = (bit_width + 7) // 8
    mask = (1 << bit_width) - 1
    while len(out) < count and r.p < end:
        h = r.varint()
        if h & 1:
            groups = h >> 1
            nbytes = groups * bit_width
            chunk = int.from_bytes(buf[r.p:r.p + nbytes], "little")
            r.p += nbytes
            for i in range(groups * 8):
                out.append((chunk >> (i * bit_width)) & mask)
        else:
            run = h >> 1
            v = int.from_bytes(buf[r.p:r.p + byte_w], "little") if byte_w else 0
            r.p += byte_w
            out.extend([v] * run)
    return out[:count]


def plain(buf, ptype, n, type_len=None):
    if ptype == 6:
        out, p = [], 0
        for _ in range(n):
            ln = struct.unpack_from("<I", buf, p)[0]
            out.append(bytes(buf[p + 4:p + 4 + ln]))
            p += 4 + ln
        return out
    if ptype == 0:
        return [bool((buf[i >> 3] >> (i & 7)) & 1) for i in range(n)]
    fmt = {1: "<i", 2: "<q", 4: "<f", 5: "<d"}.get(ptype)
    if fmt is None:
        raise ValueError(f"plain type {ptype}")
    return list(struct.unpack_from("<" + fmt[1] * n, buf, 0))


# ---------------- file ----------------
def read(path):
    data = open(path, "rb").read()
    if data[:4] != b"PAR1" or data[-4:] != b"PAR1":
        raise ValueError("not parquet")
    flen = struct.unpack_from("<I", data, len(data) - 8)[0]
    meta = Reader(data, len(data) - 8 - flen).struct()
    schema = meta[2]
    leaves = [s for s in schema[1:] if not s.get(5)]
    names = [s[4].decode() for s in leaves]
    cols = {n: [] for n in names}
    for rg in meta[4]:
        for ci, cc in enumerate(rg[1]):
            md = cc[3]
            ptype, codec, nvals = md[1], md[4], md[5]
            leaf = leaves[ci]
            max_def = 1 if leaf.get(3, 0) == 1 else 0
            start = md.get(11) or md[9]
            if md.get(11) is not None:
                start = min(md[11], md[9])
            r = Reader(data, start)
            dictionary = None
            got = 0
            col = cols[names[ci]]
            while got < nvals:
                ph = r.struct()
                ptype_page, usize, csize = ph[1], ph[2], ph[3]
                raw = data[r.p:r.p + csize]
                r.p += csize
                if ptype_page == 2:  # dictionary
                    body = zstd_decompress(raw, usize) if codec == 6 else raw
                    dictionary = plain(body, ptype, ph[7][1])
                    continue
                if ptype_page == 0:
                    dh = ph[5]
                    n, enc = dh[1], dh[2]
                    body = zstd_decompress(raw, usize) if codec == 6 else raw
                    p = 0
                    if max_def:
                        ln = struct.unpack_from("<I", body, 0)[0]
                        defs = rle_hybrid(body, 4, 4 + ln, 1, n)
                        p = 4 + ln
                    else:
                        defs = [1] * n
                    vals = body[p:]
                elif ptype_page == 3:
                    dh = ph[8]
                    n, nnull, enc = dh[1], dh[2], dh[4]
                    dlen, rlen = dh[5], dh[6]
                    compressed = dh.get(7, True)
                    lv = raw[rlen:rlen + dlen]
                    defs = rle_hybrid(lv, 0, len(lv), 1, n) if max_def else [1] * n
                    rest = raw[rlen + dlen:]
                    vals = zstd_decompress(rest, usize - rlen - dlen) if (codec == 6 and compressed) else rest
                else:
                    raise ValueError(f"page type {ptype_page}")
                present = sum(1 for d in defs if d == max_def) if max_def else n
                if enc in (2, 8):
                    bw = vals[0]
                    idx = rle_hybrid(vals, 1, len(vals), bw, present)
                    decoded = [dictionary[i] for i in idx]
                elif enc == 0:
                    decoded = plain(vals, ptype, present)
                else:
                    raise ValueError(f"encoding {enc} in column {names[ci]}")
                it = iter(decoded)
                for d in defs:
                    col.append(next(it) if d == max_def else None)
                got += n
    # строки байтов -> str
    for name, leaf in zip(names, leaves):
        if leaf[1] == 6:
            cols[name] = [v.decode() if isinstance(v, bytes) else v for v in cols[name]]
    return cols, meta[3]
