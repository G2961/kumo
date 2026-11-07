import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val localSigning = Properties().apply {
    val config = file("signing.properties")
    if (config.isFile) config.inputStream().use { load(it) }
}
fun signingValue(environment: String, property: String): String? =
    providers.environmentVariable(environment).orNull?.takeIf { it.isNotBlank() }
        ?: localSigning.getProperty(property)?.takeIf { it.isNotBlank() }
val releaseStore = file(signingValue("ANDROID_KEYSTORE_PATH", "storeFile") ?: "opencode.jks")
val releaseStorePassword = signingValue("ANDROID_KEYSTORE_PASSWORD", "storePassword")
val releaseAlias = signingValue("ANDROID_KEY_ALIAS", "keyAlias")
val releaseKeyPassword = signingValue("ANDROID_KEY_PASSWORD", "keyPassword")
val hasReleaseSigning = releaseStore.isFile && releaseStorePassword != null && releaseAlias != null && releaseKeyPassword != null

android {
    namespace = "space.g2961.opencode"
    compileSdk = 35

    defaultConfig {
        applicationId = "space.g2961.opencode"
        minSdk = 26
        targetSdk = 28
        versionCode = 12
        versionName = "0.3.2"
        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = releaseStore
                storePassword = releaseStorePassword
                keyAlias = releaseAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        debug {
            if (hasReleaseSigning) signingConfig = signingConfigs.getByName("release")
        }
        release {
            if (hasReleaseSigning) signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = false
        }
    }

    packaging {
        resources {
            excludes += "META-INF/versions/**/OSGI-INF/MANIFEST.MF"
            merges += "META-INF/LICENSE.txt"
        }
        jniLibs {
            useLegacyPackaging = true
        }
    }

    lint {
        abortOnError = false
        disable += "ExpiredTargetSdkVersion"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.10.0")
    implementation("com.github.mwiede:jsch:0.2.26")
    implementation("org.bouncycastle:bcprov-jdk18on:1.80")
    implementation("org.eclipse.jgit:org.eclipse.jgit:6.10.0.202406032230-r")
}
