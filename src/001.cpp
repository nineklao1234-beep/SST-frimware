#include <Arduino.h>
#include <SPI.h>

#define CS_PIN 5

#define CMD_WREN         0x06
#define CMD_ULBPR        0x98
#define CMD_JEDEC_ID     0x9F
#define CMD_SECTOR_ERASE 0x20
#define CMD_PAGE_PROGRAM 0x02
#define CMD_READ_DATA    0x03
#define CMD_READ_STATUS  0x05
#define CMD_READ_CONFIG  0x35

SPISettings spiSettings(10000000, MSBFIRST, SPI_MODE0);

void waitBusy() {
  uint8_t status;
  do {
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    status = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    delay(1);
  } while (status & 0x01);
}

void writeEnable() {
  SPI.beginTransaction(spiSettings);
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_WREN);
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
}

// ฟังก์ชันสร้าง Pattern ตามโหมดที่เลือก (ประมวลผลบน ESP32 ความเร็วสูง)
uint8_t getPatternByte(uint32_t offset, int patternType) {
  switch (patternType) {
    case 0: return 0xAA;                        // All 0xAA
    case 1: return 0x55;                        // All 0x55
    case 2: return (offset % 2 == 0) ? 0xAA : 0x55; // Alternating 0xAA / 0x55
    case 3: return (uint8_t)(offset & 0xFF);    // Incrementing (0x00 - 0xFF)
    case 4: return 0x00;                        // All 0x00
    default: return 0xFF;
  }
}

// ฟังก์ชันเขียน Pattern แบบสตรีมมิ่งทีละ 256 Bytes (Page Program)
void writePattern(uint32_t addr, uint32_t len, int patternType) {
  uint32_t bytesWritten = 0;
  uint8_t pageBuf[256];

  while (bytesWritten < len) {
    uint32_t currentAddr = addr + bytesWritten;
    uint16_t chunkSize = 256 - (currentAddr % 256);
    if (bytesWritten + chunkSize > len) {
      chunkSize = len - bytesWritten;
    }

    for (uint16_t i = 0; i < chunkSize; i++) {
      pageBuf[i] = getPatternByte(bytesWritten + i, patternType);
    }

    writeEnable();
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_PAGE_PROGRAM);
    SPI.transfer((currentAddr >> 16) & 0xFF);
    SPI.transfer((currentAddr >> 8) & 0xFF);
    SPI.transfer(currentAddr & 0xFF);

    for (uint16_t i = 0; i < chunkSize; i++) {
      SPI.transfer(pageBuf[i]);
    }
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    waitBusy();

    bytesWritten += chunkSize;
  }
  Serial.println("WRITE_PAT:OK");
}

// ฟังก์ชันตรวจสอบข้อมูลเทียบกับ Pattern โดยตรงบน ESP32
void verifyPattern(uint32_t addr, uint32_t len, int patternType) {
  uint32_t errors = 0;
  uint32_t firstErrAddr = 0;
  uint8_t expectedErr = 0, actualErr = 0;

  SPI.beginTransaction(spiSettings);
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_READ_DATA);
  SPI.transfer((addr >> 16) & 0xFF);
  SPI.transfer((addr >> 8) & 0xFF);
  SPI.transfer(addr & 0xFF);

  for (uint32_t i = 0; i < len; i++) {
    uint8_t actual = SPI.transfer(0x00);
    uint8_t expected = getPatternByte(i, patternType);

    if (actual != expected) {
      if (errors == 0) {
        firstErrAddr = addr + i;
        expectedErr = expected;
        actualErr = actual;
      }
      errors++;
    }
  }
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();

  if (errors == 0) {
    Serial.println("VERIFY:OK");
  } else {
    Serial.printf("VERIFY:FAIL:%d:0x%06X:EXP_0x%02X_GOT_0x%02X\n", errors, firstErrAddr, expectedErr, actualErr);
  }
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
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    uint8_t stat = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_CONFIG);
    uint8_t cfg = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.printf("REGS:%02X:%02X\n", stat, cfg);
  }
  else if (cmd.startsWith("ERASE:")) {
    uint32_t addr = strtoul(cmd.substring(6).c_str(), NULL, 16);
    writeEnable();
    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_SECTOR_ERASE);
    SPI.transfer((addr >> 16) & 0xFF);
    SPI.transfer((addr >> 8) & 0xFF);
    SPI.transfer(addr & 0xFF);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    waitBusy();
    Serial.println("ERASE:OK");
  }
  else if (cmd.startsWith("WRITE_PAT:")) {
    // Format: WRITE_PAT:<ADDR_HEX>:<LEN_DEC>:<PAT_TYPE>
    int p1 = cmd.indexOf(':', 10);
    int p2 = cmd.indexOf(':', p1 + 1);
    uint32_t addr = strtoul(cmd.substring(10, p1).c_str(), NULL, 16);
    uint32_t len = cmd.substring(p1 + 1, p2).toInt();
    int patType = cmd.substring(p2 + 1).toInt();

    writePattern(addr, len, patType);
  }
  else if (cmd.startsWith("VERIFY:")) {
    // Format: VERIFY:<ADDR_HEX>:<LEN_DEC>:<PAT_TYPE>
    int p1 = cmd.indexOf(':', 7);
    int p2 = cmd.indexOf(':', p1 + 1);
    uint32_t addr = strtoul(cmd.substring(7, p1).c_str(), NULL, 16);
    uint32_t len = cmd.substring(p1 + 1, p2).toInt();
    int patType = cmd.substring(p2 + 1).toInt();

    verifyPattern(addr, len, patType);
  }
  else if (cmd.startsWith("READ:")) {
    int idx = cmd.indexOf(':', 5);
    uint32_t addr = strtoul(cmd.substring(5, idx).c_str(), NULL, 16);
    int len = cmd.substring(idx + 1).toInt();
    if (len <= 0) len = 16;
    if (len > 256) len = 256;

    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_DATA);
    SPI.transfer((addr >> 16) & 0xFF);
    SPI.transfer((addr >> 8) & 0xFF);
    SPI.transfer(addr & 0xFF);

    Serial.print("DATA:");
    for (int i = 0; i < len; i++) {
      uint8_t b = SPI.transfer(0x00);
      Serial.printf("%02X", b);
      if (i < len - 1) Serial.print(" ");
    }
    Serial.println();
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
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