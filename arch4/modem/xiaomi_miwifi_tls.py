"""Xiaomi stock-device TLS trust, restricted to the backend HTTPS session.

Public server certificate from manufacturer CB0401V2 native3.0.85 firmware.
SHA256 e17addf4286d5acf7269abc0d54192c55fd239a33e2b8e58e1bffa819063deac.
Trust only this explicit device certificate, retaining hostname/date verification.
No OS certificate-store change, general manufacturer CA trust or verification bypass.
"""
import ssl

from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError


STOCK_SERVER_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIIFDDCCA/SgAwIBAgIUeha5V95blhAwY03e3h3H+xfT5gQwDQYJKoZIhvcNAQEL
BQAwdDELMAkGA1UEBhMCQ04xEDAOBgNVBAgTB0JlaUppbmcxEDAOBgNVBAcTB0Jl
aUppbmcxDzANBgNVBAoTBlhJQU9NSTEXMBUGA1UECxMOTUlXSUZJIFJPT1QgQ0Ex
FzAVBgNVBAMTDk1JV0lGSSBST09UIENBMCAXDTIwMTAzMDA4MjYyMVoYDzIxMjAx
MDMwMDg0NjIxWjB8MQswCQYDVQQGEwJDTjEQMA4GA1UECBMHQmVpSmluZzEQMA4G
A1UEBxMHQmVpSmluZzEPMA0GA1UEChMGWElBT01JMRswGQYDVQQLExJNSVdJRkkg
U0VSVkVSIENFUlQxGzAZBgNVBAMTEk1JV0lGSSBTRVJWRVIgQ0VSVDCCASIwDQYJ
KoZIhvcNAQEBBQADggEPADCCAQoCggEBAK/Tas6ls0dQiBd++cEud+iTOY51aVdu
1dNkPucb6wc1NWUr0GT2pRnE0UpQDs3hH+TNETdzQ5fKR2bNDo7d9hpz2B2cnrYE
SPTH2m6BetVY+XOHusTs4x4y3c7H4Ny2F5Ak/cELzDubcbFYBidVVNaduffrozCw
p62Y2Jf3xyREuEwAqgge7B2sRU1ZSMTtIuTEV6dcqHHZWbSLN3YMZII1QMvYI0W9
mZZzRCLDHT+FPm8YPj+DRgbg2x8ZbMZ4ssAexv5iKp3zwI3C9UfVqGu1BK9022yK
TPlFFTxYPzeH7raD08OtPTT047veONAI1klJLCR0jlUeZq9+lLcQ4PkCAwEAAaOC
AYowggGGMA4GA1UdDwEB/wQEAwIDiDATBgNVHSUEDDAKBggrBgEFBQcDATAMBgNV
HRMBAf8EAjAAMB0GA1UdDgQWBBT8+kDf2DYqzu4izAU5T2XfUOfDsTAfBgNVHSME
GDAWgBRLf4ugE1zHbUCMpgbG2rDbzwK8JTCBhgYIKwYBBQUHAQEEejB4MC0GCCsG
AQUFBzABhiFodHRwOi8vY2EubWlvZmZpY2UuY24vbWl3aWZpLW9jc3AwRwYIKwYB
BQUHMAKGO2h0dHA6Ly9jYS5taW9mZmljZS5jbi9taXdpZmktY2FkYXRhL21pd2lm
aS1pbnRlcm1lZGlhdGUucGVtMDoGA1UdEQQzMDGCCm1pd2lmaS5jb22CEXJvdXRl
ci5taXdpZmkuY29thwTAqB8BhwR/AAABhwTAqAEBMEwGA1UdHwRFMEMwQaA/oD2G
O2h0dHA6Ly9jYS5taW9mZmljZS5jbi9taXdpZmktY2FkYXRhL21pd2lmaS1pbnRl
cm1lZGlhdGUuY3JsMA0GCSqGSIb3DQEBCwUAA4IBAQAK5BpgLojRv1ZJ/k6FQ5iz
Wk0xoIZ+opsrKNEyrcEQnMHVGLWVHtayzKSopnsFicelG+2ibPDpnSBNkBGiTZWa
RNpm+COBoGmvim/RX+ZKAmrrBvO6NI8pnGSBqyVKy8U4mJyL2DO17+YHRF3L1lcN
VtKG3k6c5vcywwLa8BPEKZYJMGKLfQVtK6DTkcTwWiusRJu7o3FspBvT0VNfJx4e
9VC52nofradxSihjqjVjxoM8+0deJw3Pi4HW+OEZ1VCLy7STu+sn6H2OTD7pCSFx
CQcBworO2KVMg/s5IRsMnJ3nzGUIgYrWuk1jeoM3qmYHsq9j6dLAsc8ev+OJzCiU
-----END CERTIFICATE-----
"""


class XiaomiHTTPSAdapter(HTTPAdapter):
    def __init__(self):
        self._context = ssl.create_default_context(cadata=STOCK_SERVER_CERTIFICATE)
        self._context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        super().__init__()

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, options = super().build_connection_pool_key_attributes(request, verify, cert)
        options["ssl_context"] = self._context
        return host, options

    def cert_verify(self, conn, url, verify, cert):
        if verify is not True or cert:
            raise SSLError("xiaomi_device_certificate_verification_required")
        # The context already contains the explicitly trusted server certificate.
        # Do not let HTTPAdapter append the general-purpose CA bundle to it.
        conn.cert_reqs = "CERT_REQUIRED"
        conn.ca_certs = conn.ca_cert_dir = None

