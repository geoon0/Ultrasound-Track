#include <Arduino.h>

// 핀 매핑 (가승 핀번호 - 실제 회로도에 맞춰 수정 필요)
const int PIN_TX = 10;     // 40kHz 펄스 출력 핀
const int PIN_RX_1 = 11;   // 수신 채널 1 (ADC)
const int PIN_RX_2 = 12;   // 수신 채널 2 (ADC)
const int PIN_RX_3 = 13;   // 수신 채널 3 (ADC)

void setup() {
  // 시리얼 통신 초기화 (platformio.ini 설정과 동일하게 115200 bps)
  Serial.begin(115200);
  
  // 시리얼 포트 연결 대기 (일부 ESP32 모델에서 필요)
  delay(1000);
  
  Serial.println("==================================");
  Serial.println("Ultrasound-Track Firmware Booting.");
  Serial.println("==================================");

  // 송신 핀 초기화
  pinMode(PIN_TX, OUTPUT);
  digitalWrite(PIN_TX, LOW);
  
  // 수신 핀(ADC)은 보통 별도의 pinMode() 설정이 필요 없으나, 
  // 향후 DMA ADC 초기화 시 설정할 예정입니다.
}

void loop() {
  // TODO: 40kHz 펄스 생성 및 DMA ADC 데이터 수집 로직 추가 예정
  
  // 보드 정상 동작 확인용 Heartbeat 로그
  Serial.println("[Status] System is running...");
  delay(1000); 
}
