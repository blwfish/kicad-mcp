# Firmware front end — what it can and cannot handle (today)

The `design` tool turns an embedded-firmware project into a design intent →
schematic → routed PCB. It works within a **deliberately narrow envelope**, and
**outside that envelope it fails loud** (explicit, user-visible gaps) — it never
silently produces a board with the wrong pinout or a missing part. This page is
the authoritative scope statement; if the tool surprises you, check it here first.

## What it CAN handle today

### MCUs (exactly two)

| Part | Matches PlatformIO board ids | Notes |
|------|------------------------------|-------|
| `ESP32-WROOM-32E` | `esp32dev`, `esp32` | classic ESP32; USB-serial programming via CP2102 |
| `ESP32-S3-WROOM-1` | `esp32-s3-devkitc-1`, `esp32-s3`, `esp32s3` | native USB |

Any other board id — including ESP32-**C3 / C6 / S2**, and every non-ESP32 chip —
resolves to an `mcu_unknown` gap (no MCU placed).

### Project layout — PlatformIO only

- The **board** is read from `platformio.ini` (`board = ...`).
- **Pin and address definitions** are read from a file named **`config.h`**.

No other project layout is recognized (see "cannot handle" below).

### Firmware language — C/C++ preprocessor macros only

A pin is recognized when its `#define` **name** ends in `_PIN` / `_GPIO`, is a
known bare alias (`I2C_SDA` / `I2C_SCL`), or ends in a role token (`SDA`, `SCL`,
`BCLK`, `WS`, `LRCK`, `MOSI`, `MISO`, `DIN`, `DOUT`, `SCK`, `CS`, …). An I2C
device is recognized from a `<TYPE>_ADDR` / `_ADDRESS` macro. The macro's *value*
never classifies it — only the name does.

```c
// understood:
#define I2C_SDA_PIN     21
#define I2C_SCL_PIN     22
#define MPU6050_ADDR    0x68
#define HX711_DOUT_PIN  16
```

### Recognized peripheral chips (11 cards)

Recognized devices are placed and wired; anything else becomes a labeled gap.

| Firmware name (aliases) | Bus | Realized as |
|-------------------------|-----|-------------|
| `HX711` | — | chip-down IC |
| `MCP23017` | I2C | chip-down IC (+ optional `expander_terminals` → labeled GPA/GPB terminals) |
| `MAX98357A` | I2S | chip-down IC |
| `ICS-43434` (`ICS43434`) | I2S | chip-down IC |
| `SPH0645` (`SPH0645LM4H`) | I2S | chip-down IC |
| `INMP441` (`INMP441ACEZ`, `GY-INMP441`) | I2S | recognized, but **needs a `board.yaml` part override to build** |
| `MPU6050` | I2C | breakout-module header (GY-521) |
| `OLED` | I2C | breakout-module header (SSD1306) |
| `PIEZO` (`BUZZER`) | — | screw terminal (off-board) |
| `SWITCH` (`TRACK`, `LIMIT`, `REED`) | — | screw terminal (off-board) |
| `TCRT5000` | — | screw terminal (off-board) |

Facts firmware structurally can't declare — power source, board size, connectors,
placement — are supplied via a `board.yaml` sidecar.

## What it CANNOT handle today — and what happens

| Out of envelope | What the tool does |
|-----------------|--------------------|
| Any MCU other than the two above (ESP32-C3/C6/S2, RP2040/Pico, AVR/Arduino, STM32, …) | `mcu_unknown` gap; no MCU placed (peripheral nets still emitted) |
| **Arduino IDE** sketches (`.ino`; board chosen in the GUI; no `platformio.ini`) | board id not found → the MCU can't be determined |
| **Pico SDK / CMake**, bare Makefile, or any non-PlatformIO layout | same — no `platformio.ini` to read the board from |
| Pins/addresses in a file **not named `config.h`** (the `.ino` itself, `main.cpp`, …) | `config_not_found` — nothing imported |
| **MicroPython / CircuitPython** (Python: `machine.Pin(5)`, `board.GP5`) | not a C `#define` → pins are not extracted |
| **Rust**, or C that assigns pins via **runtime calls** (`gpio_init(5)`) instead of `#define` | same — pins not extracted |
| A peripheral chip with no card | `unknown_peripheral` gap; its signals become orphan nets (far end left open) |

## The guarantee: honest-by-construction

Every build also *always* lists what firmware can't declare — `power_tree`,
`decoupling`, `pullups`, `connectors`, `parts` — as gaps. **The gaps are the
to-do list.** A human or agent then supplies the missing facts (a new device
card, a `board.yaml` entry, a part override) or knowingly accepts the gap.

The point: the envelope is narrow, but the failure mode is *loud and recoverable*,
not a silently-wrong board. Expanding the envelope (more MCUs, Arduino-IDE and
non-C firmware, more chips) is planned but not yet shipped.
