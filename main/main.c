#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_bt.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_hid_common.h"
#include "esp_hidd.h"
#include "esp_hidd_gatts.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "cJSON.h"
#include "esp_hid_gap.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "nvs_flash.h"

#define BUTTON_COUNT 5
#define COOLDOWN_US 2000000LL
#define KEY_DELAY_MS 150
#define KEY_REPORT_ID 1
#define POLL_INTERVAL_MS 700
#define HID_KEY_ESCAPE 0x29

#define HID_KEY_0 0x27
#define HID_KEY_1 0x1E
#define HID_KEY_5 0x22
#define HID_KEY_6 0x23
#define HID_KEY_7 0x24
#define HID_KEY_8 0x25
#define HID_KEY_9 0x26

static const char *TAG = "tv_remote";

typedef struct {
    gpio_num_t gpio;
    const uint8_t *keys;
    size_t key_count;
} button_config_t;

static const uint8_t seq_157[] = { HID_KEY_1, HID_KEY_5, HID_KEY_7 };
static const uint8_t seq_68[] = { HID_KEY_6, HID_KEY_8 };
static const uint8_t seq_69[] = { HID_KEY_6, HID_KEY_9 };
static const uint8_t seq_15[] = { HID_KEY_1, HID_KEY_5 };
static const uint8_t seq_000[] = { HID_KEY_0, HID_KEY_0, HID_KEY_0 };

/* Example assignments only: check the pinout and strapping pins for your board. */
static const button_config_t buttons[BUTTON_COUNT] = {
    { GPIO_NUM_0, seq_157, sizeof(seq_157) },
    { GPIO_NUM_1, seq_68, sizeof(seq_68) },
    { GPIO_NUM_2, seq_69, sizeof(seq_69) },
    { GPIO_NUM_3, seq_15, sizeof(seq_15) },
    { GPIO_NUM_4, seq_000, sizeof(seq_000) },
};

static const uint8_t keyboard_report_map[] = {
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, KEY_REPORT_ID,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x03,
    /* Extend the array to include Xiaomi's reported keyboard-page Back usage 0xF1. */
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xF1, 0x00,
    0x05, 0x07, 0x19, 0x00, 0x2A, 0xF1, 0x00, 0x81, 0x00, 0xC0,
};
static esp_hid_raw_report_map_t report_maps[] = {
    { .data = keyboard_report_map, .len = sizeof(keyboard_report_map) },
};
static char ble_name[32];
static char device_id[32];
static char backend_url[256];
static esp_hidd_dev_t *hid_device;
static int64_t last_press_us;
static char http_response[512];
static size_t http_response_len;
static EventGroupHandle_t wifi_events;
#define WIFI_HAS_IP BIT0

static esp_err_t load_settings(wifi_config_t *wifi)
{
    nvs_handle_t nvs;
    char ssid[33] = {0};
    char password[65] = {0};
    char id[32] = {0};
    char url[256] = {0};
    esp_err_t err = nvs_open("tv_remote", NVS_READONLY, &nvs);
    if (err != ESP_OK) return err;

    size_t ssid_len = sizeof(ssid);
    size_t password_len = sizeof(password);
    size_t id_len = sizeof(id);
    size_t name_len = sizeof(ble_name);
    size_t url_len = sizeof(url);
    err = nvs_get_str(nvs, "ssid", ssid, &ssid_len);
    if (err == ESP_OK) err = nvs_get_str(nvs, "password", password, &password_len);
    if (err == ESP_OK) err = nvs_get_str(nvs, "ble_name", ble_name, &name_len);
    if (err == ESP_OK) err = nvs_get_str(nvs, "device_id", id, &id_len);
    if (err == ESP_OK) err = nvs_get_str(nvs, "backend_url", url, &url_len);
    nvs_close(nvs);
    if (err == ESP_OK && (ssid_len <= 1 || name_len <= 1 || id_len <= 1 || url_len <= 1)) return ESP_ERR_INVALID_SIZE;
    if (err == ESP_OK) {
        memcpy(wifi->sta.ssid, ssid, ssid_len);
        memcpy(wifi->sta.password, password, password_len);
        memcpy(device_id, id, id_len);
        memcpy(backend_url, url, url_len);
    }
    return err;
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_HAS_IP);
        ESP_LOGW(TAG, "Wi-Fi disconnected; retrying");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "Wi-Fi connected, IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_HAS_IP);
    }
}

static void start_wifi(const wifi_config_t *wifi)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, (wifi_config_t *)wifi));
    ESP_ERROR_CHECK(esp_wifi_start());
}

