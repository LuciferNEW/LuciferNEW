"""SF2 PH 205267 framing, recovered from UPacket and SFPacketCryptHelper.

Plaintext login responses are accepted by the real client. Encryption routines
remain available for the recovered request format and isolated protocol tests.
"""
from pathlib import Path
import hashlib
import hmac
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/python-deps'))
from Crypto.Cipher import AES

KEY = b'Q3k87pXh9a5oP62Z'
MAX_PAYLOAD = 2047

class ProtocolError(ValueError):
    pass

class Writer:
    def __init__(self):
        self.payload = bytearray()

    def add(self, kind, value):
        formats = {'byte': '<B', 'short': '<h', 'int': '<i', 'long': '<Q', 'float': '<f'}
        if kind == 'string':
            data = str(value).encode('utf-8') + b'\0'
            encoded = struct.pack('<H', len(data)) + data
        else:
            encoded = struct.pack(formats[kind], value)
        if len(self.payload) + len(encoded) > MAX_PAYLOAD:
            raise ProtocolError('packet exceeds the native 2048-byte buffer')
        self.payload.extend(encoded)
        return self

    def frame(self, command, encrypted=False):
        return make_frame(command, bytes(self.payload), encrypted)

class Reader:
    def __init__(self, payload):
        self.payload = bytes(payload)
        self.offset = 0

    def take(self, count):
        if count < 0 or self.offset + count > len(self.payload):
            raise ProtocolError('truncated field')
        result = self.payload[self.offset:self.offset + count]
        self.offset += count
        return result

    def get(self, kind):
        formats = {'byte': '<B', 'short': '<h', 'int': '<i', 'long': '<Q', 'float': '<f'}
        if kind == 'string':
            size = struct.unpack('<H', self.take(2))[0]
            if not size:
                raise ProtocolError('string must include a terminator')
            data = self.take(size)
            if data[-1:] != b'\0':
                raise ProtocolError('string has no terminator')
            return data[:-1].decode('utf-8', errors='strict')
        fmt = formats[kind]
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def require_end(self):
        if self.offset != len(self.payload):
            raise ProtocolError('unexpected trailing fields')

def encrypt_payload(payload):
    if not payload:
        return b''
    pad = 16 - len(payload) % 16
    cleartext = hashlib.md5(payload).digest() + payload + bytes([pad]) * pad
    if len(cleartext) > MAX_PAYLOAD:
        raise ProtocolError('encrypted packet exceeds native buffer')
    return AES.new(KEY, AES.MODE_ECB).encrypt(cleartext)

def decrypt_payload(payload):
    if not payload:
        return b''
    if len(payload) < 32 or len(payload) % 16:
        raise ProtocolError('invalid encrypted payload length')
    cleartext = AES.new(KEY, AES.MODE_ECB).decrypt(payload)
    pad = cleartext[-1]
    if not 1 <= pad <= 16 or cleartext[-pad:] != bytes([pad]) * pad:
        raise ProtocolError('invalid padding')
    digest, decoded = cleartext[:16], cleartext[16:-pad]
    if not hmac.compare_digest(digest, hashlib.md5(decoded).digest()):
        raise ProtocolError('MD5 check failed')
    return decoded

def make_frame(command, payload=b'', encrypted=False):
    if not 0 <= command <= 65535:
        raise ProtocolError('invalid command')
    if encrypted:
        payload = encrypt_payload(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError('packet exceeds native buffer')
    return struct.pack('<HH', len(payload) + 4, command) + payload

def parse_frame(frame, encrypted=False):
    if len(frame) < 4:
        raise ProtocolError('truncated frame header')
    size, command = struct.unpack_from('<HH', frame)
    if size != len(frame) or size > MAX_PAYLOAD + 4:
        raise ProtocolError('invalid frame length')
    payload = frame[4:]
    return command, decrypt_payload(payload) if encrypted else payload

def response(protocol, fields=(), result=1, encrypted=False):
    writer = Writer().add('short', protocol)
    for kind, value in fields:
        writer.add(kind, value)
    return writer.frame(result, encrypted)
