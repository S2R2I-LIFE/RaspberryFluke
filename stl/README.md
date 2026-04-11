# Pi-Fluke Case Lid

A custom 3D-printed lid and bezel designed to turn a Raspberry Pi Zero into a sleek, professional, PoE-powered network discovery tool (LLDP/CDP). 

This model is designed to act as a replacement lid that seamlessly integrates with the factory bottom casing provided in the **Waveshare Pi Zero PoE HAT** kit. When paired with a Waveshare 2.13" e-Paper display, it creates a rugged, pocket-sized tester perfect for network engineers and IT professionals.

## ✨ Design Features
* **Perfect Fit:** Features beveled edging designed to snap directly into the original plastic bottom casing that comes with the Waveshare PoE kit.
* **Port Clearances:** One side of the interior is specifically inset to compensate for the mini-HDMI and micro-USB power inputs on the Raspberry Pi Zero board, ensuring a flush fit without stressing the PCB.
* **Screen Framing:** Perfectly frames the 2.13" e-Paper display, hiding the ribbon cable and PCB bezels for a commercial-grade look.
* **Custom Branding:** Features a sleek "Pi-Fluke" inset text on the front bezel.

## 🖨️ Recommended Print Settings
This model was tested and optimized on a **Creality K1** using the **CFS (Creality Filament System)** for the multi-color text and two-tone aesthetic. You do not need a CFS to get the color for the lid if you are able to swap the filament it is only a single layer that gives it the pop color!

* **Printer Used:** Creality K1 with CFS upgrade
* **Nozzle Size:** 0.4 mm
* **Material:** PETG *(Highly recommended over PLA, as the PoE HAT and Pi Zero can generate warm temperatures during continuous use).*
* **Infill:** 15%
* **Supports:** No supports are required depending on your bridging capabilities for the lid clip housing or how you decide to print.

### 🎨 The Multi-Color / Border Slicer Trick
To achieve the exact text and inner screen border look seen in the photos, a specific layering trick was used:
* A single **0.2mm layer** was placed on the bottom of the inset for the "Pi-Fluke" text.
* Simply take the color tool and using a **0.4mm layer height line** cover that inset layer so that there is a single 0.4mm layer around the entire lid. 
* This technique covers the 0.2mm layer, resulting in a unique "mixed coloring" look for the top bezel, while leaving a perfectly defined text and accent stripe around the inside edge of the screen bezel!

## 🛠️ Hardware Used in this Build
To complete this build as pictured, you will need:
1. **Raspberry Pi Zero 2 W**
2. **Waveshare PoE/ETH USB HUB HAT** (specifically the kit that includes the white/black plastic bottom tub)
3. **Waveshare 2.13" e-Paper HAT** (V4 - Red/Black/White or Black/White)

## ⚙️ Assembly Notes
1. Assemble the Pi Zero and PoE HAT into the original Waveshare bottom case.
2. Attach the e-Paper display to the GPIO pins.
3. Gently press the 3D-printed Pi-Fluke lid over the top. Ensure the side with the internal inset is aligned with the Pi Zero's USB and HDMI ports so the board isn't pinched. 
4. The beveled edges should sit cleanly against the factory bottom case.
