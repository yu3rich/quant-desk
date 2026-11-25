package com.quantdesk.mobile

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.View
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.io.FileOutputStream

/**
 * WebView 容器：进程内启动 Flask(127.0.0.1:8765) 后加载前端。
 *
 * 数据流：
 *   MainActivity -> Python.start() -> QuantDeskServer.start(port, dataDir, webDir)
 *   -> Flask 线程监听 127.0.0.1:8765 -> WebView.loadUrl("http://127.0.0.1:8765")
 *   （Flask 只 bind 127.0.0.1，不暴露端口给其他应用）
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progress: ProgressBar
    private val port = 8765

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        progress = findViewById(R.id.progress)
        webView = findViewById(R.id.webview)

        startBackend()

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true          // web/app.js 用 localStorage 存 UI 状态
            allowFileAccess = false
            databaseEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                progress.progress = newProgress
                progress.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
            }
        }
        webView.webViewClient = object : WebViewClient() {
            // 所有导航都留在进程内，不跳外部浏览器
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean = false
        }
        webView.loadUrl("http://127.0.0.1:$port/")
    }

    /** 进程内启动 Python/Flask 后端；assets/web 解包到 filesDir 供 Flask 静态目录使用 */
    private fun startBackend() {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        val py = Python.getInstance()

        // 把打包进 assets/web/ 的前端解包到私有目录（Flask 需要真实文件路径）
        val webDir = File(filesDir, "web")
        copyAssetDir("web", webDir)

        // 数据目录：APK 私有空间，SQLite 落这里
        val dataDir = File(filesDir, "data").apply { mkdirs() }

        // QuantDeskServer.start() 定义在 src/main/python/server_main.py
        val server: PyObject = py.getModule("server_main")
        server.callAttr("start", port, dataDir.absolutePath, webDir.absolutePath)
    }

    private fun copyAssetDir(assetPath: String, targetDir: File) {
        val am = assets
        val children = am.list(assetPath) ?: return
        targetDir.mkdirs()
        for (child in children) {
            val assetChild = "$assetPath/$child"
            val targetChild = File(targetDir, child)
            if ((am.list(assetChild) ?: emptyArray()).isNotEmpty()) {
                copyAssetDir(assetChild, targetChild)
            } else {
                am.open(assetChild).use { input ->
                    FileOutputStream(targetChild).use { output ->
                        input.copyTo(output)
                    }
                }
            }
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
        // Flask 线程随进程结束；scheduler 定时任务在手机版按需触发，见 README「未验证清单」
    }
}
