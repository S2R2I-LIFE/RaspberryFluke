import subprocess
import time
import threading
import signal
import logging
import re
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13b_V4

# ============================================================
# -------------------- CONFIGURATION -------------------------
# ============================================================
DISPLAY_WIDTH = 250
DISPLAY_HEIGHT = 122
LEFT_MARGIN = 10
TOP_MARGIN = 4

# Reduced to 14 to comfortably fit 7 total lines
BASE_FONT_SIZE = 14
MIN_FONT_SIZE = 10
LINE_SPACING = 2

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
IFACE = "eth0"

POLL_INTERVAL_SECONDS = 1
SUBPROCESS_TIMEOUT_SECONDS = 3
NO_NEIGHBOR_TIMEOUT_SECONDS = 180
MIN_DISPLAY_UPDATE_INTERVAL_SECONDS = 15
# PARTIAL_REFRESH_LIMIT = 8

# ============================================================
# -------------------- LOGGING -------------------------------
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("raspberryfluke")

# ============================================================
# -------------------- FONT PRELOAD / CACHE ------------------
# ============================================================
FONT_CACHE = {
    s: ImageFont.truetype(FONT_PATH, s)
    for s in range(MIN_FONT_SIZE, BASE_FONT_SIZE + 1)
}

# ============================================================
# -------------------- SHARED STATE --------------------------
# ============================================================
# 10 Items: SW, IP, PORT, NATIVE, VOICE, SPEED, DESC, POE, MODEL, OS
current_data = ("Loading", "...", "...", "...", "...", "...", "...", "...", "...", "...")
data_lock = threading.Lock()
data_event = threading.Event()
shutdown_event = threading.Event()

# ============================================================
# -------------------- UTILITY FUNCTIONS ---------------------
# ============================================================
def run(cmd):
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        ).decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""

def clean_hostname(name):
    return name.split(".")[0] if "." in name else name

def shorten_interface(intf):
    replacements = {
        "GigabitEthernet": "Gi",
        "TenGigabitEthernet": "Te",
        "FastEthernet": "Fa",
    }
    for long, short in replacements.items():
        if intf.startswith(long):
            return intf.replace(long, short)
    return intf

def fit_font(draw, text, max_width):
    for size in range(BASE_FONT_SIZE, MIN_FONT_SIZE - 1, -1):
        font = FONT_CACHE[size]
        if draw.textlength(text, font=font) <= max_width:
            return font
    return FONT_CACHE[MIN_FONT_SIZE]

def _first_value_for_keys(kv, keys):
    for k in keys:
        v = kv.get(k, "")
        if v: return v
    return ""

def _find_first_match_value(kv, pattern):
    rx = re.compile(pattern)
    for k, v in kv.items():
        if rx.search(k) and v: return v
    return ""

def _normalize_vlan(v):
    v = (v or "").strip()
    if not v: return "N/A"
    m = re.search(r"\b(\d{1,4})\b", v)
    return m.group(1) if m else v

# ============================================================
# -------------------- DISCOVERY PARSING ---------------------
# ============================================================
def parse_lldp_keyvalue():
    kv = {}
    out = run(["lldpctl", "-f", "keyvalue"])
    if not out: return kv
    k_counts = {}
    for line in out.splitlines():
        if "=" not in line: continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k not in k_counts:
            k_counts[k] = 0
            kv[k] = v
        else:
            k_counts[k] += 1
            kv[f"{k}._{k_counts[k]}"] = v
    return kv

def extract_switch_hostname(kv):
    sw = kv.get(f"lldp.{IFACE}.chassis.name", "")
    return clean_hostname(sw) if sw else "N/A"

def extract_switch_ip(kv):
    candidates = [
        f"lldp.{IFACE}.chassis.mgmt-ip",
        f"lldp.{IFACE}.chassis.mgmt-ip.0",
        f"lldp.{IFACE}.chassis.mgmt-ip.1",
    ]
    ip = _first_value_for_keys(kv, candidates)
    if not ip:
        ip = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.chassis\.mgmt-ip(\.|$)")
    ip = (ip or "").strip()
    return ip if ip else "N/A"

def extract_port(kv):
    port = kv.get(f"lldp.{IFACE}.port.ifname", "") or kv.get(f"lldp.{IFACE}.port.descr", "")
    return shorten_interface(port) if port else "N/A"

