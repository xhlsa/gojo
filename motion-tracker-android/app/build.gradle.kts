plugins {
    id("com.android.application")
    kotlin("android")
}

android {
    compileSdk = 34
    namespace = "com.example.motiontracker"

    defaultConfig {
        applicationId = "com.example.motiontracker"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // NDK configuration for Rust JNI library
        ndk {
            abiFilters.addAll(listOf("arm64-v8a", "armeabi-v7a"))
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = "11"
    }

    buildFeatures {
        viewBinding = true
    }
}

// Pre-build: Compile Rust JNI library with cargo-ndk
tasks.register("buildRustJni") {
    doLast {
        val rustDir = projectDir.parentFile.resolve("rust")
        val jniLibDir = projectDir.resolve("src/main/jniLibs")

        // Build Rust library
        val buildCmd = if (System.getProperty("os.name").toLowerCase().contains("windows")) {
            // Windows
            "cmd /c cargo ndk -t arm64-v8a -t armeabi-v7a -o $jniLibDir build --release"
        } else {
            // Unix/Mac/Linux
            "bash -c 'cd $rustDir && cargo ndk -t arm64-v8a -t armeabi-v7a -o $jniLibDir build --release'"
        }

        val process = Runtime.getRuntime().exec(buildCmd)
        val exitCode = process.waitFor()
        if (exitCode != 0) {
            println("Rust build stdout: ${process.inputStream.bufferedReader().readText()}")
            println("Rust build stderr: ${process.errorStream.bufferedReader().readText()}")
            throw RuntimeException("Rust JNI build failed with exit code $exitCode")
        }
        println("✓ Rust JNI library built successfully")
    }
}

tasks.preBuild.get().dependsOn("buildRustJni")

dependencies {
    // Android Core
    implementation("androidx.core:core-ktx:1.10.1")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")

    // Kotlin
    implementation("org.jetbrains.kotlin:kotlin-stdlib:1.9.20")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")

    // Location Services (for GPS)
    implementation("com.google.android.gms:play-services-location:21.0.1")

    // JSON serialization
    implementation("com.google.code.gson:gson:2.10.1")

    // Testing
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.5.1")
}
