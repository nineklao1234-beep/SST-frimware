#include <Arduino.h>
#include <SPI.h>

#define CS_PIN 5

#define CMD_WREN         0x06
#define CMD_ULBPR        0x98
#define CMD_JEDEC_ID     0x9F
#define CMD_SECTOR_ERASE 0x20
#define CMD_CHIP_ERASE   0xC7
#define CMD_PAGE_PROGRAM 0x02
#define CMD_READ_DATA    0x03
#define CMD_READ_STATUS  0x05
#define CMD_READ_CONFIG  0x35

#define FLASH_SIZE_2MB   0x200000  // 2,097,152 Bytes

uint32_t currentSpiFreqHz = 10000000; // ค่าเริ่มต้น 10 MHz

// ฟังก์ชันเช็กคำสั่งยกเลิกกลางคันจาก GUI
bool checkStopCommand() {
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    if (input == "STOP") {
      return true;
    }
  }
  return false;
}

void waitBusyWithProgress() {
  uint8_t status;
  int step = 0;
  do {
    if (checkStopCommand()) {
      Serial.println("ABORTED");
      return;
    }
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    status = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    delay(5);
    step++;
    if (step % 2 == 0 && step <= 18) {
      int pct = (step / 2) * 10;
      Serial.printf("PROGRESS:%d\n", pct);
    }
  } while (status & 0x01);
  Serial.println("PROGRESS:100");
}

void writeEnable() {
  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_WREN);
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
}

uint8_t getPatternByte(uint32_t offset, int patternType) {
  switch (patternType) {
    case 0: return 0xAA;
    case 1: return 0x55;
    case 2: return (offset % 2 == 0) ? 0xAA : 0x55;
    case 3: return (uint8_t)(offset & 0xFF);
    case 4: return 0x00;
    default: return 0xFF;
  }
}

void writePattern(uint32_t addr, uint32_t len, int patternType) {
  uint32_t bytesWritten = 0;
  uint8_t pageBuf[256];
  int lastPct = -1;

  Serial.println("PROGRESS:0");

  while (bytesWritten < len) {
    if (checkStopCommand()) {
      Serial.println("ABORTED");
      return;
    }

    uint32_t currentAddr = addr + bytesWritten;
    uint16_t chunkSize = 256 - (currentAddr % 256);
    if (bytesWritten + chunkSize > len) {
      chunkSize = len - bytesWritten;
    }

    for (uint16_t i = 0; i < chunkSize; i++) {
      pageBuf[i] = getPatternByte(bytesWritten + i, patternType);
    }

    writeEnable();
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
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
    
    uint8_t status;
    do {
      SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
      digitalWrite(CS_PIN, LOW);
      SPI.transfer(CMD_READ_STATUS);
      status = SPI.transfer(0x00);
      digitalWrite(CS_PIN, HIGH);
      SPI.endTransaction();
      delayMicroseconds(50);
    } while (status & 0x01);

    bytesWritten += chunkSize;

    int currentPct = (int)((uint64_t)bytesWritten * 100 / len);
    int currentStep = (currentPct / 10) * 10;
    if (currentStep != lastPct) {
      lastPct = currentStep;
      Serial.printf("PROGRESS:%d\n", lastPct);
    }
  }
  Serial.println("PROGRESS:100");
  Serial.println("WRITE_PAT:OK");
}

void verifyPattern(uint32_t addr, uint32_t len, int patternType) {
  uint32_t errors = 0;
  uint32_t firstErrAddr = 0;
  uint8_t expectedErr = 0, actualErr = 0;
  int lastPct = -1;

  Serial.println("PROGRESS:0");

  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_READ_DATA);
  SPI.transfer((addr >> 16) & 0xFF);
  SPI.transfer((addr >> 8) & 0xFF);
  SPI.transfer(addr & 0xFF);

  for (uint32_t i = 0; i < len; i++) {
    if ((i % 1024 == 0) && checkStopCommand()) {
      digitalWrite(CS_PIN, HIGH);
      SPI.endTransaction();
      Serial.println("ABORTED");
      return;
    }

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

    int currentPct = (int)((uint64_t)(i + 1) * 100 / len);
    int currentStep = (currentPct / 10) * 10;
    if (currentStep != lastPct) {
      lastPct = currentStep;
      Serial.printf("PROGRESS:%d\n", lastPct);
    }
  }
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();

  Serial.println("PROGRESS:100");
  if (errors == 0) {
    Serial.println("VERIFY:OK");
  } else {
    Serial.printf("VERIFY:FAIL:%d:0x%06X:EXP_0x%02X_GOT_0x%02X\n", errors, firstErrAddr, expectedErr, actualErr);
  }
}

