/**
 * =============================================================================
 *  ESP32 – Aquisição ADC a 10 kHz + Envio TCP via FreeRTOS
 * =============================================================================
 *
 *  Arquitetura:
 *  ┌─────────────────────────────────────────────────────────────────────┐
 *  │  esp_timer (ISR, 100 µs)  ──►  adc_buf[wr_buf][wr_idx]             │
 *  │       │ (buffer cheio a cada 1 s)                                   │
 *  │       ▼  xSemaphoreGiveFromISR                                      │
 *  │  send_task  ──►  TCP send(adc_buf[send_buf])  ──►  memset(0)        │
 *  └─────────────────────────────────────────────────────────────────────┘
 *
 *  Double-buffer (ping-pong):
 *    - Buffer 0 ou 1 é preenchido pelo timer ISR
 *    - Quando cheio, troca de buffer e sinaliza send_task via semáforo
 *    - send_task envia o buffer inativo (recém-preenchido) e o zera
 *    - Os dois buffers nunca são acessados simultaneamente
 *
 *  Memória:
 *    2 buffers × 10.000 amostras × 4 bytes = 80 KB (ESP32 tem 520 KB SRAM)
 *
 *  Requisitos:
 *    - ESP-IDF >= 4.3  (para ESP_TIMER_ISR dispatch method)
 *    - WiFi configurado com SSID/senha corretos
 *    - Servidor TCP rodando no PC (ver server.py)
 *
 *  Pino ADC: GPIO36 (ADC1_CHANNEL_0) – não use WiFi com ADC2
 * =============================================================================
 */

#include <stdio.h>
#include <string.h>
#include <errno.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"

#include "esp_timer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs_flash.h"

#include "driver/adc.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"

/* ═══════════════════════════════════════════════════════════════════════════
   CONFIGURAÇÕES – edite aqui antes de compilar
   ═══════════════════════════════════════════════════════════════════════════ */

#define WIFI_SSID           "SUA_REDE_WIFI"       // SSID da rede
#define WIFI_PASS           "SUA_SENHA_WIFI"       // Senha da rede
#define SERVER_IP           "192.168.1.100"        // IP do computador receptor
#define SERVER_PORT         5000                   // Porta TCP do servidor

#define ADC_CHANNEL         ADC1_CHANNEL_0         // GPIO36 (não altere para ADC2 + WiFi)
#define ADC_BIT_WIDTH       ADC_WIDTH_BIT_12       // Resolução 12 bits (0–4095)
#define ADC_ATTEN           ADC_ATTEN_DB_11        // Faixa ~0–3.9 V

#define SAMPLE_RATE_HZ      10000                  // Frequência de amostragem: 10 kHz
#define SAMPLES_PER_SEC     SAMPLE_RATE_HZ         // Amostras por buffer (1 segundo)
#define TIMER_PERIOD_US     (1000000 / SAMPLE_RATE_HZ)  // 100 µs por amostra

/* ═══════════════════════════════════════════════════════════════════════════
   DOUBLE-BUFFER (PING-PONG)
   ═══════════════════════════════════════════════════════════════════════════ */

// Dois buffers de 10.000 inteiros cada (80 KB total)
static int adc_buf[2][SAMPLES_PER_SEC];

// Índices voláteis – escritos pelo ISR do timer, lidos pela send_task
static volatile int wr_buf = 0;   // buffer sendo preenchido agora (0 ou 1)
static volatile int wr_idx = 0;   // posição atual dentro do buffer ativo

/* ═══════════════════════════════════════════════════════════════════════════
   PRIMITIVAS DE SINCRONIZAÇÃO
   ═══════════════════════════════════════════════════════════════════════════ */

static SemaphoreHandle_t buf_ready_sem;    // sinalizado quando buffer fica cheio
static EventGroupHandle_t wifi_events;
#define WIFI_CONNECTED_BIT  BIT0

static const char *TAG = "ADC_STREAM";

/* ═══════════════════════════════════════════════════════════════════════════
   CALLBACK DO TIMER – executado a cada 100 µs (10 kHz)
   Colocado em IRAM para latência mínima
   ═══════════════════════════════════════════════════════════════════════════ */
