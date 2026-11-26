#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
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
#include "nvs_flash.h"

#define BUTTON_COUNT 5
#define COOLDOWN_US 2000000LL
#define KEY_DELAY_MS 150
#define KEY_REPORT_ID 1
#define POLL_INTERVAL_MS 700
#define HID_KEY_ESCAPE 0x29
#define XIAOMI_REMOTE_VID 0x2717
#define XIAOMI_REMOTE_PID 0x32B9

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

/* Built-in default Bluetooth profile, used until (and unless) the server pushes one. It mirrors DEFAULT_PROFILE in
 * backend/server.py. After that the profile comes from the server and is kept in NVS, so reports, usages and the USB
 * vendor/product ID can all change without reflashing. */
static const uint8_t keyboard_report_map[] = {
    /* keyboard, report id 1 */
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x03, 0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00, 0xC0,
    /* consumer control, report id 2 */
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02, 0x15, 0x00, 0x27, 0xFF, 0xFF, 0x00, 0x00, 0x19, 0x00, 0x2B, 0xFF, 0xFF, 0x00, 0x00, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0,
    /* system control, report id 3 */
    0x05, 0x01, 0x09, 0x80, 0xA1, 0x01, 0x85, 0x03, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x19, 0x00, 0x29, 0xFF, 0x75, 0x08, 0x95, 0x01, 0x81, 0x00, 0xC0,
};
static esp_hid_raw_report_map_t report_maps[] = {
    { .data = keyboard_report_map, .len = sizeof(keyboard_report_map) },
};
#define PROFILE_MAP_MAX 512
#define PROFILE_MAX_BOOT_TRIES 3
static uint8_t profile_map[PROFILE_MAP_MAX];
static uint16_t profile_vid = XIAOMI_REMOTE_VID;
static uint16_t profile_pid = XIAOMI_REMOTE_PID;
static char profile_rev[17] = "builtin";
static char profile_failed[17];        /* rev of a stored profile that kept crashing at boot */
static bool profile_stored_in_use;     /* running a profile loaded from NVS, not yet proven stable */
static char ble_name[32];
static char device_id[32];
static char backend_url[256];
static esp_hidd_dev_t *hid_device;
static int64_t last_press_us;
static char http_response[2048];
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
    if (!ok) {
        ESP_LOGW(TAG, "Backend request %s failed: %s (HTTP %d)", path, esp_err_to_name(err), status);
        if (http_response_len > 0) ESP_LOGW(TAG, "Response body: %.200s", http_response);
        if (http_response_len > 200) ESP_LOGW(TAG, "Body cont: %.320s", http_response + 200);
    }
    esp_http_client_cleanup(client);
    return ok;
}

/* ---- Raw HID reports -------------------------------------------------------------------------------------
 * The server decides what every key means. A command carries {"reports":[{"id":N,"data":"hex","hold_ms":M}]}; the
 * firmware just sends each report, waits hold_ms, sends an all-zero release of the same length, and moves on. */
#define MAX_REPORT_BYTES 64

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int hex_decode(const char *hex, uint8_t *out, size_t max)
{
    size_t length = strlen(hex);
    if (length == 0 || (length % 2) != 0 || length / 2 > max) return -1;
    for (size_t i = 0; i < length / 2; ++i) {
        int high = hex_nibble(hex[2 * i]), low = hex_nibble(hex[2 * i + 1]);
        if (high < 0 || low < 0) return -1;
        out[i] = (uint8_t)((high << 4) | low);
    }
    return (int)(length / 2);
}

static bool send_reports(cJSON *reports)
{
    if (!cJSON_IsArray(reports) || !esp_hidd_dev_connected(hid_device)) return false;
    cJSON *item;
    cJSON_ArrayForEach(item, reports) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(item, "id");
        const cJSON *data = cJSON_GetObjectItemCaseSensitive(item, "data");
        const cJSON *hold = cJSON_GetObjectItemCaseSensitive(item, "hold_ms");
        uint8_t report[MAX_REPORT_BYTES];
        int length = cJSON_IsString(data) ? hex_decode(data->valuestring, report, sizeof(report)) : -1;
        if (!cJSON_IsNumber(id) || id->valueint < 1 || id->valueint > 255 || length < 0) return false;
        int hold_ms = cJSON_IsNumber(hold) ? hold->valueint : 50;
        if (hold_ms < 10) hold_ms = 10;
        if (hold_ms > 2000) hold_ms = 2000;
        if (esp_hidd_dev_input_set(hid_device, 0, (size_t)id->valueint, report, (size_t)length) != ESP_OK) return false;
        vTaskDelay(pdMS_TO_TICKS(hold_ms));
        memset(report, 0, (size_t)length);
        if (esp_hidd_dev_input_set(hid_device, 0, (size_t)id->valueint, report, (size_t)length) != ESP_OK) return false;
        vTaskDelay(pdMS_TO_TICKS(30));
    }
    return true;
}

