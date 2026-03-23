#include <Arduino.h>

/*
  Comunito ESP32 — Gate + 2x Wiegand (Cam1/Cam2) + Config Portal + HTTP/Serial
  ==========================================================================
  Un SOLO ESP32 hace:
    1) Gate (2 salidas: Cam1 y Cam2) por:
       - SERIAL/USB: recibe JSONL {"cmd":"pulse","gate":1,"ms":500} desde el Pi
       - HTTP: /pulse?token=...&cam=1..2&ms=...  (compatible con portal Pi)
    2) 2 lectoras Wiegand (D0/D1 por lectora) -> calcula múltiples representaciones
       y te deja elegir desde el portal cuál se envía al Pi (para coincidir con el tag impreso).
    3) Envío de tag_event al Pi por:
       - HTTP: POST /api/tag_event  (recomendado; tu Pi ya lo soporta)
       - SERIAL: JSONL {"evt":"tag","cam":1,"tag_physical":"...","tag_internal_hex":""} (opcional)

  Portal de Config:
  - Siempre levanta SoftAP + Captive Portal:
      SSID "COMUNITO-ESP32"  PASS "comunito123"
      Abre: http://192.168.4.1
  - (Opcional) Conecta a WiFi STA si configuras ssid/pass; y publica mDNS: http://<hostname>.local

  Board:
  - ESP32 Dev Module

  Cableado default (puedes cambiar desde portal):
  - Gate Cam1: GPIO5
  - Gate Cam2: GPIO33
  - Wiegand1: D0 GPIO16, D1 GPIO17
  - Wiegand2: D0 GPIO18, D1 GPIO19
*/

// ===========================
// TIPOS (ANTES de otros includes)
// ===========================

struct Interpret {
  uint32_t nbits=0;
  uint64_t raw=0;

  String raw_hex;
  String raw_dec;

  // After strip/rev
  uint32_t adj_n=0;
  uint64_t adj=0;

  String adj_hex;
  String adj_dec;

  // common derived
  String wg26_card;
  String wg26_fac;
  String wg26_fac_card;
  String custom_slice_dec;
};

struct WgState {
  volatile uint64_t bits = 0;
  volatile uint32_t nbits = 0;
  volatile uint32_t lastBitMs = 0;

  // last complete
  uint64_t lastRawBits = 0;
  uint32_t lastRawNBits = 0;
  uint32_t lastFrameMs = 0;

  String lastRawHex;
  String lastRawDec;

  // last mapped to send
  String lastMapped;

  // anti-repeat
  String lastSentValue;
  uint32_t lastSentMs = 0;
};

struct Cfg {
  // WiFi STA (opcional)
  String sta_ssid;
  String sta_pass;
  String hostname; // mDNS

  // Gate
  String gate_token;
  int gate1_pin;          // salida cam1
  int gate2_pin;          // salida cam2
  bool gate_active_low;   // relay active-low?
  int gate_pulse_ms_def;

  // Wiegand pins
  int wg1_d0, wg1_d1;      // lectora 1
  int wg2_d0, wg2_d1;      // lectora 2

  // Anti-repeat (ms)
  int wg1_antirepeat_ms;
  int wg2_antirepeat_ms;

  // Frame gap (ms)
  int wg_frame_gap_ms;

  // Output mapping
  // out_mode: RAW_HEX | RAW_DEC | WG26_CARD | WG26_FAC_CARD | CUSTOM_SLICE
  String out1_mode;
  String out2_mode;

  bool strip1_parity;
  bool strip2_parity;

  bool rev1_bits;
  bool rev2_bits;

  int pad1_len;
  int pad2_len;

  int slice1_start;
  int slice1_len;
  int slice2_start;
  int slice2_len;

  // Send TAG to Pi
  // tag_send_mode: "http" | "serial"
  String tag_send_mode;
  String portal_url;
  String portal_api_key;
};

// ===========================
// INCLUDES (después de tipos)
// ===========================
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>

// ---------------------------
// Defaults / Portal
// ---------------------------
static const char* AP_SSID = "COMUNITO-ESP32";
static const char* AP_PASS = "comunito123";
static const byte  DNS_PORT = 53;

WebServer server(80);
DNSServer dns;
Preferences prefs;

// ---------------------------
// Util
// ---------------------------
static inline uint32_t msNow() { return (uint32_t)millis(); }

String htmlEscape(const String& s){
  String out; out.reserve(s.length()+8);
  for (size_t i=0;i<s.length();i++){
    char c=s[i];
    if(c=='&') out+="&amp;";
    else if(c=='<') out+="&lt;";
    else if(c=='>') out+="&gt;";
    else if(c=='"') out+="&quot;";
    else out+=c;
  }
  return out;
}

int toIntSafe(const String& s, int fb){
  if(!s.length()) return fb;
  char* end=nullptr;
  long v=strtol(s.c_str(), &end, 10);
  if(end==s.c_str()) return fb;
  return (int)v;
}

String leftPadZeros(const String& s, int targetLen){
  if(targetLen<=0) return s;
  if((int)s.length()>=targetLen) return s;
  String out; out.reserve(targetLen);
  for(int i=0;i<targetLen-(int)s.length();i++) out += "0";
  out += s;
  return out;
}

// ---------------------------
// Config storage
// ---------------------------
Cfg cfg;