static void IRAM_ATTR adc_timer_cb(void *arg)
{
    // Lê o ADC e armazena no buffer ativo
    adc_buf[wr_buf][wr_idx] = adc1_get_raw(ADC_CHANNEL);
    wr_idx++;

    // Buffer cheio? (1 segundo de dados = 10.000 amostras)
    if (wr_idx >= SAMPLES_PER_SEC) {
        wr_idx = 0;

        // Troca atomicamente para o outro buffer
        // A send_task usará (1 - wr_buf_novo) = buffer recém-preenchido
        wr_buf = 1 - wr_buf;

        // Notifica a send_task a partir do contexto de ISR
        BaseType_t higher_prio_woken = pdFALSE;
        xSemaphoreGiveFromISR(buf_ready_sem, &higher_prio_woken);
        portYIELD_FROM_ISR(higher_prio_woken);
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
   SEND TASK – aguarda buffer cheio, envia via TCP e zera
   ═══════════════════════════════════════════════════════════════════════════ */
static void send_task(void *pvParameters)
{
    // Aguarda WiFi estar conectado antes de qualquer envio
    ESP_LOGI(TAG, "[send_task] Aguardando conexão WiFi...");
    xEventGroupWaitBits(wifi_events, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "[send_task] WiFi pronto. Iniciando loop de envio.");

    // Endereço do servidor TCP
    struct sockaddr_in server_addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(SERVER_PORT),
        .sin_addr.s_addr = inet_addr(SERVER_IP),
    };

    while (1) {
        // ── 1. Bloqueia até o ISR sinalizar buffer cheio ────────────────
        xSemaphoreTake(buf_ready_sem, portMAX_DELAY);

        // O ISR já trocou wr_buf → o buffer cheio é o ANTERIOR ao atual
        int send_buf = 1 - wr_buf;

        ESP_LOGI(TAG, "[send_task] Buffer %d pronto (%d amostras). Enviando...",
                 send_buf, SAMPLES_PER_SEC);

        // ── 2. Cria socket TCP ──────────────────────────────────────────
        int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock < 0) {
            ESP_LOGE(TAG, "socket() falhou: errno %d", errno);
            goto clear_and_continue;
        }

        // Timeout de 5 s para connect e send
        struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
        setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        if (connect(sock, (struct sockaddr *)&server_addr,
                    sizeof(server_addr)) != 0) {
            ESP_LOGE(TAG, "connect() falhou: errno %d", errno);
            close(sock);
            goto clear_and_continue;
        }

        // ── 3. Envia cabeçalho: número de amostras (uint32_t, 4 bytes) ──
        uint32_t n = (uint32_t)SAMPLES_PER_SEC;
        if (send(sock, &n, sizeof(n), 0) < 0) {
            ESP_LOGE(TAG, "send() header falhou: errno %d", errno);
            close(sock);
            goto clear_and_continue;
        }

        // ── 4. Envia o payload em loop (send pode não enviar tudo) ──────
        const uint8_t *ptr   = (const uint8_t *)adc_buf[send_buf];
        size_t         total = (size_t)SAMPLES_PER_SEC * sizeof(int);
        size_t         sent  = 0;

        while (sent < total) {
            int ret = send(sock, ptr + sent, total - sent, 0);
            if (ret < 0) {
                ESP_LOGE(TAG, "send() dados falhou em offset %d: errno %d",
                         (int)sent, errno);
                break;
            }
            sent += (size_t)ret;
        }

        close(sock);
        ESP_LOGI(TAG, "[send_task] Enviados %d / %d bytes.", (int)sent, (int)total);

    clear_and_continue:
        // ── 5. Zera o buffer enviado ────────────────────────────────────
        memset(adc_buf[send_buf], 0, sizeof(adc_buf[send_buf]));
        ESP_LOGI(TAG, "[send_task] Buffer %d zerado.", send_buf);
    }
}

/* ═══════════════════════════════════════════════════════════════════════════
   GERENCIAMENTO DE WIFI
   ═══════════════════════════════════════════════════════════════════════════ */
static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *event_data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();

    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi desconectado – reconectando...");
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT);
        esp_wifi_connect();

    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "IP obtido: " IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    wifi_events = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID,    wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT,   IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid              = WIFI_SSID,
            .password          = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi iniciado → SSID: %s", WIFI_SSID);
}

/* ═══════════════════════════════════════════════════════════════════════════
   APP_MAIN
   ═══════════════════════════════════════════════════════════════════════════ */
void app_main(void)
{
    ESP_LOGI(TAG, "=== ESP32 ADC Stream @ %d Hz ===", SAMPLE_RATE_HZ);

    // ── NVS (requerido pelo WiFi) ────────────────────────────────────────
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES ||
        nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_err);

    // ── Configura ADC1 ──────────────────────────────────────────────────
    ESP_ERROR_CHECK(adc1_config_width(ADC_BIT_WIDTH));
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL, ADC_ATTEN));
    ESP_LOGI(TAG, "ADC configurado: canal %d, 12 bits, atenuação 11 dB",
             ADC_CHANNEL);

    // ── Zera buffers ────────────────────────────────────────────────────
    memset(adc_buf, 0, sizeof(adc_buf));

    // ── Semáforo binário para sinalização ISR → task ────────────────────
    buf_ready_sem = xSemaphoreCreateBinary();
    configASSERT(buf_ready_sem != NULL);

    // ── Inicia WiFi ─────────────────────────────────────────────────────
    wifi_init();

    // ── Cria send_task no core 0 (core 1 ficará para o timer ISR) ───────
    //    Stack de 8 KB é suficiente para o TCP stack do lwIP
    BaseType_t ret = xTaskCreatePinnedToCore(
        send_task,          // função
        "send_task",        // nome (debug)
        8192,               // stack em bytes
        NULL,               // parâmetro
        5,                  // prioridade (5 = moderada)
        NULL,               // handle (não usado)
        0                   // core 0
    );
    configASSERT(ret == pdPASS);

    // ── Cria e inicia o timer periódico de 10 kHz ────────────────────────
    //    ESP_TIMER_ISR: callback executado diretamente no contexto de ISR
    //    (menor latência vs ESP_TIMER_TASK; requer ESP-IDF >= 4.3)
    const esp_timer_create_args_t timer_args = {
        .callback        = adc_timer_cb,
        .arg             = NULL,
        .name            = "adc_10k",
        .dispatch_method = ESP_TIMER_ISR,
        .skip_unhandled_events = true,   // descarta eventos perdidos
    };

    esp_timer_handle_t adc_timer;
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &adc_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(adc_timer, TIMER_PERIOD_US));

    ESP_LOGI(TAG, "Timer de %d kHz iniciado (período: %d µs).",
             SAMPLE_RATE_HZ / 1000, TIMER_PERIOD_US);
    ESP_LOGI(TAG, "Aquisição em andamento → servidor %s:%d",
             SERVER_IP, SERVER_PORT);

    // app_main retorna: as tasks e o timer continuam rodando
}
