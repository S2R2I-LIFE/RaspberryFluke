import subprocess
import time
import threading
import signal
import logging
import re
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V4

# ============================================================
# -------------------- CONFIGURATION -------------------------
# ============================================================
DISPLAY_WIDTH = 250
DISPLAY_HEIGHT = 122
LEFT_MARGIN = 10
TOP_MARGIN = 4

BASE_FONT_SIZE = 12
MIN_FONT_SIZE = 10
LINE_SPACING = 2

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
IFACE = "eth0"

POLL_INTERVAL_SECONDS = 1
SUBPROCESS_TIMEOUT_SECONDS = 3
NO_NEIGHBOR_TIMEOUT_SECONDS = 180
MIN_DISPLAY_UPDATE_INTERVAL_SECONDS = 10
PARTIAL_REFRESH_LIMIT = 8

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
# 8 Items: (SW, IP, PORT, NATIVE, VOICE, SPEED, DESC, POE)
current_data = ("Loading", "...", "...", "...", "...", "...", "...", "...")
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
    """
    Parses lldpctl output into a dictionary. 
    Includes a deduplication trick to handle Juniper switches that output 
    duplicate keys without indices (e.g., multiple 'vlan.vlan-id' keys).
    """
    kv = {}
    out = run(["lldpctl", "-f", "keyvalue"])
    if not out: return kv
    
    k_counts = {}
    for line in out.splitlines():
        if "=" not in line: continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        
        # Prevent Python from overwriting Native VLAN with Voice VLAN
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
    """
    Extract PoE power allocation, convert from milliwatts to Watts.
    """
    power_str = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.port\.power\.allocated")
    if not power_str:
        power_str = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.port\.power\.requested")
    if not power_str:
        power_str = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.med\.power\.allocated")
    if not power_str:
        power_str = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\.lldp-med\.poe\.power")

    if power_str:
        try:
            watts = float(power_str) / 1000.0
            return f"{watts:.1f}W"
        except ValueError:
            pass
            
    return "N/A"

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

    # ABSOLUTE FALLBACK
    if not native and not voice and all_vlans:
        native = sorted(list(all_vlans))[0]

    return (native or "N/A", voice or "N/A")

def get_switch_info():
    kv = parse_lldp_keyvalue()
    sw = extract_switch_hostname(kv)
    sw_ip = extract_switch_ip(kv)
    port = extract_port(kv)
    speed = extract_port_speed(kv)
    descr = extract_port_description(kv)
    poe = extract_poe(kv)
    vlan, voice = extract_vlans(kv)
    
    return (sw, sw_ip, port, vlan, voice, speed, descr, poe)

def is_data_ready(data):
    sw, sw_ip, port, vlan, voice, speed, descr, poe = data
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
                log.info("Data update: SW=%s IP=%s PORT=%s VLAN=%s VOICE=%s SPEED=%s DESC=%s POE=%s", *new_data)
                last = new_data
        except Exception as e:
            log.error(f"Critical error in data collector: {e}", exc_info=True)
            
        time.sleep(POLL_INTERVAL_SECONDS)

# ============================================================
# -------------------- DISPLAY RENDERING ---------------------
# ============================================================
def render_image(data):
    image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    
    port_str = f"P: {data[2]}"
    if data[5] != "N/A":
        port_str += f" ({data[5]})"
    if data[7] != "N/A":
        port_str += f" | {data[7]}"
        
    native = data[3]
    voice = data[4]
    
    if native == "N/A" and voice == "N/A":
        vlan_str = "VLAN: N/A"
    elif native == "N/A" and voice != "N/A":
        vlan_str = f"V-V: {voice}"
    elif native != "N/A" and voice == "N/A":
        vlan_str = f"VLAN: {native}"
    else:
        if native == voice:
            vlan_str = f"VLAN: {native}"
        else:
            vlan_str = f"VLAN: {native} | V-V: {voice}"
            
    lines = [
        f"SW: {data[0]}",
        f"IP: {data[1]}",
        port_str,
        f"D: {data[6]}",
        vlan_str,
    ]
    
    y = TOP_MARGIN
    max_width = DISPLAY_WIDTH - (LEFT_MARGIN * 2)
    
    for line in lines:
        font = fit_font(draw, line, max_width)
        draw.text((LEFT_MARGIN, y), line, font=font, fill=0)
        y += font.size + LINE_SPACING
        
    return image.rotate(180)

def render_no_neighbor():
    return render_image(("NO NEIGHBOR", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"))

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
    
    # NOTE: Use the 'b' version here!
    epd = epd2in13b_V4.EPD() 
    
    last_display_update_mono = 0.0
    first_ready_displayed = False
    no_neighbor_displayed = False
    boot_start_mono = time.monotonic()
    last_displayed_snap = None
    
    # Create an empty RED image (so we only print in Black)
    red_image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)
    red_buffer = epd.getbuffer(red_image.rotate(180))
    
    try:
        epd.init()
        epd.Clear()
        
        boot_img = render_image(("Loading", "...", "...", "...", "...", "...", "...", "..."))
        # 3-color displays take TWO buffers: Black and Red
        epd.display(epd.getbuffer(boot_img), red_buffer)
        log.info("Displayed boot screen (Loading)")
        
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
                    img = render_image(snap)
                    epd.display(epd.getbuffer(img), red_buffer)
                    epd.sleep()
                    
                    first_ready_displayed = True
                    no_neighbor_displayed = False
                    last_display_update_mono = now_mono
                    last_displayed_snap = snap
                    log.info("First neighbor displayed. Now monitoring changes.")
                    continue
                
                if (not no_neighbor_displayed) and ((now_mono - boot_start_mono) >= NO_NEIGHBOR_TIMEOUT_SECONDS):
                    epd.init()
                    img = render_no_neighbor()
                    epd.display(epd.getbuffer(img), red_buffer)
                    epd.sleep()
                    
                    no_neighbor_displayed = True
                    last_display_update_mono = now_mono
                    log.warning("No neighbor after %ss; displayed NO NEIGHBOR screen.", NO_NEIGHBOR_TIMEOUT_SECONDS)
                continue
                
            if not is_data_ready(snap):
                continue
                
            if last_displayed_snap is not None and snap != last_displayed_snap:
                # Give it at least 15-20 seconds between updates since the hardware is slow
                if (now_mono - last_display_update_mono) < 20.0:
                    continue
                
                log.info("Data change detected: Full refresh starting (Takes ~15s)...")
                epd.init()
                img = render_image(snap)
                
                # Full refresh with Black and Red buffers
                epd.display(epd.getbuffer(img), red_buffer)
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