def extract_port_speed(kv):
    def format_speed(val):
        if not val: return None
        val = str(val).lower()
        if "10000" in val or "10gbase" in val: return "10G"
        if "1000base" in val or val == "1000" or "1g" in val: return "1G"
        if "100base" in val or val == "100": return "100M"
        if "10base" in val or val == "10": return "10M"
        if "2500" in val or "2.5g" in val: return "2.5G"
        if "5000" in val or "5g" in val: return "5G"
        if "40g" in val: return "40G"
        if "100g" in val: return "100G"
        return None

    speed = kv.get(f"lldp.{IFACE}.port.speed", "")
    if speed:
        fmt = format_speed(speed)
        if fmt: return fmt

    mau_key = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.mac\.mau")
    if mau_key:
        fmt = format_speed(mau_key)
        if fmt: return fmt

    out = run(["lldpctl"])
    if out:
        m = re.search(r"Operational MAU Type\s*:\s*([A-Za-z0-9\-]+)", out, flags=re.IGNORECASE)
        if m:
            fmt = format_speed(m.group(1))
            if fmt: return fmt

    return "N/A"

def extract_port_description(kv):
    descr = kv.get(f"lldp.{IFACE}.port.descr", "").strip()
    if not descr: return "N/A"
    return (descr[:20] + "...") if len(descr) > 20 else descr

def extract_poe(kv):
    patterns = [
        rf"^lldp\.{re.escape(IFACE)}\.port\.power\.allocated",
        rf"^lldp\.{re.escape(IFACE)}\.port\.power\.requested",
        rf"^lldp\.{re.escape(IFACE)}\.med\.power\.allocated",
        rf"^lldp\.{re.escape(IFACE)}\.lldp-med\.poe\.power"
    ]
    for p in patterns:
        power_str = _find_first_match_value(kv, p)
        if power_str:
            try:
                watts = float(power_str) / 1000.0
                if watts > 0:
                    return f"{watts:.1f}W"
            except ValueError:
                pass
    return "N/A"

def extract_model_and_os(kv):
    """
    Parses the chassis description string for vendor-specific hardware and OS versions.
    """
    descr = kv.get(f"lldp.{IFACE}.chassis.descr", "")
    if not descr:
        return ("N/A", "N/A")
        
    model = "Unknown"
    os_ver = "Unknown"
    
    if "Arista" in descr:
        m = re.search(r"running on an (?:Arista Networks )?(.*)", descr)
        if m: model = m.group(1).strip()
        v = re.search(r"version ([\w\.-]+)", descr)
        if v: os_ver = f"EOS {v.group(1)}"
        
    elif "Juniper" in descr:
        m = re.search(r"Inc\.\s+(.*?)\s+(?:Ethernet Switch|kernel)", descr)
        if m: model = m.group(1).strip().strip(',')
        v = re.search(r"JUNOS ([\w\.-]+)", descr)
        if v: os_ver = f"JUNOS {v.group(1)}"
        
    elif "Cisco" in descr or "cisco" in descr:
        m = re.search(r"(?:cisco|Cisco)\s+([a-zA-Z0-9-]{5,})", descr)
        if m: model = m.group(1).strip()
        v = re.search(r"Version ([\w\.\(\)]+)", descr)
        if v: os_ver = f"IOS {v.group(1)}"
        
    if model == "Unknown":
        model = (descr[:18] + "..") if len(descr) > 20 else descr
    if os_ver == "Unknown":
        os_ver = "N/A"
        
    return (model, os_ver)

def extract_vlans(kv):
    all_vlans = set()
    for k, v in kv.items():
        if "vlan-id" in k or ".vid" in k or re.search(rf"^lldp\.{re.escape(IFACE)}\.vlan\.", k):
            norm = _normalize_vlan(v)
            if norm and norm != "N/A":
                all_vlans.add(norm)
                
    native = None
    voice = None

    # STRICT NATIVE (PVID)
    for k, v in kv.items():
        if "pvid" in k.lower():
            if str(v).isdigit(): 
                native = _normalize_vlan(v)
            elif str(v).lower() in ["yes", "true", "1"]:
                id_key1 = k.replace("pvid", "vlan-id")
                id_key2 = k.replace("pvid", "vid")
                if id_key1 in kv: native = _normalize_vlan(kv[id_key1])
                elif id_key2 in kv: native = _normalize_vlan(kv[id_key2])
                
    if not native:
        out = run(["lldpctl"])
        if out:
            for line in out.splitlines():
                if "pvid" in line.lower():
                    m = re.search(r"\b(\d{1,4})\b", line)
                    if m: native = _normalize_vlan(m.group(1))

    # STRICT VOICE
    for k, v in kv.items():
        if "apptype" in k.lower() and "voice" in str(v).lower():
            id_key = k.replace("apptype", "vlan.vid")
            if id_key in kv:
                voice = _normalize_vlan(kv[id_key])
                break
                
    if not voice:
        patterns = [
            rf"^lldp\.{re.escape(IFACE)}\..*med\.policy.*(voice|application).*(vlan-id|vid|vlan)",
            rf"^lldp\.{re.escape(IFACE)}\..*med\.policy.*(vlan-id|vid|vlan\.vid)",
            rf"^lldp\.{re.escape(IFACE)}\..*(cdp|aux).*(voice|vlan)"
        ]
        for p in patterns:
            v_match = _find_first_match_value(kv, p)
            if v_match: 
                voice = _normalize_vlan(v_match)
                break
                
    if not voice:
        for k, v in kv.items():
            if "vlan-name" in k and "voice" in str(v).lower():
                for suffix in ["vlan-id", "vid"]:
                    id_key = k.replace("vlan-name", suffix)
                    if id_key in kv:
                        voice = _normalize_vlan(kv[id_key])
                        break
                        
    if not voice:
        out = run(["lldpctl"])
        if out:
            for p in [r"Voice\s+VLAN\s*:\s*(\d{1,4})", r"Auxiliary\s+VLAN\s*:\s*(\d{1,4})", r"Application\s+VLAN\s*:\s*(\d{1,4})", r"VLAN\s*:\s*(\d{1,4})\s*\(.*?(?:voice|aux).*?\)"]:
                m = re.search(p, out, flags=re.IGNORECASE)
                if m:
                    voice = _normalize_vlan(m.group(1))
                    break

    # BI-DIRECTIONAL PROCESS OF ELIMINATION
    if native and not voice:
        others = [v for v in all_vlans if v != native]
        if len(others) == 1: voice = others[0]
    elif voice and not native:
        others = [v for v in all_vlans if v != voice]
        if len(others) == 1: native = others[0]

    if not native and not voice and all_vlans:
        native = sorted(list(all_vlans))[0]

    return (native or "N/A", voice or "N/A")

