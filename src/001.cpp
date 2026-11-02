#include <Arduino.h>
#include <SPI.h>

#define CS_PIN 5

#define CMD_WREN         0x06
#define CMD_ULBPR        0x98
#define CMD_JEDEC_ID     0x9F
#define CMD_READ_STATUS  0x05
#define CMD_READ_CONFIG  0x35

SPISettings spiSettings(10000000, MSBFIRST, SPI_MODE0);

void writeEnable() {
  SPI.beginTransaction(spiSettings);
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_WREN);
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
}

void processCommand(String cmd) {
  cmd.trim();
  
  if (cmd == "READ_ID") {
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_JEDEC_ID);
    uint8_t mfg  = SPI.transfer(0x00);
    uint8_t type = SPI.transfer(0x00);
    uint8_t id   = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    Serial.printf("ID:%02X %02X %02X\n", mfg, type, id);
  }
  else if (cmd == "UNLOCK") {
    writeEnable();
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_ULBPR);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    delay(10);
    Serial.println("UNLOCK:OK");
  }
  else if (cmd == "READ_REGS") {
    // อ่าน Status Register (0x05)
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    uint8_t stat = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    // อ่าน Configuration Register (0x35)
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_CONFIG);
    uint8_t cfg = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.printf("REGS:%02X:%02X\n", stat, cfg);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);
  SPI.begin(18, 19, 23, CS_PIN);
  Serial.println("ESP32_READY");
}

void loop() {
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    processCommand(input);
  }
}