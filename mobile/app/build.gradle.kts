plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android") version "1.9.22"
    id("com.chaquo.python")
}

android {
    namespace = "com.quantdesk.mobile"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.quantdesk.mobile"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        ndk {
            // Chaquopy 15：arm64-v8a = 当前设备；x86_64 = 模拟器（Apple silicon 上可去掉）
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging {
        resources.excludes += setOf("META-INF/*.md")
    }
}

dependencies {
    // MainActivity 继承 AppCompatActivity，主题 parent 也是 Theme.AppCompat
    implementation("androidx.appcompat:appcompat:1.6.1")
}

chaquopy {
    defaultConfig {
        // Chaquopy 15 默认 3.8；3.12+ 仅支持 64 位 ABI，选 3.11 兼容 arm64-v8a + x86_64
        version = "3.11"
        pip {
            // 纯 Python 包，走 PyPI 即可；含 C 扩展的包需 Chaquopy 官方 Android wheel
            // （Chaquopy 会自动在 chaquo.com/pypi-13.1/ 中查找，无需手动 index）
            install("pandas")          // Chaquopy 官方 Android wheel
            install("numpy")           // Chaquopy 官方 Android wheel（pandas 依赖）
            install("flask")           // app.py 的 Web 框架（此前遗漏导致启动闪退）
            install("requests")        // 纯 Python
            install("rsa")             // 纯 Python（emtl 依赖）
            install("Pillow")          // Chaquopy 官方 Android wheel（emtl 验证码识别用）
            install("baostock")        // 纯 Python
            // 网络不稳：加长 pip 超时与重试（不改源，Chaquopy 的 chaquo.com 索引由插件自动注入）
            options("--timeout", "120")
            options("--retries", "20")
            // LightGBM：Chaquopy 仓库无 Android wheel，需用
            // chaquopy/server/pypi/build-wheel.py 在 x86-64 Linux 主机自编译后：
            //   install("./libs/lightgbm-*.whl")   // 本地 wheel，路径须含斜杠
            // 未编译前不要启用此行，否则 Gradle 阶段直接失败。
        }
    }
}