static void hid_event_handler(void *arg, esp_event_base_t base, int32_t id, void *event_data)
{
    switch ((esp_hidd_event_t)id) {
    case ESP_HIDD_START_EVENT:
        ESP_LOGI(TAG, "BLE HID started; advertising as %s", ble_name);
        ESP_ERROR_CHECK(esp_hid_ble_gap_adv_start());
        break;
    case ESP_HIDD_CONNECT_EVENT:
        ESP_LOGI(TAG, "BLE HID connected");
        break;
    case ESP_HIDD_DISCONNECT_EVENT:
        ESP_LOGI(TAG, "BLE HID disconnected; restarting advertising");
        ESP_ERROR_CHECK(esp_hid_ble_gap_adv_start());
        break;
    default:
        break;
    }
}

static esp_err_t send_key(uint8_t usage)
{
    uint8_t report[8] = {0};
    report[2] = usage;
    esp_err_t err = esp_hidd_dev_input_set(hid_device, 0, KEY_REPORT_ID, report, sizeof(report));
    if (err != ESP_OK) return err;
    vTaskDelay(pdMS_TO_TICKS(50));
    memset(report, 0, sizeof(report));
    return esp_hidd_dev_input_set(hid_device, 0, KEY_REPORT_ID, report, sizeof(report));
}

static esp_err_t http_event_handler(esp_http_client_event_t *event)
{
    if (event->event_id == HTTP_EVENT_ON_DATA && event->data && event->data_len > 0) {
        size_t available = sizeof(http_response) - http_response_len - 1;
        size_t copy_len = (size_t)event->data_len < available ? (size_t)event->data_len : available;
        memcpy(http_response + http_response_len, event->data, copy_len);
        http_response_len += copy_len;
        http_response[http_response_len] = '\0';
    }
    return ESP_OK;
}

static bool post_json(const char *path, const char *payload, char *response, size_t response_size)
{
    char url[320];
    if (snprintf(url, sizeof(url), "%s%s", backend_url, path) >= (int)sizeof(url)) return false;
    http_response_len = 0;
    http_response[0] = '\0';
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .event_handler = http_event_handler,
        .timeout_ms = 5000,
        .buffer_size = 512,
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return false;
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, payload, (int)strlen(payload));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    bool ok = err == ESP_OK && status >= 200 && status < 300;
    if (ok && response && response_size > 0) strlcpy(response, http_response, response_size);
    if (!ok) ESP_LOGW(TAG, "Backend request %s failed: %s (HTTP %d)", path, esp_err_to_name(err), status);
    esp_http_client_cleanup(client);
    return ok;
}

static uint8_t command_key(const char *command)
{
    if (!strcmp(command, "UP")) return 0x52;
    if (!strcmp(command, "DOWN")) return 0x51;
    if (!strcmp(command, "LEFT")) return 0x50;
    if (!strcmp(command, "RIGHT")) return 0x4F;
    if (!strcmp(command, "ENTER")) return 0x28;
    /* TVs commonly map the standard keyboard Escape key to Back. */
    if (!strcmp(command, "BACK")) return HID_KEY_ESCAPE;
    /* Community HID captures report Xiaomi Remote 2 Pro Back as Keyboard usage 0xF1. */
    if (!strcmp(command, "BACK_XIAOMI")) return 0xF1;
    if (command[0] >= '0' && command[0] <= '9' && command[1] == '\0')
        return command[0] == '0' ? HID_KEY_0 : (uint8_t)(HID_KEY_1 + command[0] - '1');
    return 0;
}


static void diag_connect(void)  /* TEMP DIAGNOSTIC */
{
    const char *host = strstr(backend_url, "://") + 3;
    char h[96]; size_t n = strcspn(host, ":/"); memcpy(h, host, n); h[n] = 0;
    struct addrinfo hints = {.ai_socktype = SOCK_STREAM}, *res = NULL;
    int64_t t0 = esp_timer_get_time();
    int rc = getaddrinfo(h, "443", &hints, &res);
    ESP_LOGW(TAG, "DIAG getaddrinfo(%s)=%d in %lld ms", h, rc, (esp_timer_get_time() - t0) / 1000);
    for (struct addrinfo *a = res; a; a = a->ai_next) {
        char ip[64] = "?";
        if (a->ai_family == AF_INET) inet_ntop(AF_INET, &((struct sockaddr_in *)a->ai_addr)->sin_addr, ip, sizeof(ip));
        ESP_LOGW(TAG, "DIAG family=%d addr=%s", a->ai_family, ip);
        int fd = socket(a->ai_family, SOCK_STREAM, 0);
        struct timeval tv = {.tv_sec = 5};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        t0 = esp_timer_get_time();
        int c = connect(fd, a->ai_addr, a->ai_addrlen);
        ESP_LOGW(TAG, "DIAG connect=%d errno=%d in %lld ms", c, errno, (esp_timer_get_time() - t0) / 1000);
        close(fd);
    }
    if (res) freeaddrinfo(res);
}

