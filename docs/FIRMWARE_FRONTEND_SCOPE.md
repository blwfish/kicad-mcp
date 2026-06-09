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

### Project layout — PlatformIO `config.h` or Arduino sketch

- The **board** is read from `platformio.ini` (`board = ...`), or — when there's
  no `platformio.ini`, or its board isn't recognized — from a `board.yaml`
  **`board_id`** (a board id like `esp32dev` or an Arduino FQBN like
  `esp32:esp32:esp32`). The sidecar `board_id` wins over platformio.
- **Pin and address definitions** are read from a `config.h`, **or** from an
  Arduino **`.ino` sketch** — point the tool at the sketch folder and all its
  `.ino` tabs are concatenated the way the IDE builds them.

### Firmware language — C/C++ `#define` and `const`/`constexpr`

A pin is recognized when its **name** ends in `_PIN` / `_GPIO`, is a known bare
alias (`I2C_SDA` / `I2C_SCL`), or ends in a role token (`SDA`, `SCL`, `BCLK`,
`WS`, `LRCK`, `MOSI`, `MISO`, `DIN`, `DOUT`, `SCK`, `CS`, …) — whether declared
with a `#define` or a `const` / `constexpr` integer (how Arduino sketches name
pins). An I2C device is recognized from a `<TYPE>_ADDR` / `_ADDRESS` name. The
*value* never classifies it — only the name does.

```c
// understood (config.h or .ino):
#define I2C_SDA_PIN       21
const int HX711_DOUT_PIN = 16;
constexpr uint8_t I2C_SCL = 22;
#define MPU6050_ADDR      0x68
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
| A board with **no `platformio.ini` and no `board.yaml` `board_id`** | board id not found → `mcu_unknown` (declare it in `board.yaml`) |
| Firmware that's neither a `config.h` nor an Arduino `.ino` sketch (Pico-SDK `main.c`, bare Makefile, …) | `config_not_found` — nothing imported |
| Pins assigned by **runtime calls** — `pinMode(5, OUTPUT)`, `Wire.begin(21,22)`, constructor args — instead of a `#define` / `const` | pins not extracted (the value isn't bound to a pin-named symbol) |
| **MicroPython / CircuitPython** (`machine.Pin(5)`, `board.GP5`) | not C/C++ → pins are not extracted |
| **Rust** firmware | same — pins not extracted |
| A peripheral chip with no card | `unknown_peripheral` gap; its signals become orphan nets (far end left open) |

## The guarantee: honest-by-construction

Every build also *always* lists what firmware can't declare — `power_tree`,
`decoupling`, `pullups`, `connectors`, `parts` — as gaps. **The gaps are the
to-do list.** A human or agent then supplies the missing facts (a new device
card, a `board.yaml` entry, a part override) or knowingly accepts the gap.

The point: the envelope is narrow, but the failure mode is *loud and recoverable*,
not a silently-wrong board. Arduino-IDE sketches (`.ino` + `const`, board via
`board.yaml`) now import; further expansion — more MCUs, runtime-call/non-C pin
extraction, more chips — is planned but not yet shipped.
