/**
 * =============================================================================
 *  ESP32 - Aquisicao ADC MULTICANAL + SoftAP + Servidor TCP (FreeRTOS)
 * =============================================================================
 *
 *  Le N_CH canais ADC (um por ampop/fotodiodo) e envia TODOS os canais
 *  juntos em UM UNICO pacote por segundo (1 requisicao/s, independente do
 *  numero de fotodiodos).
 *
 *  TOPOLOGIA (conexao direta, sem roteador):
 *    O ESP32 cria o hotspot "ESP32_ADC" e roda o servidor TCP em
 *    192.168.4.1:5000. O PC conecta nessa rede e abre a conexao como CLIENTE
 *    (ver server.py).
 *
 *  AMOSTRAGEM (intercalada / frame-major):
 *    A cada tick (1 frame) le-se os N_CH canais em sequencia:
 *        frame f -> [ch0_f, ch1_f, ..., chN_f]
 *    Servidor de-intercala: canal c = vals[c::N_CH].
 *
 *  PROTOCOLO (little-endian, por pacote):
 *    [4 bytes]               uint32  N_FRAMES
 *    [4 bytes]               uint32  N_CH
 *    [N_FRAMES*N_CH*2 bytes] uint16  amostras[] (ADC bruto 0..4095, intercalado)
 *
 *  MEMORIA:
 *    Os buffers (2 * N_FRAMES * N_CH * 2 bytes) sao alocados no HEAP em
 *    runtime (malloc no setup), NAO em .bss estatico. Isso evita o estouro
 *    de DRAM no link e mantem os 5000 frames. Default = 80 KB no heap.
 *    Se o malloc falhar na sua placa, reduza FRAMES_PER_BUF.
 *
 *  Pinos ADC1 usaveis: GPIO36(CH0) 39(CH3) 34(CH6) 35(CH7) 32(CH4) 33(CH5).
 *  Nao use ADC2 com Wi-Fi. Compila no core Arduino-ESP32.
 * =============================================================================
 */

#include <WiFi.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_timer.h"
#include "driver/adc.h"
#include "lwip/sockets.h"

/* -- Configuracoes de rede --------------------------------------------- */
#define AP_SSID        "ESP32_ADC"
#define AP_PASS        "12345678"        // WPA2 (>= 8 caracteres)
#define TCP_PORT       5000

/* -- Canais ADC: 1 entrada por ampop/fotodiodo ------------------------- */
//  Edite N_CH e a lista CHANNELS conforme o numero de fotodiodos.
#define N_CH           4
static const adc1_channel_t CHANNELS[N_CH] = {
  ADC1_CHANNEL_0,   // GPIO36 - fotodiodo/ampop 0
  ADC1_CHANNEL_3,   // GPIO39 - fotodiodo/ampop 1
  ADC1_CHANNEL_6,   // GPIO34 - fotodiodo/ampop 2
  ADC1_CHANNEL_7,   // GPIO35 - fotodiodo/ampop 3
};

/* -- Taxa e tamanho do buffer ------------------------------------------ */
#define FRAME_RATE_HZ  5000              // frames/s por canal
#define FRAMES_PER_BUF FRAME_RATE_HZ     // 1 segundo de dados por buffer
#define TIMER_US       (1000000 / FRAME_RATE_HZ)
#define V_REF          3.9f              // ATTEN_DB_12 ~ 0..3.9 V (teorico)
#define PREVIEW_FRAMES 3                 // frames mostrados no log por canal

/* -- Double-buffer no HEAP (ponteiros), layout intercalado ------------- */
static uint16_t *adc_buf[2] = { nullptr, nullptr };
static volatile int wr_buf = 0;          // buffer sendo preenchido
static volatile int wr_idx = 0;          // frame atual dentro do buffer

static SemaphoreHandle_t buf_ready;
static volatile uint32_t pkt_count = 0;

/* ===========================================================================
   ISR DO TIMER - le os N_CH canais a cada frame
   =========================================================================== */
void IRAM_ATTR timer_cb(void *arg)
{
  int idx  = wr_idx;            // copia local (evita ++ em volatile)
  int buf  = wr_buf;
  int base = idx * N_CH;

  for (int c = 0; c < N_CH; c++)
    adc_buf[buf][base + c] = (uint16_t)adc1_get_raw(CHANNELS[c]);

  idx = idx + 1;
  if (idx >= FRAMES_PER_BUF) {
    idx = 0;
    wr_buf = buf ^ 1;          // troca de buffer
    BaseType_t hp = pdFALSE;
    xSemaphoreGiveFromISR(buf_ready, &hp);
    portYIELD_FROM_ISR(hp);
  }
  wr_idx = idx;
}

/* -- Envia exatamente 'len' bytes -------------------------------------- */
static bool send_all(int sock, const uint8_t *p, size_t len)
{
  size_t sent = 0;
  while (sent < len) {
    int r = send(sock, p + sent, len - sent, 0);
    if (r <= 0) return false;
    sent += (size_t)r;
  }
  return true;
}