void loadCfg(){
  prefs.begin("comunito", true);

  cfg.sta_ssid = prefs.getString("sta_ssid", "");
  cfg.sta_pass = prefs.getString("sta_pass", "");
  cfg.hostname = prefs.getString("host", "gate-esp32");

  cfg.gate_token = prefs.getString("gtok", "12345");
  cfg.gate1_pin = prefs.getInt("g1p", 5);
  cfg.gate2_pin = prefs.getInt("g2p", 33);
  cfg.gate_active_low = prefs.getBool("gal", false);
  cfg.gate_pulse_ms_def = prefs.getInt("gpms", 500);

  cfg.wg1_d0 = prefs.getInt("w1d0", 16);
  cfg.wg1_d1 = prefs.getInt("w1d1", 17);
  cfg.wg2_d0 = prefs.getInt("w2d0", 18);
  cfg.wg2_d1 = prefs.getInt("w2d1", 19);

  cfg.wg1_antirepeat_ms = prefs.getInt("w1ar", 600);
  cfg.wg2_antirepeat_ms = prefs.getInt("w2ar", 600);
  cfg.wg_frame_gap_ms = prefs.getInt("wgap", 25);

  cfg.out1_mode = prefs.getString("o1m", "WG26_CARD");
  cfg.out2_mode = prefs.getString("o2m", "WG26_CARD");

  cfg.strip1_parity = prefs.getBool("s1p", true);
  cfg.strip2_parity = prefs.getBool("s2p", true);

  cfg.rev1_bits = prefs.getBool("r1b", false);
  cfg.rev2_bits = prefs.getBool("r2b", false);

  cfg.pad1_len = prefs.getInt("p1l", 0);
  cfg.pad2_len = prefs.getInt("p2l", 0);

  cfg.slice1_start = prefs.getInt("c1s", 0);
  cfg.slice1_len   = prefs.getInt("c1l", 0);
  cfg.slice2_start = prefs.getInt("c2s", 0);
  cfg.slice2_len   = prefs.getInt("c2l", 0);

  cfg.tag_send_mode = prefs.getString("tsm", "http");
  cfg.portal_url    = prefs.getString("purl", "http://192.168.88.1");
  cfg.portal_api_key= prefs.getString("pak", "");

  prefs.end();
}

void saveCfg(){
  prefs.begin("comunito", false);

  prefs.putString("sta_ssid", cfg.sta_ssid);
  prefs.putString("sta_pass", cfg.sta_pass);
  prefs.putString("host", cfg.hostname);

  prefs.putString("gtok", cfg.gate_token);
  prefs.putInt("g1p", cfg.gate1_pin);
  prefs.putInt("g2p", cfg.gate2_pin);
  prefs.putBool("gal", cfg.gate_active_low);
  prefs.putInt("gpms", cfg.gate_pulse_ms_def);

  prefs.putInt("w1d0", cfg.wg1_d0);
  prefs.putInt("w1d1", cfg.wg1_d1);
  prefs.putInt("w2d0", cfg.wg2_d0);
  prefs.putInt("w2d1", cfg.wg2_d1);

  prefs.putInt("w1ar", cfg.wg1_antirepeat_ms);
  prefs.putInt("w2ar", cfg.wg2_antirepeat_ms);
  prefs.putInt("wgap", cfg.wg_frame_gap_ms);

  prefs.putString("o1m", cfg.out1_mode);
  prefs.putString("o2m", cfg.out2_mode);

  prefs.putBool("s1p", cfg.strip1_parity);
  prefs.putBool("s2p", cfg.strip2_parity);

  prefs.putBool("r1b", cfg.rev1_bits);
  prefs.putBool("r2b", cfg.rev2_bits);

  prefs.putInt("p1l", cfg.pad1_len);
  prefs.putInt("p2l", cfg.pad2_len);

  prefs.putInt("c1s", cfg.slice1_start);
  prefs.putInt("c1l", cfg.slice1_len);
  prefs.putInt("c2s", cfg.slice2_start);
  prefs.putInt("c2l", cfg.slice2_len);

  prefs.putString("tsm", cfg.tag_send_mode);
  prefs.putString("purl", cfg.portal_url);
  prefs.putString("pak", cfg.portal_api_key);

  prefs.end();
}

// ---------------------------
// Gate pulse (GPIO)
// ---------------------------
void gatePulsePin(int pin, int ms, bool activeLow){
  if(pin < 0) return;

  pinMode(pin, OUTPUT);

  int active = activeLow ? LOW : HIGH;
  int idle   = activeLow ? HIGH : LOW;

  digitalWrite(pin, active);
  delay(ms);
  digitalWrite(pin, idle);
}

void gatePulse(int gate, int ms){
  int pin = (gate==2 ? cfg.gate2_pin : cfg.gate1_pin);
  gatePulsePin(pin, ms, cfg.gate_active_low);
}

// ---------------------------
// Wiegand capture
// ---------------------------
WgState wg1, wg2;

portMUX_TYPE wgMux1 = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE wgMux2 = portMUX_INITIALIZER_UNLOCKED;