void verifyFullChip(int patternType) {
  uint32_t totalErrors = 0;
  const uint32_t maxPrintedErrors = 100;
  int lastPct = -1;

  Serial.println("PROGRESS:0");

  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_READ_DATA);
  SPI.transfer(0x00);
  SPI.transfer(0x00);
  SPI.transfer(0x00);

  for (uint32_t i = 0; i < FLASH_SIZE_2MB; i++) {
    if ((i % 2048 == 0) && checkStopCommand()) {
      digitalWrite(CS_PIN, HIGH);
      SPI.endTransaction();
      Serial.println("ABORTED");
      return;
    }

    uint8_t actual = SPI.transfer(0x00);
    uint8_t expected = getPatternByte(i, patternType);

    if (actual != expected) {
      totalErrors++;
      if (totalErrors <= maxPrintedErrors) {
        Serial.printf("FAIL_ADDR:0x%06X:EXP_0x%02X_GOT_0x%02X\n", i, expected, actual);
      }
    }

    int currentPct = (int)((uint64_t)(i + 1) * 100 / FLASH_SIZE_2MB);
    int currentStep = (currentPct / 10) * 10;
    if (currentStep != lastPct) {
      lastPct = currentStep;
      Serial.printf("PROGRESS:%d\n", lastPct);
    }
  }
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();

  Serial.println("PROGRESS:100");
  Serial.printf("VERIFY_FULL:DONE:%u\n", totalErrors);
}

void processCommand(String cmd) {
  cmd.trim();

  if (cmd.startsWith("SET_FREQ:")) {
    int mhz = cmd.substring(9).toInt();
    if (mhz > 0 && mhz <= 80) {
      currentSpiFreqHz = mhz * 1000000;
      Serial.printf("FREQ:OK:%d\n", mhz);
    } else {
      Serial.println("FREQ:ERR");
    }
  }
  else if (cmd == "READ_ID") {
    Serial.println("PROGRESS:0");
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_JEDEC_ID);
    uint8_t mfg  = SPI.transfer(0x00);
    uint8_t type = SPI.transfer(0x00);
    uint8_t id   = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    Serial.println("PROGRESS:100");
    Serial.printf("ID:%02X %02X %02X\n", mfg, type, id);
  }
  else if (cmd == "UNLOCK") {
    Serial.println("PROGRESS:0");
    writeEnable();
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_ULBPR);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    delay(10);
    Serial.println("PROGRESS:100");
    Serial.println("UNLOCK:OK");
  }
  else if (cmd == "READ_REGS") {
    Serial.println("PROGRESS:0");
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    uint8_t stat = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_CONFIG);
    uint8_t cfg = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.println("PROGRESS:100");
    Serial.printf("REGS:%02X:%02X\n", stat, cfg);
  }
  else if (cmd.startsWith("ERASE:")) {
    uint32_t addr = strtoul(cmd.substring(6).c_str(), NULL, 16);
    Serial.println("PROGRESS:0");
    writeEnable();
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_SECTOR_ERASE);
    SPI.transfer((addr >> 16) & 0xFF);
    SPI.transfer((addr >> 8) & 0xFF);
    SPI.transfer(addr & 0xFF);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    waitBusyWithProgress();
    Serial.println("ERASE:OK");
  }
  else if (cmd == "ERASE_CHIP") {
    Serial.println("PROGRESS:0");
    writeEnable();
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_CHIP_ERASE);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    waitBusyWithProgress();
    Serial.println("ERASE_CHIP:OK");
  }
  else if (cmd.startsWith("WRITE_PAT:")) {
    int p1 = cmd.indexOf(':', 10);
    int p2 = cmd.indexOf(':', p1 + 1);
    uint32_t addr = strtoul(cmd.substring(10, p1).c_str(), NULL, 16);
    uint32_t len = cmd.substring(p1 + 1, p2).toInt();
    int patType = cmd.substring(p2 + 1).toInt();

    writePattern(addr, len, patType);
  }
  else if (cmd.startsWith("WRITE_FULL_PAT:")) {
    int patType = cmd.substring(15).toInt();
    writePattern(0x000000, FLASH_SIZE_2MB, patType);
  }
  else if (cmd.startsWith("VERIFY:")) {
    int p1 = cmd.indexOf(':', 7);
    int p2 = cmd.indexOf(':', p1 + 1);
    uint32_t addr = strtoul(cmd.substring(7, p1).c_str(), NULL, 16);
    uint32_t len = cmd.substring(p1 + 1, p2).toInt();
    int patType = cmd.substring(p2 + 1).toInt();

    verifyPattern(addr, len, patType);
  }
  else if (cmd.startsWith("VERIFY_FULL_PAT:")) {
    int patType = cmd.substring(16).toInt();
    verifyFullChip(patType);
  }
  else if (cmd.startsWith("READ:")) {
    int idx = cmd.indexOf(':', 5);
    uint32_t addr = strtoul(cmd.substring(5, idx).c_str(), NULL, 16);
    int len = cmd.substring(idx + 1).toInt();
    if (len <= 0) len = 16;
    if (len > 512) len = 512;

    Serial.println("PROGRESS:0");
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
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
    Serial.println("PROGRESS:100");
  }
}

void setup() {
  Serial.begin(921600);
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