/* ---- Server-pushed Bluetooth profile ---------------------------------------------------------------------
 * {"rev","vid","pid","report_map"(hex)} is stored in NVS and applied at the next boot (the HID report map can only be
 * set while the HID device is created). A stored profile that crashes the board is dropped after
 * PROFILE_MAX_BOOT_TRIES boots and reported to the server so it does not push the same revision again. */
static void load_profile(void)
{
    nvs_handle_t nvs;
    if (nvs_open("tv_remote", NVS_READWRITE, &nvs) != ESP_OK) return;
    uint8_t tries = 0;
    nvs_get_u8(nvs, "p_try", &tries);
    char rev[sizeof(profile_rev)] = {0};
    size_t rev_len = sizeof(rev), map_len = sizeof(profile_map);
    uint16_t vid = 0, pid = 0;
    bool valid = nvs_get_str(nvs, "p_rev", rev, &rev_len) == ESP_OK &&
                 nvs_get_u16(nvs, "p_vid", &vid) == ESP_OK && nvs_get_u16(nvs, "p_pid", &pid) == ESP_OK &&
                 nvs_get_blob(nvs, "p_map", profile_map, &map_len) == ESP_OK && map_len >= 8 && vid && pid;
    if (valid && tries >= PROFILE_MAX_BOOT_TRIES) {
        ESP_LOGE(TAG, "Stored Bluetooth profile %s failed %d boots; falling back to the built-in profile", rev, tries);
        strlcpy(profile_failed, rev, sizeof(profile_failed));
        nvs_erase_key(nvs, "p_rev");
        nvs_set_u8(nvs, "p_try", 0);
        nvs_commit(nvs);
        valid = false;
    } else if (valid) {
        nvs_set_u8(nvs, "p_try", (uint8_t)(tries + 1));
        nvs_commit(nvs);
        strlcpy(profile_rev, rev, sizeof(profile_rev));
        profile_vid = vid;
        profile_pid = pid;
        report_maps[0].data = profile_map;
        report_maps[0].len = map_len;
        profile_stored_in_use = true;
        ESP_LOGI(TAG, "Using stored Bluetooth profile %s (VID 0x%04X PID 0x%04X, %u byte report map)", rev, vid, pid, (unsigned)map_len);
    }
    nvs_close(nvs);
}

static void confirm_profile(void)   /* the stored profile ran long enough to be trusted */
{
    nvs_handle_t nvs;
    if (nvs_open("tv_remote", NVS_READWRITE, &nvs) != ESP_OK) return;
    nvs_set_u8(nvs, "p_try", 0);
    nvs_commit(nvs);
    nvs_close(nvs);
}