void IRAM_ATTR isrWg1D0(){
  portENTER_CRITICAL_ISR(&wgMux1);
  wg1.bits <<= 1;
  wg1.nbits++;
  wg1.lastBitMs = msNow();
  portEXIT_CRITICAL_ISR(&wgMux1);
}
void IRAM_ATTR isrWg1D1(){
  portENTER_CRITICAL_ISR(&wgMux1);
  wg1.bits = (wg1.bits << 1) | 1ULL;
  wg1.nbits++;
  wg1.lastBitMs = msNow();
  portEXIT_CRITICAL_ISR(&wgMux1);
}
void IRAM_ATTR isrWg2D0(){
  portENTER_CRITICAL_ISR(&wgMux2);
  wg2.bits <<= 1;
  wg2.nbits++;
  wg2.lastBitMs = msNow();
  portEXIT_CRITICAL_ISR(&wgMux2);
}
void IRAM_ATTR isrWg2D1(){
  portENTER_CRITICAL_ISR(&wgMux2);
  wg2.bits = (wg2.bits << 1) | 1ULL;
  wg2.nbits++;
  wg2.lastBitMs = msNow();
  portEXIT_CRITICAL_ISR(&wgMux2);
}

// ---------------------------
// Bit mapping helpers
// ---------------------------
uint64_t reverseBits(uint64_t v, uint32_t n){
  uint64_t r=0;
  for(uint32_t i=0;i<n;i++){
    r = (r<<1) | ((v>>i)&1ULL);
  }
  return r;
}

// slice bits MSB-first (start=0 MSB)
uint64_t sliceBitsMSB(uint64_t v, uint32_t n, uint32_t start, uint32_t len){
  if(len==0 || start>=n) return 0;
  if(start+len>n) len = n-start;
  uint32_t shiftRight = n - (start + len);
  uint64_t mask = (len==64) ? ~0ULL : ((1ULL<<len)-1ULL);
  return (v >> shiftRight) & mask;
}

String u64ToHex(uint64_t v){
  char buf[32]; snprintf(buf, sizeof(buf), "%llX", (unsigned long long)v);
  return String(buf);
}
String u64ToDec(uint64_t v){
  return String((unsigned long long)v);
}

Interpret interpretBits(uint64_t rawBits, uint32_t rawNBits, bool stripParity, bool rev, int sliceStart, int sliceLen){
  Interpret it;
  it.nbits = rawNBits;
  it.raw = rawBits;
  it.raw_hex = u64ToHex(rawBits);
  it.raw_dec = u64ToDec(rawBits);

  uint64_t bits = rawBits;
  uint32_t n = rawNBits;

  // strip parity: remove MSB and LSB
  if(stripParity && n >= 3){
    bits = (bits >> 1) & ((1ULL<<(n-2)) - 1ULL);
    n = n - 2;
  }
  if(rev && n>0){
    bits = reverseBits(bits, n);
  }

  it.adj_n = n;
  it.adj = bits;
  it.adj_hex = u64ToHex(bits);
  it.adj_dec = u64ToDec(bits);

  // W26 after strip parity => 24 bits => 8 facility + 16 card
  if(n == 24){
    uint64_t fac  = sliceBitsMSB(bits, n, 0, 8);
    uint64_t card = sliceBitsMSB(bits, n, 8, 16);
    it.wg26_fac  = String((unsigned long long)fac);
    it.wg26_card = String((unsigned long long)card);
    it.wg26_fac_card = it.wg26_fac + "-" + it.wg26_card;
  } else {
    it.wg26_fac = "";
    it.wg26_card = "";
    it.wg26_fac_card = "";
  }

  if(sliceLen > 0 && sliceStart >= 0){
    uint64_t sl = sliceBitsMSB(bits, n, (uint32_t)sliceStart, (uint32_t)sliceLen);
    it.custom_slice_dec = String((unsigned long long)sl);
  } else {
    it.custom_slice_dec = "";
  }

  return it;
}

String mapTagForChannel(int cam, const Interpret& it){
  String mode = (cam==1)?cfg.out1_mode:cfg.out2_mode;
  int padLen  = (cam==1)?cfg.pad1_len:cfg.pad2_len;

  if(mode=="RAW_HEX") return leftPadZeros(it.adj_hex, padLen);
  if(mode=="RAW_DEC") return leftPadZeros(it.adj_dec, padLen);
  if(mode=="WG26_CARD"){
    if(it.wg26_card.length()) return leftPadZeros(it.wg26_card, padLen);
    return leftPadZeros(it.adj_dec, padLen);
  }
  if(mode=="WG26_FAC_CARD"){
    if(it.wg26_fac_card.length()) return it.wg26_fac_card;
    return it.adj_dec;
  }
  if(mode=="CUSTOM_SLICE"){
    if(it.custom_slice_dec.length()) return leftPadZeros(it.custom_slice_dec, padLen);
    return leftPadZeros(it.adj_dec, padLen);
  }
  return leftPadZeros(it.adj_dec, padLen);
}

