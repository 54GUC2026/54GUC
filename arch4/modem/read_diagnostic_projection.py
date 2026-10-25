"""One bounded field projection for read-only router diagnostics; no admission."""
import math
import re

def diagnostic_field_name(key):
    return re.sub(r"[^a-z0-9]", "", key.lower())


# Diagnostic discovery is deliberately broader than measurement admission.
# Names alone never assign units, identity, radio technology or capability.
INTEREST = re.compile(r"temp|^pmsensor|^pmmodem5g|rsrp|rsrq|rssi|sinr|snr|neighbor|neighbour|nbrcell|seccell|antenna|(?:^|lte|nr)ri$|rank|rrc|txpower|pusch|pucch|prach|srs|lteca|nrca|mcs|cqi|qam|band|arfcn|earfcn|pcell|scell|cellid|pci|(?:rx|tx)(?:bytes|speed|thrpt)|download|upload|(?:rx|tx)flow|apn|roam|netselect|networktype|pppstatus|operatemode|romversion|softwareversion|hwversion|hardware|firmware|model|wainnerversion|webversion|crversion|datausage|(?:list|band)(?:4g|5g)")
SENSITIVE = re.compile(r"pass|pwd|secret|token|sessionid|encrypt|private|salt|sault|credential|authkey|^(?:ad|ld|rd|nonce)$")
IDENTITY = re.compile(r"imei|imsi|iccid|serial|msn|mac|address|ipaddr|wanip|username|^sn$")
RESOURCE_FIELDS = frozenset({"cpuusage", "total", "free", "uptime", "xtptotaltime", "xtpidletime", "processnumberofentries", "deviceuptime"})
MEASUREMENT_FIELDS = frozenset({"ri", "downlinkmodtype", "uplinkmodtype", "numrbs", "downfreq", "upfreq", "cellconnectionstatus"})
COUNTER_CANDIDATE_FIELDS = frozenset({"bytesreceived", "bytessent", "monthbytesreceived", "monthbytessent",
    "dailyflow", "totalstatistics", "currentdate", "enablepaymentday", "paymentday",
    "nextdue", "curconntime", "totalconntime", "realtime", "daytime", "monthtime", "totaltime"})
SUPPLEMENTARY_FIELDS = frozenset({"status", "pincheck", "remainpinunlocktime",
    "networkpreferredmodeselected", "networkpreferredmodeoptionlist", "networkpreferredmode",
    "dataswitchstatus", "endcstatus", "ifname", "gatewayv4", "netmask", "dns1v4", "dns2v4",
    "ipv4mtu", "lteproductid", "pppuptime", "realtimetime", "monthlytime", "datemonth",
    "pinstatus", "modemmainstate", "wifienabled", "wifionoffstate",
    "wifichip1ssid1switchonoff", "wifichip2ssid1switchonoff", "radiooff",
    "thermpalevel", "thermpafrllevel", "thermtjlevel", "antswitchenable"})
FORMAT_CANDIDATE_FIELDS = frozenset({"tac", "lac", "plmn", "enodebid", "boottime",
    "webuiversion", "configversion", "dlfrequency", "ulfrequency", "ltedlfreq", "lteulfreq",
    "nrdlfreq", "nrulfreq", "currentconnecttime", "totalconnecttime"})


def project_fields(data, *, identity_paths=()):
    """Bounded structure plus useful scalar RF/config evidence, secrets excluded."""
    fields, identities, budget, truncated = {}, [], [256], [False]
    def walk(value, path="", depth=0):
        if depth > 5 or budget[0] <= 0:
            truncated[0] = True
            return
        if isinstance(value, dict):
            if len(value) > 100:
                truncated[0] = True
            for key, item in list(value.items())[:100]:
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:/ -]{1,80}", key):
                    continue
                norm = diagnostic_field_name(key)
                if SENSITIVE.search(norm):
                    continue
                full = path + key
                if full in identity_paths or IDENTITY.search(norm):
                    identities.append({"name": full, "state": "present" if item not in (None, "", [], {}) else "empty"})
                    budget[0] -= 1
                elif isinstance(item, (dict, list)):
                    if not item:
                        fields[full] = {"state": "empty", "type": type(item).__name__}
                        budget[0] -= 1
                    else:
                        walk(item, full + ".", depth + 1)
                else:
                    state = "empty" if item in (None, "") else "returned"
                    row = {"state": state, "type": type(item).__name__}
                    # Repeated native values retain their own path and spelling.
                    # These candidates never become accepted measurements here.
                    parents = tuple(diagnostic_field_name(part) for part in path.rstrip(".").split("."))
                    format_key = next((part for part in reversed(parents) if not part.isdigit()), norm) if norm.isdigit() else norm
                    resource_member = (norm in {"available", "avaliable"} and "meminfo" in parents
                                       or norm in {"idle", "usr", "sys"} and "cpuinfo" in parents)
                    if INTEREST.search(norm) or INTEREST.search(diagnostic_field_name(full)) or norm in MEASUREMENT_FIELDS or norm in RESOURCE_FIELDS or norm in COUNTER_CANDIDATE_FIELDS or norm in SUPPLEMENTARY_FIELDS or format_key in FORMAT_CANDIDATE_FIELDS or resource_member or norm in {"up", "enable", "connectmode", "roamenable", "simstatus", "networkprovider", "mode", "selectname", "networkdata", "networkauto", "linktype"}:
                        if type(item) in (int, bool) or (type(item) is float and math.isfinite(item)):
                            row["value"] = item
                        elif isinstance(item, str):
                            # Do not copy arbitrary prose or credentials disguised
                            # as a measurement. APN/config strings are public values.
                            ordinary = re.fullmatch(r"[A-Za-z0-9_.,;:+/%° ()-]{0,180}", item)
                            band_text = (format_key in {"band", "bandinfo"} and re.fullmatch(
                                r"[0-9]+(?:\.[0-9]+)?(?:MHz|kHz|Hz)@[0-9]+\((?:N|B)?[0-9]{1,3}\)", item, re.IGNORECASE))
                            boot_text = (format_key == "boottime" and re.fullmatch(
                                r"(?:\[[0-9]{1,9}\]|[0-9]{1,9})(?::(?:\[[0-9]{1,9}\]|[0-9]{1,9})){2,3}", item))
                            if len(item) <= 180 and (ordinary or band_text or boot_text):
                                row["value"] = item
                        row["interpretation"] = "candidate; measurement admission is separate"
                    fields[full] = row
                    budget[0] -= 1
                if budget[0] <= 0:
                    break
        elif isinstance(value, list):
            if len(value) > 64:
                truncated[0] = True
            for i, item in enumerate(value[:64]):
                if budget[0] <= 0:
                    break
                if isinstance(item, (dict, list)):
                    walk(item, path + str(i) + ".", depth + 1)
                else:
                    walk({str(i): item}, path, depth + 1)
    walk(data)
    return {"fields": fields, "identity_presence": identities, "truncated": truncated[0] or budget[0] <= 0}
