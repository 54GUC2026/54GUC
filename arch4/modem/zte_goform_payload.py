"""MF296R B04's normal WebUI payload session; no radio/control admission."""
import base64
import re

from cryptography import x509
from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def wrap_session_key(encoded, key):
    """JSEncrypt RSA PKCS#1 v1.5 over a 32-byte key's hexadecimal text."""
    try:
        if not isinstance(encoded,str) or len(encoded)>16384:
            raise ValueError
        value=encoded.strip()
        if value.startswith('-----BEGIN'):
            public=(x509.load_pem_x509_certificate(value.encode()).public_key()
                if value.startswith('-----BEGIN CERTIFICATE-----')
                else serialization.load_pem_public_key(value.encode()))
        else:
            public=serialization.load_der_public_key(base64.b64decode(
                re.sub(r'\s+','',value),validate=True))
        if not isinstance(public,rsa.RSAPublicKey) or not 1024<=public.key_size<=4096:
            raise ValueError
        return base64.b64encode(public.encrypt(key.hex().encode('ascii'),padding.PKCS1v15())).decode('ascii')
    except (ValueError,TypeError,UnicodeError,UnsupportedAlgorithm):
        raise ConnectionError('zte_payload_session_certificate_invalid') from None


def decode_sim_payload(value,key):
    """Wire layout is base64(12-byte IV + 16-byte tag + ciphertext)."""
    try:
        if not isinstance(value,str) or not 1<=len(value)<=128:
            return ''
        packet=base64.b64decode(value,validate=True)
        if not 42<=len(packet)<=44:
            return ''
        text=AESGCM(key).decrypt(packet[:12],packet[28:]+packet[12:28],None).decode('ascii')
        return text if re.fullmatch(r'[0-9]{14,16}',text) else ''
    except (ValueError,TypeError,UnicodeError,InvalidTag):
        return ''
