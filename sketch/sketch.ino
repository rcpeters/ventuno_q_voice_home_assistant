#include <Arduino_LED_Matrix.h>
#include <Arduino_RouterBridge.h>
#include <math.h>

ArduinoLEDMatrix matrix;

// Array for the 104 LEDs (13 columns x 8 rows)
uint8_t logicalFrame[8 * 13];

// Assistant Variables
int currentState = 0; // 0: IDLE, 1: LISTENING, 2: PROCESSING, 3: SPEAKING
float phase = 0.0;
float scanPos = 0.0;
float scanDir = 1.0;

// RPC: Receives the command from Python
void setSystemState(int state) {
  currentState = constrain(state, 0, 3);
  if (currentState == 1) {
    scanPos = 0.0; // Reset scanner when starting to listen
  }
}

// Calculates soft brightness (grayscale) for the scanner tail
uint8_t scanBrightness(int col, float headPos, float tailLen) {
  float dist = fabs((float)col - headPos);
  if (dist > tailLen) return 0;
  float t = 1.0 - (dist / tailLen);
  return (uint8_t)(t * t * t * 7.0 + 0.5); // Cubic falloff for a more natural effect
}

void renderMatrix() {
  memset(logicalFrame, 0, sizeof(logicalFrame));

  // --- STATE 1 and 2: KITT SCANNER (Listening / Thinking) ---
  if (currentState == 1 || currentState == 2) { 
    float scanSpeed = (currentState == 1) ? 0.6 : 0.2; // Faster if listening
    scanPos += scanDir * scanSpeed;
    if (scanPos >= 12.0) { scanPos = 12.0; scanDir = -1.0; }
    if (scanPos <= 0.0 && scanDir < 0) { scanPos = 0.0; scanDir = 1.0; }

    for (int col = 0; col < 13; col++) {
      // Faint central guide lines (Brightness 1)
      logicalFrame[3 * 13 + col] = 1;
      logicalFrame[4 * 13 + col] = 1;
      
      uint8_t bri = scanBrightness(col, scanPos, 4.0);
      if (bri > 0) {
        logicalFrame[3 * 13 + col] = max((int)logicalFrame[3 * 13 + col], (int)bri);
        logicalFrame[4 * 13 + col] = max((int)logicalFrame[4 * 13 + col], (int)bri);
      }
    }
  }

  // --- STATE 3: VOICE WAVES (Speaking) ---
  if (currentState == 3) { 
    phase += 0.25; 
    float norm = sin(phase * 0.5) * 0.5 + 0.5; // General volume pulse
    float maxWaveH = norm * 3.5; 

    for (int col = 0; col < 13; col++) {
      // Faint central guide lines
      logicalFrame[3 * 13 + col] = 1;
      logicalFrame[4 * 13 + col] = 1;

      // Combination of two sines for a more organic wave
      float wave = sin(phase * 1.2 + col * 0.55) * 0.5 + sin(phase * 2.1 + col * 0.95) * 0.3;
      float displacement = wave * maxWaveH;
      int colHalf = constrain((int)(fabs(displacement) + 1.0), 1, 4);
      
      // Wave brightness increases or decreases with intensity
      int waveBri = 1 + (int)(norm * 6.0); 

      // Draw the wave column from the center to the edges
      for (int row = 4 - colHalf; row <= 3 + colHalf; row++) {
         logicalFrame[row * 13 + col] = constrain(waveBri, 1, 7);
      }
    }
  }

  // 🚀 Direct to native library with 104 bytes and grayscale enabled!
  matrix.draw(logicalFrame);
}

void setup() {
  delay(1000);
  matrix.begin();
  
  // Re-enable 3-bit grayscale (0 to 7) which the library supports natively
  matrix.setGrayscaleBits(3); 
  matrix.clear();
  
  Bridge.begin();
  Bridge.provide("set_state", setSystemState); 
}

void loop() {
  if (currentState != 0) {
    renderMatrix();
  } else {
    // Complete shutdown in IDLE
    matrix.clear();
  }
  
  // ~33 FPS for smooth animation without saturating the Bridge
  delay(30); 
}