def is_endpoint_device(kv):
    """
    Intelligently filters out IP Phones, Voice Controllers, and APs.
    Returns True if the data belongs to an endpoint, False if it's a switch.
    """
    # 1. Check standardized LLDP capabilities first
    for k, v in kv.items():
        if v == "on":
            # If it explicitly says it is a Switch (Bridge) or Router, allow it.
            if "Bridge.enabled" in k or "Router.enabled" in k:
                return False 
                
    for k, v in kv.items():
        if v == "on":
            # If it explicitly identifies as a Phone, AP, or Station, block it.
            if "Telephone.enabled" in k or "Wlan.enabled" in k or "Station.enabled" in k:
                return True   
                
    # 2. Heuristic fallback (CDP often relies on description strings instead of capabilities)
    for k, v in kv.items():
        if "descr" in k.lower() or "name" in k.lower():
            val = v.lower()
            # If it's running a known switch OS, allow it
            if "ios " in val or "junos" in val or "arista" in val or "nexus" in val or "catalyst" in val:
                return False
            # If it explicitly says phone or voice controller, block it
            if "phone" in val or "voice controller" in val or "polycom" in val or "access point" in val:
                return True
                
    # 3. Default to False (Allow) so we don't accidentally blind the tool to unknown switches
    return False

def get_switch_info():
    kv = parse_lldp_keyvalue()
    if is_endpoint_device(kv):
        return ("Loading", "...", "...", "...", "...", "...", "...", "...", "...", "...")
    sw = extract_switch_hostname(kv)
    sw_ip = extract_switch_ip(kv)
    port = extract_port(kv)
    speed = extract_port_speed(kv)
    descr = extract_port_description(kv)
    poe = extract_poe(kv)
    vlan, voice = extract_vlans(kv)
    model, os_ver = extract_model_and_os(kv)
    
    return (sw, sw_ip, port, vlan, voice, speed, descr, poe, model, os_ver)

def is_data_ready(data):
    sw, sw_ip, port, vlan, voice, speed, descr, poe, model, os_ver = data
    if sw in ("Loading", "N/A", ""): return False
    if port in ("...", "N/A", ""): return False
    return True

# ============================================================
# -------------------- BACKGROUND POLLER ---------------------
# ============================================================
def data_collector():
    global current_data
    last = None
    while not shutdown_event.is_set():
        try:
            new_data = get_switch_info()
            with data_lock:
                if new_data != current_data:
                    current_data = new_data
                    data_event.set()
            if last != new_data:
                log.info("Data update: SW=%s IP=%s P=%s V=%s VV=%s SP=%s D=%s PoE=%s M=%s OS=%s", *new_data)
                last = new_data
        except Exception as e:
            log.error(f"Critical error in data collector: {e}", exc_info=True)
            
        time.sleep(POLL_INTERVAL_SECONDS)