// ---------------------------
// Send tag to Pi (HTTP or SERIAL)
// ---------------------------
bool sendTagToPortalHTTP(int cam, const String& tagPhysical){
  if(!cfg.portal_url.length()) return false;

  WiFiClient client;
  String url = cfg.portal_url;

  if(!url.startsWith("http://") && !url.startsWith("https://")) url = "http://" + url;
  url.replace("https://", "http://"); // no TLS here
  if(url.endsWith("/")) url.remove(url.length()-1);

  String host = url;
  host.replace("http://", "");
  int slash = host.indexOf('/');
  if(slash>=0) host = host.substring(0, slash);

  String path = "/api/tag_event";
  String body = String("{\"cam\":") + cam + ",\"tag_physical\":\"" + tagPhysical + "\",\"tag_internal_hex\":\"\"}";

  String hostname = host;
  int port = 80;
  int colon = host.indexOf(':');
  if(colon>=0){
    hostname = host.substring(0, colon);
    port = toIntSafe(host.substring(colon+1), 80);
  }

  if(!client.connect(hostname.c_str(), port)) return false;

  String req;
  req += "POST " + path + " HTTP/1.1\r\n";
  req += "Host: " + hostname + "\r\n";
  req += "Connection: close\r\n";
  req += "Content-Type: application/json\r\n";
  if(cfg.portal_api_key.length()){
    req += "X-API-Key: " + cfg.portal_api_key + "\r\n";
  }
  req += "Content-Length: " + String(body.length()) + "\r\n\r\n";
  req += body;

  client.print(req);

  uint32_t t0 = msNow();
  while(client.connected() && !client.available() && (msNow()-t0)<1500) delay(1);
  String line = client.readStringUntil('\n');
  client.stop();
  return (line.indexOf("200")>=0);
}

void sendTagToPortalSerial(int cam, const String& tagPhysical){
  String js = String("{\"evt\":\"tag\",\"cam\":") + cam + ",\"tag_physical\":\"" + tagPhysical + "\",\"tag_internal_hex\":\"\"}";
  Serial.println(js);
}

bool emitTagEvent(int cam, const String& tagPhysical){
  if(cfg.tag_send_mode=="serial"){
    sendTagToPortalSerial(cam, tagPhysical);
    return true;
  }
  return sendTagToPortalHTTP(cam, tagPhysical);
}

// ---------------------------
// Process Wiegand frames
// ---------------------------
void commitFrame(int cam, WgState& st, uint64_t bits, uint32_t nbits){
  st.lastRawBits = bits;
  st.lastRawNBits = nbits;
  st.lastFrameMs = msNow();
  st.lastRawHex = u64ToHex(bits);
  st.lastRawDec = u64ToDec(bits);

  bool strip = (cam==1)?cfg.strip1_parity:cfg.strip2_parity;
  bool rev   = (cam==1)?cfg.rev1_bits:cfg.rev2_bits;
  int ss = (cam==1)?cfg.slice1_start:cfg.slice2_start;
  int sl = (cam==1)?cfg.slice1_len:cfg.slice2_len;

  Interpret it = interpretBits(bits, nbits, strip, rev, ss, sl);
  String mapped = mapTagForChannel(cam, it);
  st.lastMapped = mapped;

  int ar = (cam==1)?cfg.wg1_antirepeat_ms:cfg.wg2_antirepeat_ms;
  if(mapped.length()){
    uint32_t now = msNow();
    if(st.lastSentValue == mapped && (now - st.lastSentMs) < (uint32_t)max(0, ar)){
      return;
    }
    bool ok = emitTagEvent(cam, mapped);
    if(ok){
      st.lastSentValue = mapped;
      st.lastSentMs = now;
    }
  }
}

void pollWiegand(){
  uint32_t gap = (uint32_t)max(5, cfg.wg_frame_gap_ms);

  // channel 1
  {
    uint64_t bits; uint32_t nbits; uint32_t lastms;
    portENTER_CRITICAL(&wgMux1);
    bits = wg1.bits;
    nbits = wg1.nbits;
    lastms = wg1.lastBitMs;
    portEXIT_CRITICAL(&wgMux1);

    if(nbits>0){
      uint32_t now = msNow();
      if((now - lastms) > gap){
        portENTER_CRITICAL(&wgMux1);
        uint64_t fb = wg1.bits;
        uint32_t fn = wg1.nbits;
        wg1.bits = 0; wg1.nbits = 0;
        portEXIT_CRITICAL(&wgMux1);
        commitFrame(1, wg1, fb, fn);
      }
    }
  }

  // channel 2
  {
    uint64_t bits; uint32_t nbits; uint32_t lastms;
    portENTER_CRITICAL(&wgMux2);
    bits = wg2.bits;
    nbits = wg2.nbits;
    lastms = wg2.lastBitMs;
    portEXIT_CRITICAL(&wgMux2);

    if(nbits>0){
      uint32_t now = msNow();
      if((now - lastms) > gap){
        portENTER_CRITICAL(&wgMux2);
        uint64_t fb = wg2.bits;
        uint32_t fn = wg2.nbits;
        wg2.bits = 0; wg2.nbits = 0;
        portEXIT_CRITICAL(&wgMux2);
        commitFrame(2, wg2, fb, fn);
      }
    }
  }
}

// ---------------------------
// SERIAL JSONL for Gate from Pi
// ---------------------------
String serLine;

bool jsonGetInt(const String& js, const String& key, int& out){
  int p = js.indexOf("\""+key+"\"");
  if(p<0) return false;
  p = js.indexOf(':', p);
  if(p<0) return false;
  int e = p+1;
  while(e<(int)js.length() && (js[e]==' '||js[e]=='\t')) e++;
  int s=e;
  while(e<(int)js.length() && (isDigit(js[e]) || js[e]=='-')) e++;
  if(e<=s) return false;
  out = toIntSafe(js.substring(s,e), out);
  return true;
}

bool jsonGetStr(const String& js, const String& key, String& out){
  int p = js.indexOf("\""+key+"\"");
  if(p<0) return false;
  p = js.indexOf(':', p);
  if(p<0) return false;
  p = js.indexOf('"', p);
  if(p<0) return false;
  int q = js.indexOf('"', p+1);
  if(q<0) return false;
  out = js.substring(p+1, q);
  return true;
}

