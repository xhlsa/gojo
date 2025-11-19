// Root build.gradle.kts for motion-tracker-android

plugins {
    id("com.android.application") version "8.1.0" apply false
    kotlin("android") version "1.9.20" apply false
}

tasks.register("clean", Delete::class) {
    delete(rootProject.buildDir)
}
