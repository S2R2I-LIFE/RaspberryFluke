# test_logic.py
from parser_logic import *

# 1. DEFINE MOCK DATA FIRST
mock_kv = {
    "lldp.eth0.chassis.name": "Core-Switch-01.company.local",
    "lldp.eth0.vlan.vlan-id": "10",
    "lldp.eth0.med.policy.0.application.voice.vlan-id": "1180",
    "lldp.eth0.port.speed": "1000",
    "lldp.eth0.port.descr": "UPLINK-TO-SERVER-RACK-ROW-B"
}
iface = "eth0"

# 2. NOW EXECUTE FUNCTIONS
sw = extract_switch_hostname(mock_kv, iface)
ip = "192.168.1.1"
port = "Gi0/1"
nat = extract_native_vlan(mock_kv, iface)
voice = extract_voice_vlan(mock_kv, iface)
speed = extract_port_speed(mock_kv, iface)
desc = extract_port_description(mock_kv, iface)

# 3. CONSTRUCT TUPLE
data_tuple = (sw, ip, port, nat, voice, speed, desc)

# 4. PRINT RESULTS
labels = ["SW", "IP", "PORT", "NATIVE", "VOICE", "SPEED", "DESC"]
print("\n--- Final Data State ---")
for i in range(7):
    print(f"{labels[i]}: {data_tuple[i]}")

# 5. TEST THE MERGE LOGIC
vlan_line = f"VLAN: {data_tuple[3]}"
if data_tuple[4] != "N/A" and data_tuple[4] != data_tuple[3]:
    vlan_line += f" | V-V: {data_tuple[4]}"

print(f"\nFinal Display Line: {vlan_line}")