void pollSerial(){
  while(Serial.available()){
    char c = (char)Serial.read();
    if(c=='\n'){
      String ln = serLine;
      serLine="";
      ln.trim();
      if(!ln.length()) continue;

      // Expect {"cmd":"pulse","gate":1,"ms":500}
      String cmd;
      if(!jsonGetStr(ln, "cmd", cmd)) continue;
      cmd.trim();

            if(cmd=="pulse"){
        int gate=1, ms=cfg.gate_pulse_ms_def;
        int pin=-1;
        int active_low = cfg.gate_active_low ? 1 : 0;

        jsonGetInt(ln, "gate", gate);
        jsonGetInt(ln, "ms", ms);
        jsonGetInt(ln, "pin", pin);
        jsonGetInt(ln, "active_low", active_low);

        ms = constrain(ms, 20, 5000);

        // Prioridad 1: si llega pin, usar ese GPIO exacto
        if(pin >= 0){
          gatePulsePin(pin, ms, active_low == 1);
        }else{
          // Compatibilidad con firmware viejo
          gate = (gate==2)?2:1;
          gatePulse(gate, ms);
        }
      }
    } else {
      if(serLine.length() < 512) serLine += c;
    }
  }
}

// ---------------------------
// HTTP endpoints
// ---------------------------
bool checkToken(){
  String tok = cfg.gate_token;
  if(!tok.length()) return true;
  String got = server.arg("token");
  if(!got.length()) got = server.header("X-Token");
  return got == tok;
}

void handlePulse(){
  if(!checkToken()){
    server.send(401, "application/json", "{\"ok\":false,\"error\":\"unauthorized\"}");
    return;
  }
  int pin = toIntSafe(server.arg("pin"), 0);
  int ms  = toIntSafe(server.arg("ms"), cfg.gate_pulse_ms_def);
  int cam = toIntSafe(server.arg("cam"), 0);
  ms = constrain(ms, 20, 5000);

  int gate = 1;
  if(cam==2) gate=2;
  else if(pin == cfg.gate2_pin) gate=2;
  else if(pin == cfg.gate1_pin) gate=1;

  gatePulse(gate, ms);
  server.send(200, "application/json", String("{\"ok\":true,\"gate\":")+gate+",\"ms\":"+ms+"}");
}

String statusJson(){
  bool s1p=cfg.strip1_parity, s2p=cfg.strip2_parity;
  bool r1b=cfg.rev1_bits, r2b=cfg.rev2_bits;

  Interpret i1 = interpretBits(wg1.lastRawBits, wg1.lastRawNBits, s1p, r1b, cfg.slice1_start, cfg.slice1_len);
  Interpret i2 = interpretBits(wg2.lastRawBits, wg2.lastRawNBits, s2p, r2b, cfg.slice2_start, cfg.slice2_len);

  auto jEsc=[&](const String& s)->String{
    String t=s; t.replace("\\","\\\\"); t.replace("\"","\\\"");
    return t;
  };

  String j="{";
  j += "\"wifi\":{\"ap_ip\":\""+WiFi.softAPIP().toString()+"\",\"sta_ip\":\""+WiFi.localIP().toString()+"\",\"hostname\":\""+jEsc(cfg.hostname)+"\"},";
  j += "\"gate\":{\"g1_pin\":"+String(cfg.gate1_pin)+",\"g2_pin\":"+String(cfg.gate2_pin)+",\"active_low\":"+(cfg.gate_active_low?"true":"false")+",\"pulse_ms_def\":"+String(cfg.gate_pulse_ms_def)+"},";
  j += "\"send\":{\"mode\":\""+jEsc(cfg.tag_send_mode)+"\",\"portal_url\":\""+jEsc(cfg.portal_url)+"\"},";
  j += "\"wg_gap_ms\":"+String(cfg.wg_frame_gap_ms)+",";

  auto addIt=[&](const Interpret& it, const String& mapped)->String{
    String o="{";
    o += "\"nbits\":"+String(it.nbits)+",";
    o += "\"raw_hex\":\""+jEsc(it.raw_hex)+"\",";
    o += "\"raw_dec\":\""+jEsc(it.raw_dec)+"\",";
    o += "\"adj_nbits\":"+String(it.adj_n)+",";
    o += "\"adj_hex\":\""+jEsc(it.adj_hex)+"\",";
    o += "\"adj_dec\":\""+jEsc(it.adj_dec)+"\",";
    o += "\"wg26_card\":\""+jEsc(it.wg26_card)+"\",";
    o += "\"wg26_fac\":\""+jEsc(it.wg26_fac)+"\",";
    o += "\"wg26_fac_card\":\""+jEsc(it.wg26_fac_card)+"\",";
    o += "\"custom_slice_dec\":\""+jEsc(it.custom_slice_dec)+"\",";
    o += "\"mapped\":\""+jEsc(mapped)+"\"";
    o += "}";
    return o;
  };

  j += "\"cam1\":"+addIt(i1, wg1.lastMapped)+",";
  j += "\"cam2\":"+addIt(i2, wg2.lastMapped);
  j += "}";
  return j;
}

void handleStatus(){
  server.send(200, "application/json", statusJson());
}

String optionSel(const String& v, const String& want){
  return (v==want) ? " selected" : "";
}
String boolSel(bool v, bool want){
  return (v==want) ? " selected" : "";
}

