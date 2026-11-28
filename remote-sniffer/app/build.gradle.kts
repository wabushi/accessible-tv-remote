plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.accessibletvremote.sniffer"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.accessibletvremote.sniffer"
        minSdk = 26
        targetSdk = 35
        versionCode = 4
        versionName = "0.1.3"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}
