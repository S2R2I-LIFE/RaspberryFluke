This is a classic E-Ink display issue known as **"Ghosting" or "Stacking"**. 

### Why this happened:
E-Ink screens do partial refreshes by calculating the difference between the *old* image and the *new* image. When your script puts the display to `epd.sleep()` to save the screen's lifespan, the internal RAM on the display controller clears itself. 
When it wakes up and you send a new `displayPartial()` command, the screen thinks the background is completely blank. As a result, it draws the new black text directly over the old black text without erasing it, resulting in a garbled mess of "N/A" and LLDP stats!

### The Fix:
We need to keep a copy of the `last_rendered_img` in the script's memory. When the screen wakes up to do a partial refresh, we must first use `epd.displayPartBaseImage()` to remind the screen what is currently physically on it, and *then* run the partial update.

I also updated the "Change Detection" logic. Previously, it only updated the screen if the VLAN changed. I have updated it so that **if ANY of the 7 items change (Speed, Description, IP, etc.), the screen will automatically update.**

Here is the fully corrected code. You can copy and paste this entire block over your existing script:

```python
import subprocess
import time
import threading
import signal
import logging
import re
from PIL import Image, ImageDraw, ImageFont
from waveshare_epd import epd2in13_V3

# ============================================================
# -------------------- CONFIGURATION -------------------------
# ============================================================
DISPLAY_WIDTH = 250
DISPLAY_HEIGHT = 122
LEFT_MARGIN = 10
TOP_MARGIN = 4

BASE_FONT_SIZE = 16
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
current_data = ("Loading", "...", "...", "...", "...", "...", "...")
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
    for line in out.splitlines():
        if "=" not in line: continue
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip()
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
    speed = kv.get(f"lldp.{IFACE}.port.speed", "")
    if speed:
        if speed == "1000": return "1G"
        if speed == "10000": return "10G"
        return f"{speed}M"
    return "N/A"

def extract_port_description(kv):
    descr = kv.get(f"lldp.{IFACE}.port.descr", "").strip()
    if not descr: return "N/A"
    return (descr[:20] + "...") if len(descr) > 20 else descr

def extract_native_vlan(kv):
    val = kv.get(f"lldp.{IFACE}.vlan.vlan-id", "")
    if not val:
        val = kv.get(f"lldp.{IFACE}.port.vlan-id", "")
    return _normalize_vlan(val)

def extract_voice_vlan(kv):
    patterns = [
        rf"^lldp\.{re.escape(IFACE)}\..*med\.policy.*(voice|application).*vlan",
        rf"^lldp\.{re.escape(IFACE)}\..*med\.policy.*vlan-id"
    ]
    for p in patterns:
        v = _find_first_match_value(kv, p)
        if v: return _normalize_vlan(v)
            
    v = _find_first_match_value(kv, rf"^lldp\.{re.escape(IFACE)}\..*(cdp|aux).*(voice|vlan)")
    if v: return _normalize_vlan(v)
    
    out = run(["lldpctl"])
    if out:
        patterns = [
            r"Voice\s+VLAN\s*:\s*(\d{1,4})",
            r"Auxiliary\s+VLAN\s*:\s*(\d{1,4})",
            r"Application\s+VLAN\s*:\s*(\d{1,4})",
            r"\bvoice\b.*\bVLAN\b.*?(\d{1,4})"
        ]
        for p in patterns:
            m = re.search(p, out, flags=re.IGNORECASE)
            if m: return _normalize_vlan(m.group(1))
    return "N/A"

def get_switch_info():
    kv = parse_lldp_keyvalue()
    sw = extract_switch_hostname(kv)
    sw_ip = extract_switch_ip(kv)
    port = extract_port(kv)
    vlan = extract_native_vlan(kv)
    voice = extract_voice_vlan(kv)
    speed = extract_port_speed(kv)
    descr = extract_port_description(kv)
    return (sw, sw_ip, port, vlan, voice, speed, descr)

def is_data_ready(data):
    sw, sw_ip, port, vlan, voice, speed, descr = data
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
        new_data = get_switch_info()
        with data_lock:
            if new_data != current_data:
                current_data = new_data
                data_event.set()
        if last != new_data:
            log.info("Data update: SW=%s IP=%s PORT=%s VLAN=%s VOICE=%s SPEED=%s DESC=%s", *new_data)
            last = new_data
        time.sleep(POLL_INTERVAL_SECONDS)

# ============================================================
# -------------------- DISPLAY RENDERING ---------------------
# ============================================================
def render_image(data):
    image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    
    vlan_str = f"VLAN: {data[3]}"
    if data[4] != "N/A" and data[4] != data[3]:
        vlan_str += f" | V-V: {data[4]}"
    
    lines = [
        f"SW: {data[0]}",
        f"IP: {data[1]}",
        f"P: {data[2]} ({data[5]})", 
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
    return render_image(("NO NEIGHBOR", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"))

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
    epd = epd2in13_V3.EPD()
    partial_refresh_count = 0
    last_display_update_mono = 0.0
    
    first_ready_displayed = False
    no_neighbor_displayed = False
    boot_start_mono = time.monotonic()
    
    # TRACK STATE FOR PROPER E-INK REFRESHES
    last_displayed_snap = None
    last_rendered_img = None
    
    try:
        epd.init()
        epd.Clear(0xFF)
        boot_img = render_image(("Loading", "...", "...", "...", "...", "...", "..."))
        epd.display(epd.getbuffer(boot_img))
        last_rendered_img = boot_img
        log.info("Displayed boot screen (Loading)")
        
        threading.Thread(target=data_collector, daemon=True).start()
        
        while not shutdown_event.is_set():
            data_event.wait(timeout=1.0)
            data_event.clear()
            with data_lock:
                snap = current_data
            now_mono = time.monotonic()
            
            # --- PHASE 1: BOOTING / WAITING FOR FIRST NEIGHBOR ---
            if not first_ready_displayed:
                if is_data_ready(snap):
                    epd.init()
                    epd.Clear(0xFF)
                    img = render_image(snap)
                    epd.display(epd.getbuffer(img))
                    epd.sleep()
                    
                    first_ready_displayed = True
                    no_neighbor_displayed = False
                    partial_refresh_count = 0
                    last_display_update_mono = now_mono
                    last_displayed_snap = snap
                    last_rendered_img = img
                    log.info("First neighbor displayed. Now monitoring changes.")
                    continue
                
                if (not no_neighbor_displayed) and ((now_mono - boot_start_mono) >= NO_NEIGHBOR_TIMEOUT_SECONDS):
                    epd.init()
                    epd.Clear(0xFF)
                    img = render_no_neighbor()
                    epd.display(epd.getbuffer(img))
                    epd.sleep()
                    
                    no_neighbor_displayed = True
                    partial_refresh_count = 0
                    last_display_update_mono = now_mono
                    last_rendered_img = img
                    log.warning("No neighbor after %ss; displayed NO NEIGHBOR screen.", NO_NEIGHBOR_TIMEOUT_SECONDS)
                continue
                
            # --- PHASE 2: CONTINUOUS MONITORING ---
            if not is_data_ready(snap):
                continue
                
            # Trigger an update if ANY of the 7 data points change
            if last_displayed_snap is not None and snap != last_displayed_snap:
                if (now_mono - last_display_update_mono) < MIN_DISPLAY_UPDATE_INTERVAL_SECONDS:
                    continue
                
                epd.init()
                img = render_image(snap)
                
                if partial_refresh_count >= PARTIAL_REFRESH_LIMIT or last_rendered_img is None:
                    epd.Clear(0xFF)
                    epd.display(epd.getbuffer(img))
                    partial_refresh_count = 0
                    log.info("Data change: full refresh.")
                else:
                    try:
                        # CRITICAL FIX: Remind display of the previous image before drawing new ones
                        epd.displayPartBaseImage(epd.getbuffer(last_rendered_img))
                        epd.displayPartial(epd.getbuffer(img))
                        partial_refresh_count += 1
                        log.info("Data change: partial refresh.")
                    except AttributeError:
                        # Fallback if library version doesn't support BaseImage
                        epd.Clear(0xFF)
                        epd.display(epd.getbuffer(img))
                        partial_refresh_count = 0
                    except Exception:
                        epd.Clear(0xFF)
                        epd.display(epd.getbuffer(img))
                        partial_refresh_count = 0
                        log.warning("Partial refresh failed; full refresh used.", exc_info=True)
                
                epd.sleep()
                last_display_update_mono = now_mono
                last_displayed_snap = snap
                last_rendered_img = img
                
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
```