String pageIndex(){
  String h;
  h += "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
  h += "<title>Comunito ESP32</title>";
  h += "<style>body{font-family:system-ui;margin:16px;background:#fafafa} .card{background:#fff;border:1px solid #ddd;border-radius:12px;padding:12px;margin:10px 0;max-width:980px} input,select{padding:7px 9px;border:1px solid #bbb;border-radius:10px;min-width:220px} .grid{display:grid;grid-template-columns:repeat(2,minmax(260px,1fr));gap:10px 14px} .grid3{display:grid;grid-template-columns:repeat(3,minmax(200px,1fr));gap:10px 14px} .btn{padding:8px 12px;border:1px solid #888;border-radius:10px;background:#f5f5f5;cursor:pointer} .muted{color:#666;font-size:12px} code{background:#f0f0f0;padding:2px 6px;border-radius:8px} pre{background:#0b1020;color:#e6e6e6;padding:10px;border-radius:10px;overflow:auto}</style>";

  h += "<h2>Comunito ESP32 — Gate + 2x Wiegand</h2>";
  h += "<div class='muted'>AP: <code>COMUNITO-ESP32</code> PASS <code>comunito123</code> • IP <code>"+WiFi.softAPIP().toString()+"</code>";
  h += " • STA: <code>"+WiFi.localIP().toString()+"</code> • mDNS: <code>http://"+htmlEscape(cfg.hostname)+".local</code> (si STA)</div>";

  h += "<div class='card'><b>Acciones rápidas</b><div style='margin-top:10px'>";
  h += "<button class='btn' onclick='pulse(1)'>🟩 Pulso Gate 1</button> ";
  h += "<button class='btn' onclick='pulse(2)'>🟩 Pulso Gate 2</button> ";
  h += "<a class='btn' href='/status'>Ver status JSON</a>";
  h += "</div><div class='muted' id='msg'></div></div>";

  h += "<form method='POST' action='/save'>";

  h += "<div class='card'><h3>WiFi / Hostname (opcional STA)</h3>";
  h += "<div class='grid'>";
  h += "<label>STA SSID<br><input name='sta_ssid' value='"+htmlEscape(cfg.sta_ssid)+"' placeholder='(opcional)'></label>";
  h += "<label>STA PASS<br><input name='sta_pass' value='"+htmlEscape(cfg.sta_pass)+"' placeholder='(opcional)'></label>";
  h += "<label>Hostname mDNS<br><input name='host' value='"+htmlEscape(cfg.hostname)+"' placeholder='gate-esp32'></label>";
  h += "</div>";
  h += "</div>";

  h += "<div class='card'><h3>Gate (2 cámaras)</h3>";
  h += "<div class='grid3'>";
  h += "<label>Token HTTP /pulse<br><input name='gtok' value='"+htmlEscape(cfg.gate_token)+"'></label>";
  h += "<label>Gate1 Pin (Cam1)<br><input type='number' name='g1p' value='"+String(cfg.gate1_pin)+"'></label>";
  h += "<label>Gate2 Pin (Cam2)<br><input type='number' name='g2p' value='"+String(cfg.gate2_pin)+"'></label>";
  h += "<label>Active Low<br><select name='gal'><option value='0'"+boolSel(cfg.gate_active_low,false)+">NO</option><option value='1'"+boolSel(cfg.gate_active_low,true)+">SI</option></select></label>";
  h += "<label>Pulso default (ms)<br><input type='number' name='gpms' value='"+String(cfg.gate_pulse_ms_def)+"'></label>";
  h += "</div>";
  h += "</div>";

  h += "<div class='card'><h3>Wiegand — Pines + Anti-repetición</h3>";
  h += "<div class='grid3'>";
  h += "<label>Wiegand1 D0<br><input type='number' name='w1d0' value='"+String(cfg.wg1_d0)+"'></label>";
  h += "<label>Wiegand1 D1<br><input type='number' name='w1d1' value='"+String(cfg.wg1_d1)+"'></label>";
  h += "<label>Anti-repeat1 (ms)<br><input type='number' name='w1ar' value='"+String(cfg.wg1_antirepeat_ms)+"'></label>";
  h += "<label>Wiegand2 D0<br><input type='number' name='w2d0' value='"+String(cfg.wg2_d0)+"'></label>";
  h += "<label>Wiegand2 D1<br><input type='number' name='w2d1' value='"+String(cfg.wg2_d1)+"'></label>";
  h += "<label>Anti-repeat2 (ms)<br><input type='number' name='w2ar' value='"+String(cfg.wg2_antirepeat_ms)+"'></label>";
  h += "<label>Frame gap (ms)<br><input type='number' name='wgap' value='"+String(cfg.wg_frame_gap_ms)+"'></label>";
  h += "</div>";
  h += "</div>";

  auto mappingBlock=[&](int cam)->String{
    String mode = (cam==1)?cfg.out1_mode:cfg.out2_mode;
    bool strip = (cam==1)?cfg.strip1_parity:cfg.strip2_parity;
    bool rev   = (cam==1)?cfg.rev1_bits:cfg.rev2_bits;
    int pad    = (cam==1)?cfg.pad1_len:cfg.pad2_len;
    int ss     = (cam==1)?cfg.slice1_start:cfg.slice2_start;
    int sl     = (cam==1)?cfg.slice1_len:cfg.slice2_len;

    String pre = (cam==1)?"1":"2";
    String b;
    b += "<h4>Salida Cam"+String(cam)+"/Lectora"+String(cam)+"</h4>";
    b += "<div class='grid3'>";
    b += "<label>Modo salida<br><select name='o"+pre+"m'>"
         "<option value='RAW_HEX'"+optionSel(mode,"RAW_HEX")+">RAW_HEX</option>"
         "<option value='RAW_DEC'"+optionSel(mode,"RAW_DEC")+">RAW_DEC</option>"
         "<option value='WG26_CARD'"+optionSel(mode,"WG26_CARD")+">WG26_CARD</option>"
         "<option value='WG26_FAC_CARD'"+optionSel(mode,"WG26_FAC_CARD")+">WG26_FAC_CARD</option>"
         "<option value='CUSTOM_SLICE'"+optionSel(mode,"CUSTOM_SLICE")+">CUSTOM_SLICE</option>"
         "</select></label>";
    b += "<label>Strip parity<br><select name='s"+pre+"p'><option value='0'"+boolSel(strip,false)+">NO</option><option value='1'"+boolSel(strip,true)+">SI</option></select></label>";
    b += "<label>Reverse bits<br><select name='r"+pre+"b'><option value='0'"+boolSel(rev,false)+">NO</option><option value='1'"+boolSel(rev,true)+">SI</option></select></label>";
    b += "<label>Pad zeros length<br><input type='number' name='p"+pre+"l' value='"+String(pad)+"'></label>";
    b += "<label>Slice start<br><input type='number' name='c"+pre+"s' value='"+String(ss)+"'></label>";
    b += "<label>Slice len<br><input type='number' name='c"+pre+"l' value='"+String(sl)+"'></label>";
    b += "</div>";
    return b;
  };

  h += "<div class='card'><h3>Mapping de tag</h3>";
  h += mappingBlock(1);
  h += "<hr style='border:none;border-top:1px solid #eee;margin:12px 0'>";
  h += mappingBlock(2);
  h += "</div>";

  h += "<div class='card'><h3>Enviar tag al Pi</h3>";
  h += "<div class='grid'>";
  h += "<label>Modo envío<br><select name='tsm'>"
       "<option value='http'"+optionSel(cfg.tag_send_mode,"http")+">HTTP</option>"
       "<option value='serial'"+optionSel(cfg.tag_send_mode,"serial")+">SERIAL</option>"
       "</select></label>";
  h += "<label>Portal URL (Pi)<br><input name='purl' value='"+htmlEscape(cfg.portal_url)+"'></label>";
  h += "<label>Portal API Key (opcional)<br><input name='pak' value='"+htmlEscape(cfg.portal_api_key)+"'></label>";
  h += "</div>";
  h += "</div>";

  h += "<div class='card'><button class='btn' type='submit'>💾 Guardar config</button> <a class='btn' href='/'>Refrescar</a></div>";

  h += "<div class='card'><h3>Vista en vivo</h3><pre id='live'>cargando…</pre></div>";

  h += "</form>";

  h += "<script>"
       "async function pulse(g){"
       "  let r=await fetch('/pulse?token='+encodeURIComponent('"+htmlEscape(cfg.gate_token)+"')+'&cam='+g+'&ms='+("+String(cfg.gate_pulse_ms_def)+"));"
       "  let j=await r.json().catch(()=>({}));"
       "  document.getElementById('msg').textContent = j.ok?('OK gate '+j.gate):('Error '+(j.error||''));"
       "}"
       "async function poll(){"
       "  try{const r=await fetch('/status'); const j=await r.json(); document.getElementById('live').textContent=JSON.stringify(j,null,2);}catch(e){}"
       "}"
       "setInterval(poll,800); poll();"
       "</script>";

  return h;
}