# ============================================================
# -------------------- DISPLAY RENDERING ---------------------
# ============================================================
def render_image(data):
    # Unpack 10 items
    sw, ip, port, native, voice, speed, descr, poe, model, os_ver = data
    
    image_black = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)
    image_red = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)
    
    draw_black = ImageDraw.Draw(image_black)
    draw_red = ImageDraw.Draw(image_red)
    
    # Line 1: SW
    line1 = [("SW: ", False), (sw, True)]
    # Line 2: IP
    line2 = [("IP: ", False), (ip, True)]
    
    # Line 3: Port / Speed / PoE
    line3 = [("P: ", False), (port, True)]
    if speed != "N/A":
        line3.extend([(" (", False), (speed, True), (")", False)])
    if poe != "N/A":
        line3.extend([(" | ", False), (poe, True)])
        
    # Line 4: Description
    line4 = [("D: ", False), (descr, True)]
    
    # Line 5: VLAN
    if native == "N/A" and voice == "N/A":
        line5 = [("VLAN: ", False), ("N/A", True)]
    elif native == "N/A" and voice != "N/A":
        line5 = [("V-V: ", False), (voice, True)]
    elif native != "N/A" and voice == "N/A":
        line5 = [("VLAN: ", False), (native, True)]
    else:
        if native == voice:
            line5 = [("VLAN: ", False), (native, True)]
        else:
            line5 = [("VLAN: ", False), (native, True), (" | V-V: ", False), (voice, True)]
            
    # Line 6: Model
    line6 = [("M: ", False), (model, True)]
    
    # Line 7: OS Version
    line7 = [("OS: ", False), (os_ver, True)]
            
    lines = [line1, line2, line3, line4, line5, line6, line7]
    
    y = TOP_MARGIN
    max_width = DISPLAY_WIDTH - (LEFT_MARGIN * 2)
    
    for line_spans in lines:
        full_text = "".join([span[0] for span in line_spans])
        font = fit_font(draw_black, full_text, max_width)
        
        x = LEFT_MARGIN
        for text, is_red in line_spans:
            if is_red:
                draw_red.text((x, y), text, font=font, fill=0)
            else:
                draw_black.text((x, y), text, font=font, fill=0)
            x += draw_black.textlength(text, font=font)
            
        y += font.size + LINE_SPACING
        
    return image_black.rotate(180), image_red.rotate(180)

def render_no_neighbor():
    return render_image(("NO NEIGHBOR", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"))

# ============================================================
# -------------------- SIGNAL HANDLING -----------------------
# ============================================================
def handle_shutdown(signum, frame):
    log.info("Shutdown requested (signal %s)", signum)
    shutdown_event.set()
    data_event.set()

# ============================================================
# -------------------- MAIN SERVICE LOOP ---------------------
# ============================================================
def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    
    epd = epd2in13b_V4.EPD() 
    
    last_display_update_mono = 0.0
    first_ready_displayed = False
    no_neighbor_displayed = False
    boot_start_mono = time.monotonic()
    last_displayed_snap = None
    
    try:
        # We skip the Loading screen and go straight to collecting data!
        log.info("Starting data collector, waiting for first neighbor...")
        
        threading.Thread(target=data_collector, daemon=True).start()
        
        while not shutdown_event.is_set():
            data_event.wait(timeout=1.0)
            data_event.clear()
            with data_lock:
                snap = current_data
            now_mono = time.monotonic()
            
            if not first_ready_displayed:
                if is_data_ready(snap):
                    epd.init()
                    img_b, img_r = render_image(snap)
                    epd.display(epd.getbuffer(img_b), epd.getbuffer(img_r))
                    epd.sleep()
                    
                    first_ready_displayed = True
                    no_neighbor_displayed = False
                    last_display_update_mono = now_mono
                    last_displayed_snap = snap
                    log.info("First neighbor displayed. Now monitoring changes.")
                    continue
                
                if (not no_neighbor_displayed) and ((now_mono - boot_start_mono) >= NO_NEIGHBOR_TIMEOUT_SECONDS):
                    epd.init()
                    img_b, img_r = render_no_neighbor()
                    epd.display(epd.getbuffer(img_b), epd.getbuffer(img_r))
                    epd.sleep()
                    
                    no_neighbor_displayed = True
                    last_display_update_mono = now_mono
                    log.warning("No neighbor after timeout.")
                continue
                
            if not is_data_ready(snap):
                continue
                
            if last_displayed_snap is not None and snap != last_displayed_snap:
                if (now_mono - last_display_update_mono) < MIN_DISPLAY_UPDATE_INTERVAL_SECONDS:
                    continue
                
                log.info("Data change detected: Full refresh starting...")
                epd.init()
                img_b, img_r = render_image(snap)
                epd.display(epd.getbuffer(img_b), epd.getbuffer(img_r))
                epd.sleep()
                
                last_display_update_mono = now_mono
                last_displayed_snap = snap
                log.info("Refresh complete.")
                
        try:
            epd.sleep()
        except Exception:
            pass
        log.info("Exited cleanly.")
    except Exception:
        try:
            epd.sleep()
        except Exception:
            pass
        raise

if __name__ == "__main__":
    main()
