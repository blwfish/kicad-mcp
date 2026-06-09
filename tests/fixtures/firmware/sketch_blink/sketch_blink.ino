// Arduino-IDE sketch (no platformio.ini): pins via #define AND const/constexpr.
// Board comes from the board.yaml sidecar's board_id escape hatch.
#define LED_PIN 2
const int I2C_SDA = 21;
constexpr uint8_t I2C_SCL = 22;
const int SAMPLE_INTERVAL_MS = 1000;   // config, not a pin

void setup() {}
void loop() {}