void handleRoot(){ server.send(200, "text/html; charset=utf-8", pageIndex()); }

void handleSave(){
  cfg.sta_ssid = server.arg("sta_ssid");
  cfg.sta_pass = server.arg("sta_pass");
  cfg.hostname = server.arg("host");
  if(!cfg.hostname.length()) cfg.hostname="gate-esp32";

  cfg.gate_token = server.arg("gtok");
  cfg.gate1_pin = toIntSafe(server.arg("g1p"), cfg.gate1_pin);
  cfg.gate2_pin = toIntSafe(server.arg("g2p"), cfg.gate2_pin);
  cfg.gate_active_low = (server.arg("gal")=="1");
  cfg.gate_pulse_ms_def = toIntSafe(server.arg("gpms"), cfg.gate_pulse_ms_def);

  cfg.wg1_d0 = toIntSafe(server.arg("w1d0"), cfg.wg1_d0);
  cfg.wg1_d1 = toIntSafe(server.arg("w1d1"), cfg.wg1_d1);
  cfg.wg2_d0 = toIntSafe(server.arg("w2d0"), cfg.wg2_d0);
  cfg.wg2_d1 = toIntSafe(server.arg("w2d1"), cfg.wg2_d1);

  cfg.wg1_antirepeat_ms = toIntSafe(server.arg("w1ar"), cfg.wg1_antirepeat_ms);
  cfg.wg2_antirepeat_ms = toIntSafe(server.arg("w2ar"), cfg.wg2_antirepeat_ms);
  cfg.wg_frame_gap_ms = toIntSafe(server.arg("wgap"), cfg.wg_frame_gap_ms);

  cfg.out1_mode = server.arg("o1m"); if(!cfg.out1_mode.length()) cfg.out1_mode="WG26_CARD";
  cfg.out2_mode = server.arg("o2m"); if(!cfg.out2_mode.length()) cfg.out2_mode="WG26_CARD";

  cfg.strip1_parity = (server.arg("s1p")=="1");
  cfg.strip2_parity = (server.arg("s2p")=="1");

  cfg.rev1_bits = (server.arg("r1b")=="1");
  cfg.rev2_bits = (server.arg("r2b")=="1");

  cfg.pad1_len = toIntSafe(server.arg("p1l"), cfg.pad1_len);
  cfg.pad2_len = toIntSafe(server.arg("p2l"), cfg.pad2_len);

  cfg.slice1_start = toIntSafe(server.arg("c1s"), cfg.slice1_start);
  cfg.slice1_len   = toIntSafe(server.arg("c1l"), cfg.slice1_len);
  cfg.slice2_start = toIntSafe(server.arg("c2s"), cfg.slice2_start);
  cfg.slice2_len   = toIntSafe(server.arg("c2l"), cfg.slice2_len);

  cfg.tag_send_mode = server.arg("tsm"); if(!cfg.tag_send_mode.length()) cfg.tag_send_mode="http";
  cfg.portal_url = server.arg("purl");
  cfg.portal_api_key = server.arg("pak");

  saveCfg();

  pinMode(cfg.gate1_pin, OUTPUT);
  pinMode(cfg.gate2_pin, OUTPUT);
  int idle = cfg.gate_active_low ? HIGH : LOW;
  digitalWrite(cfg.gate1_pin, idle);
  digitalWrite(cfg.gate2_pin, idle);

  pinMode(cfg.wg1_d0, INPUT_PULLUP);
  pinMode(cfg.wg1_d1, INPUT_PULLUP);
  pinMode(cfg.wg2_d0, INPUT_PULLUP);
  pinMode(cfg.wg2_d1, INPUT_PULLUP);

  detachInterrupt(cfg.wg1_d0);
  detachInterrupt(cfg.wg1_d1);
  detachInterrupt(cfg.wg2_d0);
  detachInterrupt(cfg.wg2_d1);

  attachInterrupt(digitalPinToInterrupt(cfg.wg1_d0), isrWg1D0, FALLING);
  attachInterrupt(digitalPinToInterrupt(cfg.wg1_d1), isrWg1D1, FALLING);
  attachInterrupt(digitalPinToInterrupt(cfg.wg2_d0), isrWg2D0, FALLING);
  attachInterrupt(digitalPinToInterrupt(cfg.wg2_d1), isrWg2D1, FALLING);

  if(WiFi.status()==WL_CONNECTED){
    MDNS.end();
    MDNS.begin(cfg.hostname.c_str());
  }

  server.sendHeader("Location", "/");
  server.send(303);
}