static void apply_profile(const cJSON *profile)
{
    const cJSON *rev = cJSON_GetObjectItemCaseSensitive((cJSON *)profile, "rev");
    const cJSON *vid = cJSON_GetObjectItemCaseSensitive((cJSON *)profile, "vid");
    const cJSON *pid = cJSON_GetObjectItemCaseSensitive((cJSON *)profile, "pid");
    const cJSON *map = cJSON_GetObjectItemCaseSensitive((cJSON *)profile, "report_map");
    uint8_t decoded[PROFILE_MAP_MAX];
    int map_len = cJSON_IsString(map) ? hex_decode(map->valuestring, decoded, sizeof(decoded)) : -1;
    if (!cJSON_IsString(rev) || strlen(rev->valuestring) < 1 || strlen(rev->valuestring) >= sizeof(profile_rev) ||
        !cJSON_IsNumber(vid) || vid->valueint < 1 || vid->valueint > 0xFFFF ||
        !cJSON_IsNumber(pid) || pid->valueint < 1 || pid->valueint > 0xFFFF || map_len < 8) {
        ESP_LOGW(TAG, "Ignoring invalid Bluetooth profile from the server");
        return;
    }
    if (!strcmp(rev->valuestring, profile_rev)) return;
    nvs_handle_t nvs;
    if (nvs_open("tv_remote", NVS_READWRITE, &nvs) != ESP_OK) return;
    esp_err_t err = nvs_set_str(nvs, "p_rev", rev->valuestring);
    if (err == ESP_OK) err = nvs_set_u16(nvs, "p_vid", (uint16_t)vid->valueint);
    if (err == ESP_OK) err = nvs_set_u16(nvs, "p_pid", (uint16_t)pid->valueint);
    if (err == ESP_OK) err = nvs_set_blob(nvs, "p_map", decoded, (size_t)map_len);
    if (err == ESP_OK) err = nvs_set_u8(nvs, "p_try", 0);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not store Bluetooth profile: %s", esp_err_to_name(err));
        return;
    }
    ESP_LOGW(TAG, "Received Bluetooth profile %s; restarting to apply it", rev->valuestring);
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
}

/* Wi-Fi change requested from the web UI ("wifi" object in the poll response). */
#define WIFI_APPLY_TIMEOUT_MS 20000
static char last_wifi_change_id[40];   /* never apply the same change twice */
static char wifi_result_id[40];        /* change whose outcome still has to be reported to the server */
static bool wifi_result_ok;

static esp_err_t save_wifi_credentials(const char *ssid, const char *password)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("tv_remote", NVS_READWRITE, &nvs);
    if (err != ESP_OK) return err;
    err = nvs_set_str(nvs, "ssid", ssid);
    if (err == ESP_OK) err = nvs_set_str(nvs, "password", password);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    return err;
}

/* Try the new network first; keep it only if the board gets an IP and can reach the backend through it.
 * Otherwise restore the previous credentials so a typo cannot strand the remote. NVS is written last. */
static bool apply_wifi_change(const char *ssid, const char *password)
{
    wifi_config_t previous, next;
    if (esp_wifi_get_config(WIFI_IF_STA, &previous) != ESP_OK) return false;
    next = previous;
    memset(next.sta.ssid, 0, sizeof(next.sta.ssid));
    memset(next.sta.password, 0, sizeof(next.sta.password));
    memcpy(next.sta.ssid, ssid, strlen(ssid));
    memcpy(next.sta.password, password, strlen(password));

    ESP_LOGI(TAG, "Trying new Wi-Fi network %s", ssid);
    xEventGroupClearBits(wifi_events, WIFI_HAS_IP);
    esp_wifi_set_config(WIFI_IF_STA, &next);
    esp_wifi_disconnect();   /* the disconnect handler reconnects with the new configuration */
    EventBits_t bits = xEventGroupWaitBits(wifi_events, WIFI_HAS_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(WIFI_APPLY_TIMEOUT_MS));
    char poll[160];
    snprintf(poll, sizeof(poll), "{\"device_id\":\"%s\",\"name\":\"%s\"}", device_id, ble_name);
    bool ok = (bits & WIFI_HAS_IP) && post_json("/api/poll", poll, NULL, 0) &&
              save_wifi_credentials(ssid, password) == ESP_OK;
    if (ok) {
        ESP_LOGI(TAG, "Switched to Wi-Fi network %s", ssid);
    } else {
        ESP_LOGW(TAG, "Could not use Wi-Fi network %s; restoring the previous one", ssid);
        esp_wifi_set_config(WIFI_IF_STA, &previous);
        esp_wifi_disconnect();
    }
    return ok;
}

