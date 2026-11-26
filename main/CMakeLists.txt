idf_component_register(
    SRCS "main.c" "esp_hid_gap.c"
    INCLUDE_DIRS "."
    REQUIRES esp_hid driver esp_event esp_netif esp_wifi nvs_flash bt esp_http_client json
    PRIV_REQUIRES mbedtls
)