static void backend_task(void *arg)
{
    (void)arg;
    bool registered = false;
    while (true) {
        xEventGroupWaitBits(wifi_events, WIFI_HAS_IP, pdFALSE, pdTRUE, portMAX_DELAY);
        diag_connect();
        char response[sizeof(http_response)];
        char poll[160];
        snprintf(poll, sizeof(poll), "{\"device_id\":\"%s\",\"name\":\"%s\"}", device_id, ble_name);
        if (post_json("/api/poll", poll, response, sizeof(response))) {
            if (!registered) {
                ESP_LOGI(TAG, "Registered with backend as %s", device_id);
                registered = true;
            }
            cJSON *root = cJSON_Parse(response);
            cJSON *command = root ? cJSON_GetObjectItemCaseSensitive(root, "command") : NULL;
            if (cJSON_IsObject(command)) {
                cJSON *id = cJSON_GetObjectItemCaseSensitive(command, "id");
                cJSON *name = cJSON_GetObjectItemCaseSensitive(command, "key");
                if (cJSON_IsString(id) && cJSON_IsString(name)) {
                    uint8_t usage = command_key(name->valuestring);
                    bool sent = usage && esp_hidd_dev_connected(hid_device) && send_key(usage) == ESP_OK;
                    if (sent) {
                        char ack[192];
                        snprintf(ack, sizeof(ack), "{\"device_id\":\"%s\",\"command_id\":\"%s\"}", device_id, id->valuestring);
                        post_json("/api/ack", ack, NULL, 0);
                    } else {
                        ESP_LOGW(TAG, "Unknown key command or BLE send failed");
                    }
                }
            }
            cJSON_Delete(root);
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
    }
}

static void configure_buttons(void)
{
    for (size_t i = 0; i < BUTTON_COUNT; ++i) {
        gpio_config_t config = {
            .pin_bit_mask = 1ULL << buttons[i].gpio,
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_ERROR_CHECK(gpio_config(&config));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    wifi_config_t wifi = {0};
    err = load_settings(&wifi);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Provisioning missing or invalid. Run scripts/flash.py to configure Wi-Fi and BLE name (err=%s).", esp_err_to_name(err));
        return;
    }

    wifi_events = xEventGroupCreate();
    if (!wifi_events) ESP_ERROR_CHECK(ESP_ERR_NO_MEM);
    start_wifi(&wifi);
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));
    ESP_ERROR_CHECK(esp_hid_gap_init(HIDD_BLE_MODE));
    ESP_ERROR_CHECK(esp_hid_ble_gap_adv_init(ESP_HID_APPEARANCE_KEYBOARD, ble_name));
    ESP_ERROR_CHECK(esp_ble_gatts_register_callback(esp_hidd_gatts_event_handler));

    esp_hid_device_config_t hid_config = {
        .vendor_id = 0xCafe,
        .product_id = 0x4004,
        .version = 0x0100,
        .device_name = ble_name,
        .manufacturer_name = "Accessible TV Remote",
        .serial_number = "ESP32C3",
        .report_maps = report_maps,
        .report_maps_len = 1,
    };
    ESP_ERROR_CHECK(esp_hidd_dev_init(&hid_config, ESP_HID_TRANSPORT_BLE, hid_event_handler, &hid_device));

    xTaskCreate(backend_task, "backend_poll", 6144, NULL, 5, NULL);

    configure_buttons();
    ESP_LOGI(TAG, "Ready. BLE HID name: %s", ble_name);
    bool was_pressed[BUTTON_COUNT] = {0};
    while (true) {
        for (size_t i = 0; i < BUTTON_COUNT; ++i) {
            bool pressed = gpio_get_level(buttons[i].gpio) == 0;
            if (pressed && !was_pressed[i]) {
                int64_t now = esp_timer_get_time();
                if (last_press_us == 0 || now - last_press_us >= COOLDOWN_US) {
                    last_press_us = now;
                    if (esp_hidd_dev_connected(hid_device)) {
                        for (size_t key = 0; key < buttons[i].key_count; ++key) {
                            err = send_key(buttons[i].keys[key]);
                            if (err != ESP_OK) ESP_LOGW(TAG, "Could not send key: %s", esp_err_to_name(err));
                            vTaskDelay(pdMS_TO_TICKS(KEY_DELAY_MS));
                        }
                    } else {
                        ESP_LOGW(TAG, "Button pressed while no BLE host is connected");
                    }
                }
            }
            was_pressed[i] = pressed;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