static void backend_task(void *arg)
{
    (void)arg;
    bool registered = false;
    while (true) {
        xEventGroupWaitBits(wifi_events, WIFI_HAS_IP, pdFALSE, pdTRUE, portMAX_DELAY);
        if (wifi_result_id[0]) {
            char result[160];
            snprintf(result, sizeof(result), "{\"device_id\":\"%s\",\"change_id\":\"%s\",\"ok\":%s}",
                     device_id, wifi_result_id, wifi_result_ok ? "true" : "false");
            if (post_json("/api/wifi/result", result, NULL, 0)) wifi_result_id[0] = '\0';
        }
        char poll[256];
        int poll_len = snprintf(poll, sizeof(poll), "{\"device_id\":\"%s\",\"name\":\"%s\",\"profile_rev\":\"%s\"",
                                device_id, ble_name, profile_rev);
        if (profile_failed[0] && poll_len > 0 && poll_len < (int)sizeof(poll))
            poll_len += snprintf(poll + poll_len, sizeof(poll) - (size_t)poll_len, ",\"profile_failed\":\"%s\"", profile_failed);
        if (poll_len > 0 && poll_len < (int)sizeof(poll) - 2) strlcpy(poll + poll_len, "}", sizeof(poll) - (size_t)poll_len);
        if (post_json("/api/poll", poll, NULL, 0)) {
            if (!registered) {
                ESP_LOGI(TAG, "Registered with backend as %s", device_id);
                registered = true;
            }
            cJSON *root = cJSON_Parse(http_response);   /* cJSON copies the strings, so later requests may reuse the buffer */
            cJSON *command = root ? cJSON_GetObjectItemCaseSensitive(root, "command") : NULL;
            if (cJSON_IsObject(command)) {
                cJSON *id = cJSON_GetObjectItemCaseSensitive(command, "id");
                if (cJSON_IsString(id) && send_reports(cJSON_GetObjectItemCaseSensitive(command, "reports"))) {
                    char ack[192];
                    snprintf(ack, sizeof(ack), "{\"device_id\":\"%s\",\"command_id\":\"%s\"}", device_id, id->valuestring);
                    post_json("/api/ack", ack, NULL, 0);
                } else if (cJSON_IsString(id)) {
                    ESP_LOGW(TAG, "Command not sent (no BLE host connected, or invalid reports)");
                }
            }
            const cJSON *wifi = root ? cJSON_GetObjectItemCaseSensitive(root, "wifi") : NULL;
            if (cJSON_IsObject(wifi)) {
                const cJSON *change_id = cJSON_GetObjectItemCaseSensitive((cJSON *)wifi, "id");
                const cJSON *ssid = cJSON_GetObjectItemCaseSensitive((cJSON *)wifi, "ssid");
                const cJSON *password = cJSON_GetObjectItemCaseSensitive((cJSON *)wifi, "password");
                if (cJSON_IsString(change_id) && cJSON_IsString(ssid) && cJSON_IsString(password) &&
                    strlen(change_id->valuestring) < sizeof(last_wifi_change_id) &&
                    strcmp(change_id->valuestring, last_wifi_change_id) != 0) {
                    size_t ssid_len = strlen(ssid->valuestring), password_len = strlen(password->valuestring);
                    strlcpy(last_wifi_change_id, change_id->valuestring, sizeof(last_wifi_change_id));
                    strlcpy(wifi_result_id, change_id->valuestring, sizeof(wifi_result_id));
                    wifi_result_ok = ssid_len >= 1 && ssid_len <= 32 && password_len <= 63 &&
                                     apply_wifi_change(ssid->valuestring, password->valuestring);
                }
            }
            const cJSON *profile = root ? cJSON_GetObjectItemCaseSensitive(root, "profile") : NULL;
            if (cJSON_IsObject(profile)) apply_profile(profile);
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

    load_profile();
    esp_hid_device_config_t hid_config = {
        /* Identity and report map come from the server profile (default: Xiaomi Bluetooth Remote 2 Pro IDs
         * 0x2717/0x32B9, so a Xiaomi TV applies its own key layout for that remote). */
        .vendor_id = profile_vid,
        .product_id = profile_pid,
        .version = 0x0100,
        .device_name = ble_name,
        .manufacturer_name = "Accessible TV Remote",
        .serial_number = "ESP32C3",
        .report_maps = report_maps,
        .report_maps_len = 1,
    };
    ESP_ERROR_CHECK(esp_hidd_dev_init(&hid_config, ESP_HID_TRANSPORT_BLE, hid_event_handler, &hid_device));

    xTaskCreate(backend_task, "backend_poll", 8192, NULL, 5, NULL);

    configure_buttons();
    ESP_LOGI(TAG, "Ready. BLE HID name: %s", ble_name);
    bool was_pressed[BUTTON_COUNT] = {0};
    while (true) {
        if (profile_stored_in_use && esp_timer_get_time() > 20000000LL) {
            profile_stored_in_use = false;
            confirm_profile();
        }
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