/* -- Log resumido por canal do buffer recem-preenchido ----------------- */
static void log_buffer(int sb)
{
  for (int c = 0; c < N_CH; c++) {
    uint16_t mn = 4095, mx = 0;
    uint32_t acc = 0;
    for (int f = 0; f < FRAMES_PER_BUF; f++) {
      uint16_t v = adc_buf[sb][f * N_CH + c];
      if (v < mn) mn = v;
      if (v > mx) mx = v;
      acc += v;
    }
    float avg = (float)acc / FRAMES_PER_BUF;

    char prev[48];
    int off = 0;
    for (int f = 0; f < PREVIEW_FRAMES; f++)
      off += snprintf(prev + off, sizeof(prev) - off, "%u ",
                      adc_buf[sb][f * N_CH + c]);

    Serial.printf("[ADC] ch%d | min=%u max=%u avg=%.1f | Vavg=%.3fV | %s...\n",
                  c, mn, mx, avg, avg / 4095.0f * V_REF, prev);
  }
}

/* ===========================================================================
   SEND TASK - servidor TCP: aceita o PC e faz streaming dos buffers
   =========================================================================== */
static void send_task(void *arg)
{
  int listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  struct sockaddr_in srv;
  memset(&srv, 0, sizeof(srv));
  srv.sin_family      = AF_INET;
  srv.sin_addr.s_addr = htonl(INADDR_ANY);
  srv.sin_port        = htons(TCP_PORT);

  int opt = 1;
  setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
  bind(listen_sock, (struct sockaddr *)&srv, sizeof(srv));
  listen(listen_sock, 1);
  Serial.printf("[NET] Servidor TCP ouvindo em 192.168.4.1:%d\n", TCP_PORT);

  for (;;) {
    Serial.println("[NET] Aguardando o servidor (PC) conectar no hotspot...");
    struct sockaddr_in cli;
    socklen_t cl = sizeof(cli);
    int client = accept(listen_sock, (struct sockaddr *)&cli, &cl);
    if (client < 0) { vTaskDelay(pdMS_TO_TICKS(100)); continue; }
    Serial.printf("[NET] Cliente conectado: %s\n", inet_ntoa(cli.sin_addr));

    xSemaphoreTake(buf_ready, 0);   // comeca no proximo buffer cheio

    bool ok = true;
    while (ok) {
      xSemaphoreTake(buf_ready, portMAX_DELAY);
      int sb = 1 - wr_buf;          // buffer recem-preenchido

      log_buffer(sb);

      uint32_t hdr[2] = { (uint32_t)FRAMES_PER_BUF, (uint32_t)N_CH };
      size_t payload = (size_t)FRAMES_PER_BUF * N_CH * sizeof(uint16_t);

      ok = send_all(client, (const uint8_t *)hdr, sizeof(hdr));
      if (ok)
        ok = send_all(client, (const uint8_t *)adc_buf[sb], payload);

      if (ok) {
        pkt_count = pkt_count + 1;   // evita ++ em volatile
        Serial.printf("[NET] Pacote #%lu ENVIADO: %d canais x %d frames "
                      "(%u bytes)\n",
                      (unsigned long)pkt_count, N_CH, FRAMES_PER_BUF,
                      (unsigned)(sizeof(hdr) + payload));
      } else {
        Serial.println("[NET] Falha no envio. Encerrando conexao.");
      }
    }
    close(client);
    Serial.println("[NET] Conexao encerrada.\n");
  }
}

/* ===========================================================================
   SETUP / LOOP
   =========================================================================== */
void setup()
{
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== ESP32 ADC Stream MULTICANAL (SoftAP + Servidor TCP) ===");

  // Aloca os buffers no heap (evita estouro de .bss no link)
  size_t buf_bytes = (size_t)FRAMES_PER_BUF * N_CH * sizeof(uint16_t);
  Serial.printf("[MEM] Heap livre antes: %u bytes\n", ESP.getFreeHeap());
  for (int b = 0; b < 2; b++) {
    adc_buf[b] = (uint16_t *)malloc(buf_bytes);
    if (adc_buf[b] == nullptr) {
      Serial.printf("[MEM] FALHA ao alocar buffer %d (%u bytes). "
                    "Reduza FRAMES_PER_BUF.\n", b, (unsigned)buf_bytes);
      while (true) delay(1000);
    }
    memset(adc_buf[b], 0, buf_bytes);
  }
  Serial.printf("[MEM] 2 buffers de %u bytes alocados. Heap livre: %u bytes\n",
                (unsigned)buf_bytes, ESP.getFreeHeap());

  // ADC1: 12 bits + atenuacao por canal (DB_12 substitui o antigo DB_11)
  adc1_config_width(ADC_WIDTH_BIT_12);
  for (int c = 0; c < N_CH; c++)
    adc1_config_channel_atten(CHANNELS[c], ADC_ATTEN_DB_12);
  Serial.printf("[ADC] %d canais configurados @ %d frames/s por canal.\n",
                N_CH, FRAME_RATE_HZ);

  // SoftAP -> ESP32 em 192.168.4.1
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("[AP] Rede '%s' criada (senha '%s'). IP: %s\n",
                AP_SSID, AP_PASS, WiFi.softAPIP().toString().c_str());

  buf_ready = xSemaphoreCreateBinary();
  xTaskCreatePinnedToCore(send_task, "send", 8192, NULL, 5, NULL, 0);

  esp_timer_create_args_t targs = {};
  targs.callback              = timer_cb;
  targs.name                  = "adc";
  targs.dispatch_method       = ESP_TIMER_TASK;
  targs.skip_unhandled_events = true;

  esp_timer_handle_t htimer;
  esp_timer_create(&targs, &htimer);
  esp_timer_start_periodic(htimer, TIMER_US);

  Serial.printf("[ADC] Amostragem iniciada (periodo do frame: %d us).\n",
                TIMER_US);
}

void loop()
{
  vTaskDelay(portMAX_DELAY);
}