void handleNotFound(){
  server.sendHeader("Location", "/");
  server.send(302, "text/plain", "redirect");
}

// ---------------------------
// WiFi init
// ---------------------------
void startAP(){
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PASS);
  dns.start(DNS_PORT, "*", WiFi.softAPIP());
}
void startSTA(){
  if(!cfg.sta_ssid.length()) return;
  WiFi.begin(cfg.sta_ssid.c_str(), cfg.sta_pass.c_str());
}

// ---------------------------
// Setup / Loop
// ---------------------------
void setup(){
  Serial.begin(115200);
  delay(200);

  loadCfg();

  startAP();
  startSTA();

  pinMode(cfg.gate1_pin, OUTPUT);
  pinMode(cfg.gate2_pin, OUTPUT);
  int idle = cfg.gate_active_low ? HIGH : LOW;
  digitalWrite(cfg.gate1_pin, idle);
  digitalWrite(cfg.gate2_pin, idle);

  pinMode(cfg.wg1_d0, INPUT_PULLUP);
  pinMode(cfg.wg1_d1, INPUT_PULLUP);
  pinMode(cfg.wg2_d0, INPUT_PULLUP);
  pinMode(cfg.wg2_d1, INPUT_PULLUP);

  attachInterrupt(digitalPinToInterrupt(cfg.wg1_d0), isrWg1D0, FALLING);
  attachInterrupt(digitalPinToInterrupt(cfg.wg1_d1), isrWg1D1, FALLING);
  attachInterrupt(digitalPinToInterrupt(cfg.wg2_d0), isrWg2D0, FALLING);
  attachInterrupt(digitalPinToInterrupt(cfg.wg2_d1), isrWg2D1, FALLING);

  server.on("/", HTTP_GET, handleRoot);
  server.on("/save", HTTP_POST, handleSave);
  server.on("/pulse", HTTP_ANY, handlePulse);
  server.on("/status", HTTP_GET, handleStatus);
  server.onNotFound(handleNotFound);
  server.begin();
}

bool mdnsStarted=false;
uint32_t lastWiFiCheck=0;

void loop(){
  dns.processNextRequest();
  server.handleClient();

  pollSerial();
  pollWiegand();

  if((msNow()-lastWiFiCheck) > 1200){
    lastWiFiCheck = msNow();
    if(WiFi.status()==WL_CONNECTED && !mdnsStarted){
      if(MDNS.begin(cfg.hostname.c_str())){
        mdnsStarted=true;
      }
    }
  }

  delay(1);
}
