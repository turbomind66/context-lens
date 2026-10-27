#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
leveldb.py —— 纯 Python 的 LevelDB 读取器（只读，够用即可）

用途：读 Chrome/Electron 的 IndexedDB（*.indexeddb.leveldb），
把里面的 key/value 还原出来。不依赖任何第三方库，也不需要编译。

实现范围：
  · Snappy 块解压（LevelDB 的 SSTable 用 snappy 压数据块）
  · SSTable(.ldb/.sst)：footer → index block → data block → (key,value)
  · Write-Ahead Log(.log)：32KB 分块 + FULL/FIRST/MIDDLE/LAST 拼装 → WriteBatch
  · 多文件合并：同一个 user key 取最大 sequence

不做：CRC 校验、布隆过滤器、压缩以外的编码。
"""
import os
import glob
import struct


# ── 基础：varint ────────────────────────────────────────────
def read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


# ── Snappy 解压 ─────────────────────────────────────────────
def snappy_decompress(data):
    ulen, pos = read_varint(data, 0)
    out = bytearray()
    n = len(data)
    while pos < n:
        tag = data[pos]
        pos += 1
        t = tag & 0x03
        if t == 0:                                  # literal
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                extra = ln - 59
                ln = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            out += data[pos:pos + ln]
            pos += ln
        else:                                       # copy
            if t == 1:
                ln = 4 + ((tag >> 2) & 0x07)
                off = ((tag >> 5) << 8) | data[pos]
                pos += 1
            elif t == 2:
                ln = (tag >> 2) + 1
                off = int.from_bytes(data[pos:pos + 2], "little")
                pos += 2
            else:
                ln = (tag >> 2) + 1
                off = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4
            start = len(out) - off
            if start < 0:
                raise ValueError("bad snappy offset")
            for k in range(ln):
                out.append(out[start + k])
    if ulen != len(out):
        # 长度对不上也先返回，调用方自己判断
        pass
    return bytes(out)


# ── SSTable 数据块 ──────────────────────────────────────────
def _parse_block(block):
    """块结构：entries... + restarts(uint32 * n) + n(uint32)"""
    if len(block) < 4:
        return []
    n_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * n_restarts
    if end < 0:
        return []
    out = []
    pos = 0
    key = b""
    while pos < end:
        shared, pos = read_varint(block, pos)
        non_shared, pos = read_varint(block, pos)
        vlen, pos = read_varint(block, pos)
        key = key[:shared] + block[pos:pos + non_shared]
        pos += non_shared
        val = block[pos:pos + vlen]
        pos += vlen
        out.append((key, val))
    return out


def _read_block(f, offset, size):
    f.seek(offset)
    raw = f.read(size)
    if len(raw) < size:
        return []
    f.seek(offset + size)
    trailer = f.read(5)
    ctype = trailer[0] if trailer else 0
    if ctype == 1:
        raw = snappy_decompress(raw)
    elif ctype != 0:
        return []          # 其它压缩（zstd 等）暂不支持
    return _parse_block(raw)


def read_sstable(path):
    """返回 [(internal_key, value), ...]"""
    size = os.path.getsize(path)
    if size < 48:
        return []
    with open(path, "rb") as f:
        f.seek(size - 48)
        footer = f.read(48)
        pos = 0
        _, pos = read_varint(footer, pos)          # metaindex offset
        _, pos = read_varint(footer, pos)          # metaindex size
        idx_off, pos = read_varint(footer, pos)
        idx_size, pos = read_varint(footer, pos)
        out = []
        for _, handle in _read_block(f, idx_off, idx_size):
            p = 0
            blk_off, p = read_varint(handle, p)
            blk_size, p = read_varint(handle, p)
            out.extend(_read_block(f, blk_off, blk_size))
        return out


# ── WAL ─────────────────────────────────────────────────────
_BLOCK = 32768
_TYPE_FULL, _TYPE_FIRST, _TYPE_MIDDLE, _TYPE_LAST = 1, 2, 3, 4


def read_wal(path):
    """返回 [(seq, key, value)]"""
    with open(path, "rb") as f:
        data = f.read()
    out = []
    pending = b""
    pos = 0
    n = len(data)
    while pos + 7 <= n:
        # 块尾剩余不足 7 字节则跳过填充
        left_in_block = _BLOCK - (pos % _BLOCK)
        if left_in_block < 7:
            pos += left_in_block
            continue
        length = struct.unpack_from("<H", data, pos + 4)[0]
        rtype = data[pos + 6]
        body = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if rtype == 0 and length == 0:
            continue
        if rtype == _TYPE_FULL:
            out.extend(_parse_batch(body))
            pending = b""
        elif rtype == _TYPE_FIRST:
            pending = body
        elif rtype == _TYPE_MIDDLE:
            pending += body
        elif rtype == _TYPE_LAST:
            pending += body
            out.extend(_parse_batch(pending))
            pending = b""
    return out


def _parse_batch(body):
    if len(body) < 12:
        return []
    seq = struct.unpack_from("<Q", body, 0)[0]
    count = struct.unpack_from("<I", body, 8)[0]
    pos = 12
    out = []
    for _ in range(count):
        if pos >= len(body):
            break
        typ = body[pos]
        pos += 1
        klen, pos = read_varint(body, pos)
        key = body[pos:pos + klen]
        pos += klen
        val = b""
        if typ == 1:
            vlen, pos = read_varint(body, pos)
            val = body[pos:pos + vlen]
            pos += vlen
        out.append((seq, key, val))
    return out


# ── 合并一个 leveldb 目录 ───────────────────────────────────
def read_db(db_dir, want=None, max_value=None):
    """读整个 leveldb 目录，返回 {user_key: value}（同 key 取最大 seq）。

    want: 可选的字节串，只有 value 里包含它才保留（省内存）。
    """
    best = {}      # key -> (seq, value)
    entries = []
    for p in glob.glob(os.path.join(db_dir, "*.ldb")) + glob.glob(os.path.join(db_dir, "*.sst")):
        try:
            for ikey, val in read_sstable(p):
                if len(ikey) < 8:
                    continue
                seq = struct.unpack("<Q", ikey[-8:])[0] >> 8
                entries.append((seq, ikey[:-8], val))
        except Exception:
            continue
    for p in glob.glob(os.path.join(db_dir, "*.log")):
        try:
            entries.extend(read_wal(p))
        except Exception:
            continue

    for seq, key, val in entries:
        if want is not None and want not in val:
            continue
        cur = best.get(key)
        if cur is None or seq > cur[0]:
            best[key] = (seq, val)
    return {k: v[1] for k, v in best.items()}


if __name__ == "__main__":
    import sys, json
    d = sys.argv[1]
    want = sys.argv[2].encode() if len(sys.argv) > 2 else None
    recs = read_db(d, want=want)
    print(f"records={len(recs)}")
    for i, (k, v) in enumerate(recs.items()):
        if i >= 5:
            break
        print("KEY", k[:60].hex(), k[:60])
        print("VAL", v[:300])
