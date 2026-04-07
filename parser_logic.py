# parser_logic.py
import re

def _normalize_vlan(v):
    v = (v or "").strip()
    if not v: return "N/A"
    m = re.search(r"\b(\d{1,4})\b", v)
    return m.group(1) if m else v

def _find_first_match_value(kv, pattern):
    rx = re.compile(pattern)
    for k, v in kv.items():
        if rx.search(k) and v: return v
    return ""

def extract_native_vlan(kv, iface):
    val = kv.get(f"lldp.{iface}.vlan.vlan-id", "")
    if not val: val = kv.get(f"lldp.{iface}.port.vlan-id", "")
    return _normalize_vlan(val)

def extract_voice_vlan(kv, iface):
    patterns = [
        rf"^lldp\.{re.escape(iface)}\..*med\.policy.*(voice|application).*vlan",
        rf"^lldp\.{re.escape(iface)}\..*med\.policy.*vlan-id"
    ]
    for p in patterns:
        v = _find_first_match_value(kv, p)
        if v: return _normalize_vlan(v)
    v = _find_first_match_value(kv, rf"^lldp\.{re.escape(iface)}\..*(cdp|aux).*(voice|vlan)")
    return _normalize_vlan(v) if v else "N/A"

def extract_port_speed(kv, iface):
    speed = kv.get(f"lldp.{iface}.port.speed", "")
    if speed:
        if speed == "1000": return "1G"
        if speed == "10000": return "10G"
        return f"{speed}M"
    return "N/A"

def extract_port_description(kv, iface):
    descr = kv.get(f"lldp.{iface}.port.descr", "").strip()
    if not descr: return "N/A"
    return (descr[:20] + "...") if len(descr) > 20 else descr

def extract_switch_hostname(kv, iface):
    sw = kv.get(f"lldp.{iface}.chassis.name", "N/A")
    return sw.split(".")[0] if "." in sw else sw
